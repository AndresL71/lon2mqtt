#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
lon2mqtt - LonWorks (ISO/IEC 14908) <-> MQTT bridge with Home Assistant discovery.

Talks directly to an EnOcean/Echelon USB network interface running MIP/U61 firmware
(e.g. U10 FT rev B) through its FTDI serial port (/dev/ttyUSBx). No kernel driver,
no lonifd daemon, no LON network interface is required.

  * Link layer : UMIP framing at 460800 8N1, interface placed in layer-2 mode.
  * Reads      : network-management "NV Fetch" (0x73) addressed by Neuron ID.
  * Writes     : explicit acknowledged NV update to the node's subnet/node,
                 using the selector reported by "Query NV Config" (0x68).
  * Events     : the interface runs promiscuously in layer 2, so bound NV updates
                 seen on the bus trigger an immediate re-poll of the affected entity.

Everything site-specific (MQTT broker, domain, addresses, Neuron IDs, entities)
lives in the YAML configuration file. See README.md and config.example.yaml.

Usage:
  lon2mqtt.py -c config.yaml                  run the bridge
  lon2mqtt.py -c config.yaml scan  <node>     list every NV of a node
  lon2mqtt.py -c config.yaml read  <node> <nv> [--type SNVT_x]
  lon2mqtt.py -c config.yaml write <node> <nv> <value> [--type SNVT_x]
  lon2mqtt.py -c config.yaml sniff            decode bus traffic

