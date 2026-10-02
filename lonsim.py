#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
lonsim - a simulated U61 interface with a few LON devices behind it, on a pseudo-terminal.

Used by the test suite and by `lonweb.py --demo`, so the bridge and the web interface can
be tried without hardware. It speaks the same UMIP framing as the real interface and
answers the network-management messages lon2mqtt and lonutil send.

It models behaviour, not timing: there is no collision, no CSMA and no retry logic.
"""

import os
import select
import threading
import time

import lon2mqtt as L


def _u16(v):
    return bytes([v >> 8, v & 0xFF])


class SimNV:
    def __init__(self, name, snvt, direction, value, selector=None, auth=False, feeds=None):
        self.name, self.snvt, self.direction, self.value = name, snvt, direction, bytes(value)
        self.selector, self.auth, self.feeds = selector, auth, feeds


class SimNode:
    SI_BASE = 0xF200

    def __init__(self, name, neuron_id, subnet, node, program_id, nvs, aliases=0,
                 configured=True, nm_auth=False, self_doc="", addresses=(), max_read=48):
        self.name = name
        self.nid = bytes.fromhex(neuron_id)
        self.subnet, self.node = (subnet, node) if configured else (0, 0)
        self.program_id = program_id.encode().ljust(8, b"\x00")[:8]
        self.nvs, self.aliases = nvs, aliases
        self.configured, self.nm_auth, self.self_doc = configured, nm_auth, self_doc
        self.addresses = list(addresses)
        self.max_read = max_read            # largest read-memory block the buffers allow
        self.online = True
        self.silent = False                 # True = powered off: answers nothing
        self.respond_to_query = False
        self.stats = [0, 0, 0, 0, 0]
        self.reset_cause, self.error_log = 0x01, 0
        self.winks = 0
        for i, nv in enumerate(nvs):
            if nv.selector is None:
                nv.selector = 0x3FFF - i

    # ---- memory images
    def read_only(self):
        d = bytearray(41)
        d[0:6] = self.nid
        d[6], d[7] = 0x10, 0x01
        d[8:10] = _u16(0xF000)
        d[10] = min(len(self.nvs), 62)
        d[11:13] = _u16(self.SI_BASE)
        d[13:21] = self.program_id
        d[21] = 0x08 | (4 if self.configured else 2)
        d[22] = 0xF0
        d[23] = 0x07
        d[24], d[25], d[26], d[27], d[28] = 0xBB, 0xBB, 0x22, 0x33, 0x33
        d[36] = self.aliases
        d[37] = 0x00
        return bytes(d)

    def config_data(self):
        d = bytearray(25)
        d[2:8] = b"SIM\x00\x00\x00"
        d[8], d[9], d[10], d[11] = (5 << 3) | 5, 0x60, 4, 0x3F
        d[24] = (5 << 4) | (0x08 if self.nm_auth else 0) | 3
        return bytes(d)

    def si_data(self):
        descs, ext = b"", b""
        for nv in self.nvs:
            descs += bytes([0x80, nv.snvt])
            ext += bytes([0x20]) + nv.name.encode() + b"\x00"
        body = descs + self.self_doc.encode() + b"\x00" + ext
        n = len(self.nvs)
        return _u16(6 + len(body)) + bytes([n & 0xFF, 1, n >> 8, 0]) + body

    def memory(self, mode, offset, count):
        if mode == 1:
            img, base = self.read_only(), 0
        elif mode == 2:
            img, base = self.config_data(), 0
        elif mode == 0:
            img, base = self.si_data(), self.SI_BASE
        else:
            return None
        if offset < base or offset - base + count > len(img) or count > self.max_read:
            return None
        return img[offset - base:offset - base + count]

    def status(self):
        state = (4 if self.online else 0x0C) if self.configured else 2
        return b"".join(_u16(s) for s in self.stats) + bytes(
            [self.reset_cause, state, 21, self.error_log, 0x10])

    def nv_config(self, idx):
        if idx < len(self.nvs):
            nv = self.nvs[idx]
            b0 = (0x40 if nv.direction == "out" else 0) | (nv.selector >> 8)
            bound = nv.selector < 0x3000
            b2 = (0x10 if nv.auth else 0) | (0 if bound and nv.direction == "out" else 0x0F)
            return bytes([b0, nv.selector & 0xFF, b2])
        if idx < len(self.nvs) + self.aliases:
            return bytes([0x7F, 0xFF, 0x0F, 0xFF])
        return None

    def domain(self, idx):
        if idx != 0:
            return None
        if not self.configured:
            return bytes(8) + b"\xff" + b"\xff" * 6
        dom = self.sim.domain
        return dom.ljust(6, b"\x00") + bytes([self.subnet, 0x80 | self.node, len(dom)]) + b"\xff" * 6

    def address(self, idx):
        if idx >= 15:
            return None
        return self.addresses[idx] if idx < len(self.addresses) else bytes(5)

    def apply_update(self, selector, value):
        for nv in self.nvs:
            if nv.direction == "in" and nv.selector == selector:
                nv.value = bytes(value)
                if nv.feeds is not None:
                    self.nvs[nv.feeds].value = bytes(value)
                return nv
        return None


class SimU61(threading.Thread):
    def __init__(self, nodes, domain="0a"):
        super().__init__(daemon=True, name="lonsim")
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        self._slave = slave                 # keep open so the pty survives client reconnects
        self.domain = bytes.fromhex(domain)
        self.nodes = nodes
        for n in nodes:
            n.sim = self
        self.dec = L.UmipDecoder()
        self.stopping = False
        self.sent = []                      # parsed LPDUs received from the host

    # ---- uplink helpers
    def uplink(self, npdu):
        body = bytes([0x01]) + npdu
        c = L.crc16_lontalk(body)
        os.write(self.master, L.umip_encode(L.NI_L2_INCOMING, body + bytes([c >> 8, c & 0xFF])))

    def reply(self, node, pdufmt, pdu, to, domain):
        hdr = (pdufmt << 4) | (2 << 2) | L.DOMAIN_LEN_CODE[len(domain)]
        self.uplink(bytes([hdr, node.subnet, 0x80 | node.node, to[0], 0x80 | to[1]]) + domain + pdu)

    def press_service_pin(self, node):
        hdr = (L.PDU_APDU << 4) | 0            # broadcast, zero-length domain
        self.uplink(bytes([hdr, node.subnet, 0x80 | node.node, 0x00, 0x7F]) + node.nid + node.program_id)

    def inject_update(self, src, dest_node, selector, value):
        """Another device on the bus sends a bound NV update to dest_node."""
        hdr = (L.PDU_TPDU << 4) | (2 << 2) | L.DOMAIN_LEN_CODE[len(self.domain)]
        tpdu = bytes([0x03, 0x80 | (selector >> 8), selector & 0xFF]) + bytes(value)
        self.uplink(bytes([hdr, src[0], 0x80 | src[1], dest_node.subnet, 0x80 | dest_node.node])
                    + self.domain + tpdu)
        dest_node.apply_update(selector, value)

    # ---- downlink handling
    def run(self):
        while not self.stopping:
            r, _, _ = select.select([self.master], [], [], 0.2)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                time.sleep(0.05)
                continue
            for cmd, data in self.dec.feed(chunk):
                try:
                    self.handle(cmd, data)
                except Exception as e:      # pragma: no cover
                    print("lonsim error:", e)

    def stop(self):
        self.stopping = True

    def handle(self, cmd, data):
        if cmd == L.NI_LAYER_MODE:
            os.write(self.master, L.umip_encode(L.NI_LAYER_MODE, data))
            return
        if cmd != L.NI_L2_SEND:
            return
        c = L.crc16_lontalk(data)
        p = L.parse_lpdu(data + bytes([c >> 8, c & 0xFF]))
        if not p:
            return
        self.sent.append(p)
        kind, dst = p["dst"]
        for n in self.nodes:
            if n.silent:
                continue
            if kind == "nid" and dst != n.nid:
                continue
            if kind == "node" and (not n.configured or dst != (n.subnet, n.node)):
                continue
            if kind == "group":
                continue
            if kind != "nid" and n.configured and p["domain"] != self.domain:
                continue
            self.deliver(n, p, broadcast=kind == "broadcast")

    def deliver(self, n, p, broadcast):
        pdu, src, dom = p["pdu"], p["src"], p["domain"]
        tid = pdu[0] & 0x0F
        if p["pdufmt"] == L.PDU_APDU:
            self.command(n, pdu)
            return
        if p["pdufmt"] == L.PDU_TPDU and (pdu[0] >> 4) & 7 == L.TPDU_ACKD:
            apdu = pdu[1:]
            if apdu[0] & 0x80:
                sel = ((apdu[0] & 0x3F) << 8) | apdu[1]
                target = next((v for v in n.nvs if v.direction == "in" and v.selector == sel), None)
                if target is not None and target.auth:
                    self.reply(n, L.PDU_AUTH, bytes([tid]) + os.urandom(8), src, dom)
                    return
                if n.online:
                    n.apply_update(sel, apdu[2:])
            else:
                self.command(n, apdu)
            self.reply(n, L.PDU_TPDU, bytes([(L.TPDU_ACK << 4) | tid]), src, dom)
            return
        if p["pdufmt"] != L.PDU_SPDU or (pdu[0] >> 4) & 7 != L.SPDU_REQUEST:
            return
        code, data = pdu[1], pdu[2:]
        resp = self.request(n, code, data)
        if resp is None:                    # no answer at all (e.g. not selected)
            return
        ok, body = resp
        rc = ((code & 0x1F) | 0x20) if ok else (code & 0x1F)
        self.reply(n, L.PDU_SPDU, bytes([(L.SPDU_RESPONSE << 4) | tid, rc]) + body, src, dom)

    def command(self, n, apdu):
        code = apdu[0]
        if code == 0x62 and len(apdu) > 1:
            n.respond_to_query = bool(apdu[1])
        elif code == 0x70:
            n.winks += 1
        elif code == 0x6C and len(apdu) > 1:
            if apdu[1] == 0:
                n.online = False
            elif apdu[1] == 1:
                n.online = True
            elif apdu[1] == 2:
                n.online, n.reset_cause = True, 0x14

    def request(self, n, code, data):
        fail = (False, b"")
        if n.nm_auth and code not in (0x61, 0x62) and code >= 0x60:
            return fail
        if code == 0x51:
            return True, n.status()
        if code == 0x53:
            n.stats, n.reset_cause, n.error_log = [0] * 5, 0, 0
            return True, b""
        if code == 0x61:
            sel = data[0] if data else 0
            if sel == 0 and n.configured:
                return None
            if sel == 1 and not n.respond_to_query:
                return None
            if sel == 2 and (n.configured or not n.respond_to_query):
                return None
            return True, n.nid + n.program_id
        if code == 0x62:
            n.respond_to_query = bool(data[0])
            return True, b""
        if code == 0x67:
            a = n.address(data[0])
            return (True, a) if a is not None else fail
        if code == 0x68:
            cfg = n.nv_config(data[0])
            return (True, cfg) if cfg is not None else fail
        if code == 0x6A:
            d = n.domain(data[0])
            return (True, d) if d is not None else fail
        if code == 0x6D:
            m = n.memory(data[0], (data[1] << 8) | data[2], data[3])
            return (True, m) if m is not None else fail
        if code == 0x73:
            idx = data[0]
            if idx < len(n.nvs):
                nv = n.nvs[idx]
                value = nv.value if n.online else bytes(len(nv.value))
                return True, bytes([idx]) + value
            if idx < len(n.nvs) + n.aliases:
                return True, bytes([idx])
            return fail
        return fail


# ============================================================================
# A small demo installation
# ============================================================================

SW, LUX, TEMP_P, COUNT = 95, 79, 105, 8


def demo_nodes():
    dimmer = SimNode("Lighting module", "02a1b2c3d400", 1, 12, "DIM2CH", [
        SimNV("nvoPresence", SW, "out", [0, 0]),
        SimNV("nvoLamp1", SW, "out", [0xC8, 1]),
        SimNV("nvoLamp2", SW, "out", [0, 0]),
        SimNV("nvoRunHours", COUNT, "out", [0x01, 0x2C]),
        SimNV("nviLamp1", SW, "in", [0xC8, 1], selector=0x0115, feeds=1),
        SimNV("nviLamp2", SW, "in", [0, 0], selector=0x0116, feeds=2),
        SimNV("nviLocked", SW, "in", [0, 0], auth=True),
    ], aliases=3, self_doc="&3.2@0,3200",
        addresses=[bytes([1, 0x09, 0x23, 0x04, 1]), bytes([0x83, 0x01, 0x34, 0x54, 7])])
    weather = SimNode("Weather station", "02c0ffee0101", 1, 20, "WEATHR", [
        SimNV("nvoLux", LUX, "out", (843).to_bytes(2, "big")),
        SimNV("nvoOutdoorTemp", TEMP_P, "out", (1875).to_bytes(2, "big")),
        SimNV("nviLuxThreshold", LUX, "in", (200).to_bytes(2, "big")),
    ], self_doc="&3.2@0,1", max_read=16)
    garden = SimNode("Garden module", "02decade0202", 1, 31, "GARDEN", [
        SimNV("nvoDusk", SW, "out", [0, 1]),
        SimNV("nviGardenLight", SW, "in", [0, 0]),
    ])
    spare = SimNode("Spare module", "02ffee001122", 0, 0, "DIM2CH", [
        SimNV("nvoLamp1", SW, "out", [0, 0]), SimNV("nviLamp1", SW, "in", [0, 0], feeds=0),
    ], configured=False)
    return [dimmer, weather, garden, spare]


def demo_config(port):
    return {
        "mqtt": {"broker": "", "base_topic": "lon2mqtt", "discovery_prefix": "homeassistant"},
        "lonworks": {"serial_port": port, "domain_id": "0a", "source_subnet": 1,
                     "source_node": 127, "timeout": 0.5, "retries": 2},
        "nodes": [
            {"id": "lighting", "name": "Lighting module", "neuron_id": "02a1b2c3d400", "entities": [
                {"id": "lamp_1", "name": "Lamp 1", "type": "light", "snvt_type": "SNVT_switch",
                 "nv_in": 4, "nv_out": 1, "polling_interval": 15},
                {"id": "lamp_2", "name": "Lamp 2", "type": "light", "snvt_type": "SNVT_switch",
                 "nv_in": 5, "nv_out": 2, "polling_interval": 15},
                {"id": "presence", "name": "Presence", "type": "binary_sensor",
                 "snvt_type": "SNVT_switch", "device_class": "motion", "nv_out": 0,
                 "polling_interval": 3},
            ]},
            {"id": "weather", "name": "Weather station", "neuron_id": "02c0ffee0101", "entities": [
                {"id": "illuminance", "name": "Outdoor illuminance", "type": "sensor",
                 "snvt_type": "SNVT_lux", "nv_out": 0, "polling_interval": 20},
                {"id": "outdoor_temp", "name": "Outdoor temperature", "type": "sensor",
                 "snvt_type": "SNVT_temp_p", "nv_out": 1, "polling_interval": 20},
                {"id": "lux_threshold", "name": "Dusk threshold", "type": "number",
                 "snvt_type": "SNVT_lux", "nv_in": 2, "min": 0, "max": 2000, "step": 10,
                 "polling_interval": 60},
            ]},
            {"id": "garden", "name": "Garden module", "neuron_id": "02decade0202", "entities": [
                {"id": "garden_light", "name": "Garden light", "type": "switch",
                 "snvt_type": "SNVT_switch", "nv_in": 1, "polling_interval": 30},
                {"id": "dusk", "name": "Dusk sensor", "type": "binary_sensor",
                 "snvt_type": "SNVT_switch", "device_class": "light", "nv_out": 0,
                 "polling_interval": 30},
            ]},
        ],
    }


def start_demo(activity=True):
    sim = SimU61(demo_nodes())
    sim.start()
    if activity:
        threading.Thread(target=_activity, args=(sim,), daemon=True, name="lonsim-activity").start()
    return sim


def _activity(sim):
    """Make the demo move: presence, daylight, and a wall switch bound to lamp 2."""
    import math
    dimmer, weather = sim.nodes[0], sim.nodes[1]
    t0, step = time.time(), 0
    while not sim.stopping:
        time.sleep(4)
        step += 1
        t = time.time() - t0
        lux = int(800 + 500 * math.sin(t / 60))
        weather.nvs[0].value = lux.to_bytes(2, "big")
        weather.nvs[1].value = int(1850 + 150 * math.sin(t / 90)).to_bytes(2, "big")
        dimmer.nvs[0].value = bytes([0, 1 if step % 5 < 2 else 0])
        if step % 6 == 0:
            level = [0, 60, 120, 200][(step // 6) % 4]
            sim.inject_update((1, 9), dimmer, 0x0116, [level, 1 if level else 0])