<node> is a node id/name from the configuration or a 12-hex-digit Neuron ID.
"""

import argparse
import fcntl
import json
import logging
import os
import queue
import re
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install pyyaml")

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

__version__ = "0.2.0"
log = logging.getLogger("lon2mqtt")


# ============================================================================
# UMIP / network-interface constants
# (reference: izot/lon-driver u61/U61Link.c, izot/lon-stack-dx lon_usb_link.c)
# ============================================================================

UMIP_SYNC = 0x7E            # frame = 7E 00 [len][ni_cmd][data...]; 7E in data is doubled
UMIP_EXT_LENGTH = 0xFF      # len == FF -> 16-bit big-endian length follows

NI_L2_SEND = 0x12           # downlink layer-2 frame: [LPDU header][NPDU] (NI appends CRC)
NI_L2_INCOMING = 0x1A       # uplink layer-2 frame:   [LPDU header][NPDU][CRC16]
NI_LAYER_MODE = 0xE5        # payload 1 = layer 2, 0 = layer 5
NI_RESET = 0x50
NI_CRC_ERROR = 0x31

# LonTalk PDU formats / types
PDU_TPDU, PDU_SPDU, PDU_AUTH, PDU_APDU = 0, 1, 2, 3
TPDU_ACKD, TPDU_UNACKD_RPT, TPDU_ACK = 0, 1, 2
SPDU_REQUEST, SPDU_RESPONSE = 0, 2
AUTH_CHALLENGE = 0

# Network-management message codes
NM_QUERY_NV_CONFIG = 0x68
NM_NV_FETCH = 0x73
NM_SUCCESS = lambda code: (code & 0x1F) | 0x20   # noqa: E731
NM_FAILURE = lambda code: code & 0x1F             # noqa: E731

DOMAIN_LEN_CODE = {0: 0, 1: 1, 3: 2, 6: 3}
DOMAIN_LEN = {v: k for k, v in DOMAIN_LEN_CODE.items()}

BOUND_SELECTOR_LIMIT = 0x3000   # selectors 0x0000-0x2FFF are bound connections


class LonError(Exception):
    pass


class LonTimeout(LonError):
    pass


class LonRejected(LonError):
    """The node answered with a failure code."""


class LonAuthRequired(LonError):
    pass


# ============================================================================
# SNVT codecs
# ============================================================================

class SwitchCodec:
    """SNVT_switch (#95): value 0..100 % in 0.5 % steps, state -1 (null) / 0 / 1."""
    name = "SNVT_switch"
    size = 2
    unit = "%"

    def decode(self, b):
        state = b[1] - 256 if b[1] > 127 else b[1]
        return {"value": b[0] / 2.0, "state": state}

    def encode(self, v):
        value, state = v["value"], v["state"]
        if not 0 <= value <= 100:
            raise ValueError("SNVT_switch value must be 0..100")
        return bytes([int(round(value * 2)), state & 0xFF])

    def parse(self, text):
        """'75:1', '75:on', 'on', 'off'."""
        t = text.strip().lower()
        if t in ("on", "off"):
            return {"value": 100.0 if t == "on" else 0.0, "state": 1 if t == "on" else 0}
        val, _, st = t.partition(":")
        st = {"on": 1, "off": 0, "null": -1}.get(st, st or "1")
        return {"value": float(val), "state": int(st)}


class NumericCodec:
    """Fixed-point scalar SNVT: physical = raw * scale + offset."""

    def __init__(self, name, size, signed, scale=1.0, offset=0.0, unit=None,
                 device_class=None, decimals=None):
        self.name, self.size, self.signed = name, size, signed
        self.scale, self.offset, self.unit, self.device_class = scale, offset, unit, device_class
        self.decimals = decimals if decimals is not None else max(0, -int(f"{scale:e}".split("e")[1]))

    def decode(self, b):
        raw = int.from_bytes(b[:self.size], "big", signed=self.signed)
        v = round(raw * self.scale + self.offset, self.decimals)
        return int(v) if self.decimals == 0 else v

    def encode(self, v):
        raw = int(round((float(v) - self.offset) / self.scale))
        return raw.to_bytes(self.size, "big", signed=self.signed)

    def parse(self, text):
        return float(text)


class EnumCodec:
    def __init__(self, name, size, labels):
        self.name, self.size, self.labels = name, size, labels
        self.unit = None
        self.device_class = "enum"

    def decode(self, b):
        raw = int.from_bytes(b[:self.size], "big", signed=True)
        return self.labels.get(raw, raw)

    def encode(self, v):
        rev = {lbl: k for k, lbl in self.labels.items()}
        raw = rev[v] if v in rev else int(v)
        return raw.to_bytes(self.size, "big", signed=True)

    def parse(self, text):
        return text


class RawCodec:
    name = "raw"
    size = None
    unit = None

    def decode(self, b):
        return b.hex()

    def encode(self, v):
        return bytes.fromhex(v)

    def parse(self, text):
        return text


CODECS = {c.name: c for c in (
    SwitchCodec(),
    RawCodec(),
    NumericCodec("SNVT_lux", 2, False, 1, unit="lx", device_class="illuminance"),
    NumericCodec("SNVT_temp_p", 2, True, 0.01, unit="°C", device_class="temperature"),
    NumericCodec("SNVT_temp", 2, False, 0.1, -274.0, unit="°C", device_class="temperature"),
    NumericCodec("SNVT_lev_percent", 2, True, 0.005, unit="%", decimals=3),
    NumericCodec("SNVT_lev_cont", 1, False, 0.5, unit="%"),
    NumericCodec("SNVT_count", 2, False, 1),
    NumericCodec("SNVT_power", 2, False, 0.1, unit="W", device_class="power"),
    NumericCodec("SNVT_elec_kwh", 2, False, 1, unit="kWh", device_class="energy"),
    NumericCodec("SNVT_press_p", 2, True, 1, unit="Pa", device_class="pressure"),
    NumericCodec("SNVT_speed", 2, False, 0.1, unit="m/s", device_class="speed"),
    EnumCodec("SNVT_occupancy", 1, {-1: "null", 0: "occupied", 1: "unoccupied",
                                    2: "bypass", 3: "standby"}),
)}


# ============================================================================
# Serial link: UMIP framing
# ============================================================================

def umip_encode(ni_cmd, data):
    out = bytearray([UMIP_SYNC, 0x00])
    n = len(data) + 1
    if n < UMIP_EXT_LENGTH:
        out.append(n)                       # U10/U20 firmware: length and cmd not escaped
    else:
        out.append(UMIP_EXT_LENGTH)
        for b in (n >> 8, n & 0xFF):
            out += bytes([b, b]) if b == UMIP_SYNC else bytes([b])
    out.append(ni_cmd)
    for b in data:
        out += bytes([b, b]) if b == UMIP_SYNC else bytes([b])
    return bytes(out)


class UmipDecoder:
    """Uplink state machine (mirrors ProcessUplink() in U61Link.c)."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.state, self.buf, self.length, self.hdr = "IDLE1", bytearray(), None, 1

    def _check(self):
        m = self.buf
        if self.length is None and len(m) >= 1:
            self.length = m[0]
            self.hdr = 1
        if self.length == UMIP_EXT_LENGTH and self.hdr == 1:
            if len(m) < 3:
                return None
            self.length, self.hdr = (m[1] << 8) | m[2], 3
        if self.length is not None and len(m) >= self.hdr + self.length:
            cmd = m[self.hdr]
            data = bytes(m[self.hdr + 1:self.hdr + self.length])
            self._reset()
            return cmd, data
        return None

    def feed(self, chunk):
        frames = []
        for b in chunk:
            if self.state == "IDLE1":
                if b == UMIP_SYNC:
                    self.state = "IDLE2"
            elif self.state == "IDLE2":
                if b == 0x00:
                    self.state, self.buf, self.length = "PACKET", bytearray(), None
                else:
                    self.state = "IDLE1"
            elif self.state == "PACKET":
                if b == UMIP_SYNC:
                    self.state = "ESC"
                    continue
                self.buf.append(b)
                f = self._check()
                if f:
                    frames.append(f)
            elif self.state == "ESC":
                if b == UMIP_SYNC:
                    self.state = "PACKET"
                    self.buf.append(UMIP_SYNC)
                    f = self._check()
                    if f:
                        frames.append(f)
                elif b == 0x00:             # re-sync mid-frame
                    self.state, self.buf, self.length = "PACKET", bytearray(), None
                else:
                    self._reset()
        return frames


def open_serial(path, baud=460800):
    speed = getattr(termios, f"B{baud}")
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR |
               termios.IGNCR | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CLOCAL | termios.CREAD
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
    try:
        fcntl.ioctl(fd, getattr(termios, "TIOCMBIS", 0x5416),
                    struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
    except OSError:
        pass                                # e.g. pseudo-terminals used for testing
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


# ============================================================================
# LonTalk packet construction / parsing
# ============================================================================

def crc16_lontalk(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def nv_index_bytes(idx):
    return bytes([idx]) if idx < 255 else bytes([0xFF, idx >> 8, idx & 0xFF])


def parse_lpdu(lpdu):
    """Parse an uplink LPDU ([hdr][NPDU][CRC]). Returns dict or None."""
    if len(lpdu) < 6:
        return None
    body, crc = lpdu[:-2], (lpdu[-2] << 8) | lpdu[-1]
    npdu = body[1:]
    b0 = npdu[0]
    pdufmt, addrfmt = (b0 >> 4) & 3, (b0 >> 2) & 3
    dlen = DOMAIN_LEN[b0 & 3]
    try:
        src = (npdu[1], npdu[2] & 0x7F)
        selfield = npdu[2] >> 7
        i, dst = 3, None
        if addrfmt == 0:                        # broadcast
            dst, i = ("broadcast", npdu[3]), 4
        elif addrfmt == 1:                      # group
            dst, i = ("group", npdu[3]), 4
        elif addrfmt == 2 and selfield:         # subnet/node
            dst, i = ("node", (npdu[3], npdu[4] & 0x7F)), 5
        elif addrfmt == 2:                      # group acknowledgement
            dst, i = ("node", (npdu[3], npdu[4] & 0x7F)), 7
        else:                                   # Neuron ID
            dst, i = ("nid", bytes(npdu[4:10])), 10
        domain = bytes(npdu[i:i + dlen])
        pdu = bytes(npdu[i + dlen:])
    except IndexError:
        return None
    if not pdu:
        return None
    return {"pdufmt": pdufmt, "src": src, "dst": dst, "domain": domain, "pdu": pdu,
            "crc_ok": crc16_lontalk(body) == crc, "raw": lpdu}


def nv_update_in_packet(p):
    """Return the selector if the packet carries an NV update, else None."""
    pdu = p["pdu"]
    if p["pdufmt"] == PDU_APDU:
        apdu = pdu
    elif p["pdufmt"] == PDU_TPDU and ((pdu[0] >> 4) & 7) in (TPDU_ACKD, TPDU_UNACKD_RPT):
        apdu = pdu[1:]
    elif p["pdufmt"] == PDU_SPDU and ((pdu[0] >> 4) & 7) == SPDU_REQUEST:
        apdu = pdu[1:]
    else:
        return None
    if len(apdu) >= 2 and (apdu[0] & 0xC0) == 0x80:
        return ((apdu[0] & 0x3F) << 8) | apdu[1]
    return None


# ============================================================================
# LON link: serial I/O thread + request/response transactions
# ============================================================================

class NvInfo:
    def __init__(self, index, cfg):
        self.index = index
        self.raw = cfg
        self.is_output = bool(cfg[0] & 0x40)
        self.selector = ((cfg[0] & 0x3F) << 8) | cfg[1]
        self.priority = bool(cfg[0] & 0x80)
        b2 = cfg[2] if len(cfg) > 2 else 0x0F
        self.turnaround = bool(b2 & 0x80)
        self.service = (b2 >> 5) & 3            # 0 ackd, 1 unackd-repeated, 2 unackd
        self.auth = bool(b2 & 0x10)
        self.addr_index = b2 & 0x0F             # 15 = no address table entry
        self.bound = self.selector < BOUND_SELECTOR_LIMIT

    def __repr__(self):
        return (f"NV{self.index}({'out' if self.is_output else 'in'}, sel=0x{self.selector:04x}"
                f"{', bound' if self.bound else ''}{', auth' if self.auth else ''})")


class LonNode:
    def __init__(self, id, name, neuron_id, subnet=None, node=None):
        self.id, self.name = id, name
        self.nid = parse_neuron_id(neuron_id)
        self.subnet, self.node = subnet, node
        self.nv_info = {}
        self.fail_count = 0                 # consecutive timeouts (bridge back-off)
        self.retry_at = 0.0
        self.unresolved = False             # NV configuration still to be queried

    @property
    def nid_hex(self):
        return self.nid.hex()


def parse_neuron_id(text):
    if isinstance(text, (int, float)):
        raise ValueError("put the Neuron ID in quotes in the YAML file (it was read as a number)")
    h = re.sub(r"[^0-9a-fA-F]", "", str(text))
    if len(h) != 12:
        raise ValueError(f"Neuron ID must have 12 hex digits (48 bits), got '{text}'")
    return bytes.fromhex(h)


class LonLink:
    def __init__(self, cfg):
        self.port = cfg.get("serial_port", "/dev/ttyUSB0")
        self.baud = int(cfg.get("baudrate", 460800))
        self.domain = bytes.fromhex(str(cfg.get("domain_id", "")))
        if len(self.domain) not in DOMAIN_LEN_CODE:
            raise ValueError("domain_id must be 0, 1, 3 or 6 bytes")
        self.src_subnet = int(cfg.get("source_subnet", 1))
        self.src_node = int(cfg.get("source_node", 127))
        if not (1 <= self.src_subnet <= 255 and 1 <= self.src_node <= 127):
            raise ValueError("source_subnet must be 1..255 and source_node 1..127")
        self.timeout = float(cfg.get("timeout", 1.5))
        self.retries = int(cfg.get("retries", 3))
        self.fd = None
        self._decoder = UmipDecoder()
        self._tx_lock = threading.Lock()
        self._wlock = threading.Lock()
        self._pending = None                 # (matcher, event, result list)
        self._mode_event = threading.Event()
        self._tid = int(time.time()) % 15
        self._reader = None
        self._stop = threading.Event()
        self.on_packet = []                  # callbacks(packet) for unsolicited traffic
        self.on_traffic = []                 # callbacks(direction, packet) for every packet
        self.connected = False

    # ---------------------------------------------------------------- lifecycle
    def open(self):
        self.fd = open_serial(self.port, self.baud)
        self._decoder = UmipDecoder()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="lon-reader", daemon=True)
        self._reader.start()
        self._mode_event.clear()
        self._write(umip_encode(NI_LAYER_MODE, b"\x01"))
        if self._mode_event.wait(1.0):
            log.info("Interface on %s in layer-2 mode", self.port)
        else:
            log.warning("No layer-mode echo from %s; continuing anyway", self.port)
        self.connected = True

    def close(self):
        self._stop.set()
        self.connected = False
        if self._reader:
            self._reader.join(2)
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _write(self, frame):
        with self._wlock:
            log.debug("TX %s", frame.hex())
            os.write(self.fd, frame)

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if not r:
                    continue
                chunk = os.read(self.fd, 1024)
                if not chunk:
                    raise OSError("serial port closed")
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as e:
                log.error("Serial read error: %s", e)
                self.connected = False
                return
            for cmd, data in self._decoder.feed(chunk):
                try:
                    self._on_frame(cmd, data)
                except Exception:           # never kill the reader thread
                    log.exception("Error handling uplink frame")

    def _on_frame(self, cmd, data):
        if cmd == NI_LAYER_MODE:
            self._mode_event.set()
            return
        if cmd == NI_RESET:
            log.warning("Network interface reset; restoring layer-2 mode")
            self._write(umip_encode(NI_LAYER_MODE, b"\x01"))
            return
        if cmd == NI_CRC_ERROR:
            log.debug("Interface reported a bus CRC error")
            return
        if cmd != NI_L2_INCOMING:
            log.debug("RX ni_cmd 0x%02x %s", cmd, data.hex())
            return
        p = parse_lpdu(data)
        if not p:
            return
        log.debug("RX %s", data.hex())
        for cb in self.on_traffic:
            cb("rx", p)
        pend = self._pending
        if pend and pend[0](p):
            pend[2].append(p)
            if not pend[3]:                 # single-response transaction
                pend[1].set()
            return
        for cb in self.on_packet:
            cb(p)

    # ---------------------------------------------------------------- packets
    def _next_tid(self):
        self._tid = self._tid % 15 + 1          # 1..15, rolling
        return self._tid

    def _npdu(self, pdufmt, dest, pdu):
        """dest: bytes(6) Neuron ID, (subnet, node) or ("broadcast", subnet) with 0 = domain."""
        if isinstance(dest, (bytes, bytearray)):
            addrfmt, addr = 3, bytes([self.src_subnet, 0x80 | self.src_node, 0x00]) + dest
        elif dest[0] == "broadcast":
            addrfmt, addr = 0, bytes([self.src_subnet, 0x80 | self.src_node, dest[1]])
        else:
            addrfmt, addr = 2, bytes([self.src_subnet, 0x80 | self.src_node,
                                      dest[0], 0x80 | dest[1]])
        hdr = (pdufmt << 4) | (addrfmt << 2) | DOMAIN_LEN_CODE[len(self.domain)]
        return bytes([0x01, hdr]) + addr + self.domain + pdu     # LPDU hdr: prio 0, backlog 1

    def _to_me(self, p):
        return p["dst"] == ("node", (self.src_subnet, self.src_node)) and p["domain"] == self.domain

    def _send(self, lpdu):
        self._write(umip_encode(NI_L2_SEND, lpdu))
        if self.on_traffic:
            crc = crc16_lontalk(lpdu)
            p = parse_lpdu(lpdu + bytes([crc >> 8, crc & 0xFF]))
            if p:
                for cb in self.on_traffic:
                    cb("tx", p)

    def _transact(self, lpdu, matcher, timeout, collect=False):
        """Send and wait for the first matching packet, or (collect) for all within timeout."""
        if not self.connected:
            raise LonError("link not connected")
        with self._tx_lock:
            ev, res = threading.Event(), []
            self._pending = (matcher, ev, res, collect)
            try:
                self._send(lpdu)
                ev.wait(timeout)
            finally:
                self._pending = None
            if collect:
                return res
            return res[0] if res else None

    def send_unackd(self, dest, apdu, repeat=1):
        """Unacknowledged APDU (no reply expected)."""
        if not self.connected:
            raise LonError("link not connected")
        with self._tx_lock:
            for i in range(repeat):
                self._send(self._npdu(PDU_APDU, dest, apdu))
                if i + 1 < repeat:
                    time.sleep(0.05)

    def send_ackd(self, dest, apdu, auth=False, what="message"):
        """Acknowledged APDU; raises LonTimeout / LonAuthRequired."""
        for attempt in range(self.retries):
            tid = self._next_tid()
            hdr = (0x80 if auth else 0) | (TPDU_ACKD << 4) | tid
            lpdu = self._npdu(PDU_TPDU, dest, bytes([hdr]) + apdu)

            def match(p, tid=tid):
                if not self._to_me(p) or p["pdu"][0] & 0x0F != tid:
                    return False
                if p["pdufmt"] == PDU_TPDU and (p["pdu"][0] >> 4) & 7 == TPDU_ACK:
                    return True
                return p["pdufmt"] == PDU_AUTH and (p["pdu"][0] >> 4) & 3 == AUTH_CHALLENGE

            p = self._transact(lpdu, match, self.timeout)
            if p is None:
                continue
            if p["pdufmt"] == PDU_AUTH:
                raise LonAuthRequired(f"{what} requires LonTalk authentication (not supported)")
            return
        raise LonTimeout(f"no ACK for {what}")

    def broadcast_request(self, code, data=b"", window=1.0, subnet=0):
        """Request/response to every node of the domain; returns [(src, response bytes)]."""
        tid = self._next_tid()
        lpdu = self._npdu(PDU_SPDU, ("broadcast", subnet),
                          bytes([(SPDU_REQUEST << 4) | tid, code]) + data)

        def match(p, tid=tid):
            return (p["pdufmt"] == PDU_SPDU and self._to_me(p)
                    and (p["pdu"][0] >> 4) & 7 == SPDU_RESPONSE and p["pdu"][0] & 0x0F == tid)

        return [(p["src"], p["pdu"][1:]) for p in self._transact(lpdu, match, window, collect=True)]

    # ---------------------------------------------------------------- services
    def nm_request(self, node, code, data):
        """Network-management request addressed by Neuron ID. Returns response data."""
        for attempt in range(self.retries):
            tid = self._next_tid()
            lpdu = self._npdu(PDU_SPDU, node.nid, bytes([(SPDU_REQUEST << 4) | tid, code]) + data)

            def match(p, tid=tid):
                return (p["pdufmt"] == PDU_SPDU and self._to_me(p)
                        and (p["pdu"][0] >> 4) & 7 == SPDU_RESPONSE and p["pdu"][0] & 0x0F == tid)

            p = self._transact(lpdu, match, self.timeout)
            if p is None:
                continue
            if node.subnet is None and p["src"][0]:     # 0/0 = unconfigured device
                node.subnet, node.node = p["src"]
                log.info("Node %s is subnet/node %d/%d", node.name, *p["src"])
            resp = p["pdu"][1:]
            if not resp:
                raise LonError("empty NM response")
            if resp[0] == NM_SUCCESS(code):
                return resp[1:]
            if resp[0] == NM_FAILURE(code):
                raise LonRejected(f"request 0x{code:02x} rejected by {node.name}")
            raise LonError(f"unexpected response code 0x{resp[0]:02x} from {node.name}")
        raise LonTimeout(f"no response from {node.name} (request 0x{code:02x})")

    def nm_ackd(self, node, code, data=b""):
        """Network-management command that nodes only accept as a non-request message."""
        self.send_ackd(node.nid, bytes([code]) + data, what=f"command 0x{code:02x} to {node.name}")

    def query_nv_config(self, node, idx):
        cfg = self.nm_request(node, NM_QUERY_NV_CONFIG, nv_index_bytes(idx))
        if len(cfg) < 2:
            raise LonError(f"short nv_config for NV{idx}: {cfg.hex()}")
        info = NvInfo(idx, cfg)
        node.nv_info[idx] = info
        return info

    def nv_info(self, node, idx):
        return node.nv_info.get(idx) or self.query_nv_config(node, idx)

    def nv_fetch(self, node, idx):
        data = self.nm_request(node, NM_NV_FETCH, nv_index_bytes(idx))
        if data[:1] == b"\xff" and idx >= 255:
            return data[3:]
        return data[1:]

    def nv_update(self, node, idx, value):
        """Acknowledged NV update. Raises on NACK/timeout/authentication."""
        info = self.nv_info(node, idx)
        if info.is_output:
            raise LonError(f"NV{idx} on {node.name} is an output; nodes ignore updates to outputs")
        dest = (node.subnet, node.node) if node.subnet is not None else node.nid
        apdu = bytes([0x80 | (info.selector >> 8), info.selector & 0xFF]) + value
        self.send_ackd(dest, apdu, auth=info.auth, what=f"NV{idx} update on {node.name}")


# ============================================================================
# Entities
# ============================================================================

ENTITY_TYPES = ("light", "switch", "binary_sensor", "sensor", "number")


class Entity:
    def __init__(self, node, cfg, base_topic):
        self.node = node
        self.id = slug(cfg["id"])
        self.name = cfg.get("name", self.id)
        self.type = cfg.get("type", "sensor")
        if self.type not in ENTITY_TYPES:
            raise ValueError(f"{self.id}: type must be one of {ENTITY_TYPES}")
        self.nv_in = cfg.get("nv_in", cfg.get("snvt_in"))
        self.nv_out = cfg.get("nv_out", cfg.get("snvt_out"))
        tname = cfg.get("snvt_type", "raw")
        if tname not in CODECS:
            raise ValueError(f"{self.id}: unknown snvt_type '{tname}' (known: {sorted(CODECS)})")
        self.codec = CODECS[tname]
        self.poll_interval = float(cfg.get("polling_interval", 30))
        self.field = cfg.get("field")                  # SNVT_switch sensors: "value" | "state"
        self.dimmable = bool(cfg.get("dimmable", True))
        self.invert = bool(cfg.get("invert", False))
        self.device_class = cfg.get("device_class", getattr(self.codec, "device_class", None))
        self.unit = cfg.get("unit", self.codec.unit)
        self.min, self.max, self.step = cfg.get("min"), cfg.get("max"), cfg.get("step")
        self.icon = cfg.get("icon")
        self.writable = self.type in ("light", "switch", "number")
        if self.writable and self.nv_in is None:
            raise ValueError(f"{self.id}: type '{self.type}' needs nv_in")
        if self.nv_in is None and self.nv_out is None:
            raise ValueError(f"{self.id}: needs nv_in and/or nv_out")
        if self.type == "light" and self.codec.name != "SNVT_switch":
            raise ValueError(f"{self.id}: lights require snvt_type SNVT_switch")
        self.state_topic = f"{base_topic}/{self.id}/state"
        self.command_topic = f"{base_topic}/{self.id}/set"
        self.last_value = None
        self.last_payload = None
        self.last_on_level = 100.0
        self.next_poll = 0.0
        self.state = None                    # last decoded state (dict)
        self.updated = None                  # time.time() of the last successful read
        self.error = None                    # last poll/write error text

    @property
    def poll_nv(self):
        return self.nv_out if self.nv_out is not None else self.nv_in

    @property
    def uid(self):
        return f"lon2mqtt_{self.node.nid_hex}_{self.id}"

    # ------------------------------------------------------------ LON -> MQTT
    def to_state(self, raw):
        v = self.codec.decode(raw)
        self.last_value = v
        payload = {"raw": raw.hex()}
        if isinstance(v, dict):                               # SNVT_switch
            on = v["state"] == 1 and v["value"] > 0
            if v["value"] > 0:
                self.last_on_level = v["value"]
            payload.update(value=v["value"], switch_state=v["state"])
            if self.type == "light":
                payload.update(state="ON" if on else "OFF",
                               brightness=int(round(v["value"] * 2)) if self.dimmable else None)
            elif self.type in ("switch", "binary_sensor"):
                on = v["state"] == 1
                payload["state"] = "ON" if on != self.invert else "OFF"
            elif self.field == "state":
                payload["value"] = v["state"]
        else:
            payload["value"] = v
            if self.type == "switch":
                payload["state"] = "ON" if (v not in (0, "0", "00")) != self.invert else "OFF"
            if self.type == "binary_sensor":
                active = v not in (0, "unoccupied", "null", "0000", "00")
                payload["state"] = "ON" if active != self.invert else "OFF"
        return {k: v for k, v in payload.items() if v is not None}

    # ------------------------------------------------------------ MQTT -> LON
    def from_command(self, text):
        text = text.strip()
        if self.type == "light":
            try:
                cmd = json.loads(text)
            except ValueError:
                cmd = {"state": text.upper()}
            if str(cmd.get("state", "ON")).upper() == "OFF":
                return self.codec.encode({"value": 0, "state": 0})
            if "brightness" in cmd and self.dimmable:
                level = max(0.0, min(100.0, float(cmd["brightness"]) / 2.0))
            else:
                level = self.last_on_level if self.dimmable else 100.0
            if level <= 0:
                return self.codec.encode({"value": 0, "state": 0})
            return self.codec.encode({"value": level, "state": 1})
        if self.type == "switch":
            on = text.upper() in ("ON", "1", "TRUE")
            if self.codec.name == "SNVT_switch":
                return self.codec.encode({"value": 100.0 if on else 0.0, "state": 1 if on else 0})
            return self.codec.encode(1 if on else 0)
        if self.type == "number":
            v = float(text)
            if self.min is not None and v < float(self.min) or self.max is not None and v > float(self.max):
                raise ValueError(f"{v} outside {self.min}..{self.max}")
            return self.codec.encode(v)
        raise ValueError(f"{self.type} entities are read-only")

    # ------------------------------------------------------------ discovery
    def discovery(self, prefix, avail_topic, sw_version):
        device = {
            "identifiers": [f"lon2mqtt_{self.node.nid_hex}"],
            "name": self.node.name,
            "manufacturer": "LonWorks",
            "model": f"Neuron ID {self.node.nid_hex.upper()}",
            "sw_version": f"lon2mqtt {sw_version}",
        }
        c = {"name": self.name, "unique_id": self.uid, "object_id": self.id,
             "state_topic": self.state_topic, "availability_topic": avail_topic,
             "device": device}
        if self.icon:
            c["icon"] = self.icon
        if self.type == "light":
            c.update(schema="json", command_topic=self.command_topic,
                     supported_color_modes=["brightness"] if self.dimmable else ["onoff"],
                     brightness_scale=200)
        elif self.type == "switch":
            c.update(command_topic=self.command_topic, value_template="{{ value_json.state }}",
                     payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF")
        elif self.type == "binary_sensor":
            c.update(value_template="{{ value_json.state }}", payload_on="ON", payload_off="OFF")
            if self.device_class and self.device_class != "enum":
                c["device_class"] = self.device_class
        elif self.type in ("sensor", "number"):
            c["value_template"] = "{{ value_json.value }}"
            if self.unit:
                c["unit_of_measurement"] = self.unit
            if self.device_class and self.device_class != "enum" and self.unit:
                c["device_class"] = self.device_class
            if self.type == "sensor" and not isinstance(self.codec, (EnumCodec, RawCodec)):
                c["state_class"] = "measurement"
            if self.type == "number":
                c["command_topic"] = self.command_topic
                for k in ("min", "max", "step"):
                    if getattr(self, k) is not None:
                        c[k] = getattr(self, k)
        topic = f"{prefix}/{self.type}/{self.node.id}/{self.id}/config"
        return topic, c


def slug(text):
    s = re.sub(r"[^a-z0-9_]+", "_", str(text).lower()).strip("_")
    if not s:
        raise ValueError(f"cannot derive an id from '{text}'")
    return s


# ============================================================================
# Bridge
# ============================================================================

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        m = cfg.get("mqtt", {})
        self.base = m.get("base_topic", "lon2mqtt").rstrip("/")
        self.prefix = m.get("discovery_prefix", "homeassistant").rstrip("/")
        self.avail_topic = f"{self.base}/status"
        self.link = LonLink(cfg.get("lonworks", {}))
        self.nodes, self.entities = load_nodes(cfg, self.base)
        self.by_command = {e.command_topic: e for e in self.entities if e.writable}
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.selector_map = {}
        self.link.on_packet.append(self._on_bus_packet)
        self.client = None
        self.mqtt_connected = False
        self.mqtt_enabled = bool(m.get("enabled", True)) and bool(m.get("broker"))
        self.paused = threading.Event()      # set = polling suspended (commands still work)
        self.on_state = []                   # callbacks(entity) after a state change or error

    # ------------------------------------------------------------ MQTT
    def mqtt_start(self):
        if not self.mqtt_enabled:
            log.info("MQTT disabled (no broker configured)")
            return
        if mqtt is None:
            log.error("MQTT disabled: paho-mqtt is not installed (pip install paho-mqtt)")
            self.mqtt_enabled = False
            return
        m = self.cfg.get("mqtt", {})
        cid = m.get("client_id", f"lon2mqtt-{socket.gethostname()}")
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:                          # paho-mqtt < 2.0
            c = mqtt.Client(client_id=cid)
        if m.get("username"):
            c.username_pw_set(m["username"], m.get("password"))
        if m.get("tls"):
            c.tls_set()
        c.will_set(self.avail_topic, "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 60)
        c.connect_async(m.get("broker", "localhost"), int(m.get("port", 1883)),
                        int(m.get("keepalive", 60)))
        c.loop_start()
        self.client = c

    def _on_disconnect(self, *args):
        self.mqtt_connected = False
        log.warning("MQTT disconnected")

    def _on_connect(self, client, userdata, flags, rc, *args):
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if failed:
            log.error("MQTT connection refused (%s)", rc)
            return
        self.mqtt_connected = True
        log.info("MQTT connected (%s)", rc)
        client.subscribe(f"{self.base}/+/set", qos=1)
        client.subscribe(f"{self.prefix}/status", qos=1)    # HA birth message
        self._publish_discovery()
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        for e in self.entities:
            e.last_payload = None                            # force republish
            e.next_poll = 0

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace")
        if msg.topic == f"{self.prefix}/status":
            if payload == "online":
                log.info("Home Assistant restarted; republishing discovery")
                self._publish_discovery()
                for e in self.entities:
                    e.last_payload = None
            return
        e = self.by_command.get(msg.topic)
        if e:
            self.jobs.put(("write", e, payload))

    def _publish_discovery(self):
        for e in self.entities:
            topic, conf = e.discovery(self.prefix, self.avail_topic, __version__)
            self.client.publish(topic, json.dumps(conf), qos=1, retain=True)

    def _publish_state(self, e, raw):
        state = e.to_state(raw)
        payload = json.dumps(state, sort_keys=True)
        had_error, e.error, e.updated = e.error, None, time.time()
        if payload != e.last_payload:
            e.state, e.last_payload = state, payload
            if self.client:
                self.client.publish(e.state_topic, payload, qos=0, retain=True)
            log.info("%s <- %s", e.id, payload)
        elif not had_error:
            return
        self._notify(e)

    def _notify(self, e):
        for cb in self.on_state:
            try:
                cb(e)
            except Exception:
                log.exception("state listener failed")

    def _fail(self, e, err):
        e.error = str(err)
        self._notify(e)

    def command(self, entity_id, payload):
        """Queue a command for an entity (same payloads as the MQTT command topic)."""
        for e in self.entities:
            if e.id == entity_id:
                if not e.writable:
                    raise ValueError(f"{entity_id} is read-only")
                self.jobs.put(("write", e, payload))
                return
        raise KeyError(entity_id)

    # ------------------------------------------------------------ LON side
    def _resolve(self, only=None):
        """
        Query NV configuration of every configured NV (selector, direction, auth).
        A node that does not answer is skipped after its first timeout and resolved
        again when it comes back, so a dead node cannot hold up start-up.
        """
        if only is None:
            self.selector_map.clear()
            for n in self.nodes:
                n.unresolved = False
        else:
            only.unresolved = False
        for e in self.entities:
            if only is not None and e.node is not only:
                continue
            for idx, want_out in ((e.nv_in, False), (e.nv_out, True)):
                if idx is None or e.node.unresolved or self.stop.is_set():
                    continue
                try:
                    info = self.link.nv_info(e.node, idx)
                except LonTimeout as err:
                    log.warning("%s does not answer (%s); will retry when it responds",
                                e.node.name, err)
                    e.node.unresolved = True
                    continue
                except LonError as err:
                    log.warning("%s: cannot query NV%s on %s: %s", e.id, idx, e.node.name, err)
                    continue
                log.info("%s: %s", e.id, info)
                if info.is_output != want_out:
                    log.warning("%s: NV%d is an %s but is configured as nv_%s", e.id, idx,
                                "output" if info.is_output else "input",
                                "out" if want_out else "in")
                if info.auth:
                    log.warning("%s: NV%d requires authentication; writes will fail", e.id, idx)
                if info.bound and e not in self.selector_map.setdefault(info.selector, []):
                    self.selector_map[info.selector].append(e)

    def _on_bus_packet(self, p):
        sel = nv_update_in_packet(p)
        if sel is None:
            return
        for e in self.selector_map.get(sel, ()):
            log.debug("Bus NV update sel 0x%04x from %s -> refresh %s", sel, p["src"], e.id)
            self.jobs.put(("poll_soon", e, 0.3))

    def _poll(self, e):
        n = e.node
        try:
            raw = self.link.nv_fetch(n, e.poll_nv)
            n.fail_count = 0
            if n.unresolved:
                self._resolve(n)
            if self.codec_size_ok(e, raw):
                self._publish_state(e, raw)
            else:
                self._fail(e, f"NV{e.poll_nv} returned {len(raw)} bytes, "
                              f"{e.codec.name} expects {e.codec.size}")
        except LonTimeout as err:
            # Back off a silent node so it cannot stall commands and other polls.
            n.fail_count += 1
            delay = min(60, 5 * 2 ** (n.fail_count - 1))
            n.retry_at = time.monotonic() + delay
            log.warning("%s: %s; retrying %s in %ds", e.id, err, n.name, delay)
            self._fail(e, err)
        except LonError as err:
            log.warning("%s: poll failed: %s", e.id, err)
            self._fail(e, err)

    @staticmethod
    def codec_size_ok(e, raw):
        if e.codec.size and len(raw) != e.codec.size:
            log.warning("%s: NV%d returned %d bytes, %s expects %d", e.id, e.poll_nv, len(raw),
                        e.codec.name, e.codec.size)
            return False
        return True

    def _write(self, e, payload):
        try:
            data = e.from_command(payload)
        except (ValueError, KeyError, TypeError) as err:
            log.warning("%s: invalid command %r: %s", e.id, payload, err)
            self._fail(e, f"invalid command: {err}")
            return
        try:
            self.link.nv_update(e.node, e.nv_in, data)
            log.info("%s -> NV%d = %s", e.id, e.nv_in, data.hex())
            if e.nv_out is None:
                self._publish_state(e, data)             # no feedback NV: optimistic
            e.next_poll = time.monotonic() + 0.3         # confirm through the feedback NV
        except LonError as err:
            log.error("%s: write failed: %s", e.id, err)
            self._fail(e, f"write failed: {err}")

    def _connect_link(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                self.link.open()
                self._resolve()
                return True
            except (OSError, LonError) as err:
                log.error("Cannot open %s: %s (retry in %ds)", self.link.port, err, backoff)
                self.link.close()
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        return False

    def run(self):
        self.mqtt_start()
        if not self._connect_link():
            return
        while not self.stop.is_set():
            if not self.link.connected:
                if self.client:
                    self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
                self.link.close()
                if not self._connect_link():
                    break
                if self.client:
                    self.client.publish(self.avail_topic, "online", qos=1, retain=True)
            now = time.monotonic()
            pollable = [] if self.paused.is_set() else \
                [e for e in self.entities if e.next_poll != float("inf")]
            due = min((e.next_poll for e in pollable), default=now + 1)
            try:
                job = self.jobs.get(timeout=max(0.0, min(due - now, 1.0)))
            except queue.Empty:
                job = None
            if job:
                kind, e, arg = job
                if kind == "write":
                    self._write(e, arg)
                elif kind == "poll_soon":
                    e.next_poll = min(e.next_poll, time.monotonic() + arg)
                continue
            if self.paused.is_set():                         # paused while waiting
                continue
            now = time.monotonic()
            for e in sorted(pollable, key=lambda x: x.next_poll):
                if e.next_poll <= now:
                    if e.node.retry_at > now:               # node is backing off
                        e.next_poll = e.node.retry_at
                        continue
                    self._poll(e)
                    e.next_poll = (now + e.poll_interval) if e.poll_interval > 0 else float("inf")
                    break                                    # re-check the job queue
        self.shutdown()

    def shutdown(self):
        if self.client:
            try:
                self.client.publish(self.avail_topic, "offline", qos=1, retain=True).wait_for_publish(2)
            except (RuntimeError, ValueError):
                pass
            self.client.loop_stop()
            self.client.disconnect()
        self.link.close()


# ============================================================================
# Configuration
# ============================================================================

def load_config(path, strict=True):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("configuration must be a YAML mapping")
    for key in ("lonworks", "nodes"):
        if key not in cfg and strict:
            raise ValueError(f"configuration is missing '{key}'")
    cfg.setdefault("lonworks", {})
    cfg["nodes"] = cfg.get("nodes") or []
    return cfg


def load_nodes(cfg, base_topic="lon2mqtt"):
    nodes, entities, seen = [], [], set()
    for n in cfg.get("nodes") or []:
        nid = n.get("neuron_id")
        if not nid:
            raise ValueError(f"node {n.get('name')} has no neuron_id")
        node = LonNode(slug(n.get("id", n.get("name", nid))), n.get("name", str(nid)), nid,
                       n.get("subnet"), n.get("node"))
        nodes.append(node)
        for ec in n.get("entities") or []:
            e = Entity(node, ec, base_topic)
            if e.id in seen:
                raise ValueError(f"duplicate entity id '{e.id}'")
            seen.add(e.id)
            entities.append(e)
    return nodes, entities


def find_node(nodes, key):
    for n in nodes:
        if key in (n.id, n.name) or slug(key) == n.id:
            return n
    try:
        return LonNode(key, key, key)            # ad-hoc Neuron ID
    except ValueError:
        sys.exit(f"Unknown node '{key}' (use a configured node id/name or a 12-digit Neuron ID)")


# ============================================================================
# CLI tools
# ============================================================================

def cli_scan(link, node):
    print(f"{'idx':>4}  {'dir':<4} {'selector':<8} {'bound':<5} {'auth':<4} {'len':>3}  value")
    idx = 0
    while idx < 4096:
        try:
            info = link.query_nv_config(node, idx)
        except LonTimeout:
            print(f"{idx:>4}  (no response)")
            idx += 1
            continue
        except LonError:
            print(f"-- NV{idx} rejected: end of NV table ({idx} NVs)")
            break
        try:
            val = link.nv_fetch(node, idx)
        except LonError:
            val = b""
        print(f"{idx:>4}  {'out' if info.is_output else 'in':<4} 0x{info.selector:04x}   "
              f"{'yes' if info.bound else '':<5} {'yes' if info.auth else '':<4} {len(val):>3}  {val.hex()}")
        idx += 1
    if node.subnet is not None:
        print(f"\n{node.name}: subnet/node {node.subnet}/{node.node}, Neuron ID {node.nid_hex}")


def cli_decode(codec_name, raw):
    if codec_name and codec_name in CODECS and CODECS[codec_name].name != "raw":
        return f"{raw.hex()}  =>  {CODECS[codec_name].decode(raw)}"
    return raw.hex()


def cli_sniff(link, stop):
    def show(p):
        sel = nv_update_in_packet(p)
        extra = f" NV-update sel=0x{sel:04x}" if sel is not None else ""
        print(f"{time.strftime('%H:%M:%S')} {p['src'][0]}/{p['src'][1]} -> {p['dst']} "
              f"fmt={p['pdufmt']} pdu={p['pdu'].hex()}{extra}{'' if p['crc_ok'] else ' CRC!'}")
    link.on_packet.append(show)
    print("Sniffing (Ctrl-C to stop)...")
    stop.wait()


def main(argv=None):
    ap = argparse.ArgumentParser(description="LonWorks <-> MQTT bridge (U61 serial interfaces)")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "scan", "read", "write", "sniff"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--type", help="SNVT type used to decode/encode values in read/write")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * a.verbose if a.command != "run"
                        else logging.INFO - 10 * min(a.verbose, 1),
                        format="%(asctime)s %(levelname)-7s %(message)s")

    cfg = load_config(a.config)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    if a.command == "run":
        bridge = Bridge(cfg)
        bridge.stop = stop
        bridge.run()
        return 0

    link = LonLink(cfg["lonworks"])
    nodes, _ = load_nodes(cfg)
    link.open()
    try:
        if a.command == "sniff":
            cli_sniff(link, stop)
            return 0
        if not a.args:
            ap.error(f"{a.command} needs a node")
        node = find_node(nodes, a.args[0])
        if a.command == "scan":
            cli_scan(link, node)
        elif a.command == "read":
            idx = int(a.args[1])
            print(cli_decode(a.type, link.nv_fetch(node, idx)))
        elif a.command == "write":
            idx, text = int(a.args[1]), a.args[2]
            codec = CODECS.get(a.type or "raw")
            if codec is None:
                ap.error(f"unknown --type {a.type}")
            data = codec.encode(codec.parse(text))
            info = link.nv_info(node, idx)
            print(f"{info} <- {data.hex()}")
            link.nv_update(node, idx, data)
            print("ACK; read back:", cli_decode(a.type, link.nv_fetch(node, idx)))
    except LonError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
