#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
lonutil - LonWorks node diagnostics over a layer-2 U61 interface.

Covers the read-mostly part of what Echelon's NodeUtil does: find devices, listen for
service-pin messages, query status and statistics, show the domain, address, NV
configuration and alias tables, list network variables with their SNVT types
(self-identification data), read memory, poll and update NVs, wink, and change the
application mode (online / offline / reset).

It deliberately does NOT write configuration (domain, address table, NV config,
memory): those operations can take a commissioned network apart.

Library use:
    from lon2mqtt import LonLink, LonNode
    from lonutil import NodeTools
    link = LonLink({...}); link.open()
    tools = NodeTools(link)
    tools.status(LonNode("n", "n", "021234567890"))

Command line:
    lonutil.py -c config.yaml find
    lonutil.py -c config.yaml pins
    lonutil.py -c config.yaml status|info|domains|addresses|nvs|aliases <node>
    lonutil.py -c config.yaml wink|online|offline|reset|clear <node>
    lonutil.py -c config.yaml mem <node> <abs|ro|cfg|stat> <offset> <count>

Message formats follow ISO/IEC 14908-1 as implemented by EnOcean's open-source
lon-stack-dx (lcs_netmgmt.c, lon_types.h).
"""

import argparse
import json
import sys
import threading
import time

from lon2mqtt import (CODECS, PDU_APDU, LonError, LonLink, LonNode, LonRejected, LonTimeout,
                      load_config, load_nodes, nv_index_bytes)

# --- network management / diagnostic message codes --------------------------
ND_QUERY_STATUS = 0x51
ND_CLEAR_STATUS = 0x53
NM_QUERY_ID = 0x61
NM_RESPOND_TO_QUERY = 0x62
NM_QUERY_ADDR = 0x67
NM_QUERY_NV_CONFIG = 0x68
NM_QUERY_DOMAIN = 0x6A
NM_SET_NODE_MODE = 0x6C
NM_READ_MEMORY = 0x6D
NM_WINK = 0x70
NM_QUERY_SNVT = 0x72
NM_SERVICE_PIN = 0x7F

QUERY_ID_UNCONFIGURED, QUERY_ID_SELECTED = 0, 1
MEM_MODES = {"abs": 0, "ro": 1, "cfg": 2, "stat": 3}
NODE_MODES = {"offline": 0, "online": 1, "reset": 2}

# --- lookup tables ----------------------------------------------------------
NODE_STATES = {
    2: "unconfigured (has application)",
    3: "applicationless",
    4: "configured, online",
    6: "configured, hard-offline",
    0x0C: "configured, soft-offline",
    0x8C: "configured, bypass-offline",
}

ERROR_LOG = {
    0: "no error", 129: "bad event", 130: "NV length mismatch", 131: "NV message too short",
    132: "EEPROM write fail", 133: "bad address type", 134: "preemption mode timeout",
    135: "already preempted", 136: "sync NV update lost", 137: "invalid response allocation",
    138: "invalid domain", 139: "read past end of message", 140: "write past end of message",
    141: "invalid address table index", 142: "incomplete message", 143: "NV update on output NV",
    144: "no message available", 145: "illegal send", 146: "unknown PDU", 147: "invalid NV index",
    148: "divide by zero", 149: "invalid application error", 150: "memory allocation failure",
    151: "write past end of network buffer", 152: "application checksum error",
    153: "configuration checksum error", 154: "invalid transceiver register address",
    155: "transceiver register timeout", 156: "write past end of application buffer",
    157: "I/O ready", 158: "self-test failed", 159: "subnet router",
    160: "authentication mismatch", 161: "self-installation semaphore set",
    162: "read/write semaphore set", 163: "application signature bad",
    164: "router firmware version mismatch",
}

BUFFER_SIZE = [255, 20, 20, 21, 22, 24, 26, 30, 34, 42, 50, 66, 82, 114, 146, 210]
BUFFER_COUNT = [0, 1, 1, 2, 3, 5, 7, 11, 15, 23, 31, 47, 63, 95, 127, 191]
REPEAT_TIMER_MS = [16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072]
RECEIVE_TIMER_MS = [128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192,
                    12288, 16384, 24576]
SERVICE_TYPES = ["ackd", "unackd-repeated", "unackd", "request"]

# SNVT type indexes (LonMark SNVT Master List). Unknown indexes are shown by number.
SNVT_NAMES = dict(enumerate("""
amp amp_mil angle angle_vel btu_kilo btu_mega char_ascii count count_inc date_cal date_day
date_time elec_kwh elec_whr flow flow_mil length length_kilo length_micr length_mil lev_cont
lev_disc mass mass_kilo mass_mega mass_mil power power_kilo ppm press res res_kilo sound_db
speed speed_mil str_asc str_int telcom temp time_passed vol vol_kilo vol_mil volt volt_dbmv
volt_kilo volt_mil amp_f angle_f angle_vel_f count_f count_inc_f flow_f length_f lev_cont_f
mass_f power_f ppm_f press_f res_f sound_db_f speed_f temp_f time_f vol_f volt_f btu_f
elec_whr_f config_src color grammage grammage_f file_req file_status freq_f freq_hz freq_kilohz
freq_milhz lux ISO_7811 lev_percent multiplier state time_stamp zerospan magcard elapsed_tm
alarm currency file_pos muldiv obj_request obj_status preset switch trans_table override
pwr_fact pwr_fact_f density density_f rpm hvac_emerg angle_deg temp_p temp_setpt time_sec
hvac_mode occupancy area hvac_overid hvac_status press_p address scene scene_cfg setting
evap_state therm_mode defr_mode defr_term defr_state time_min time_hour ph ph_f chlr_status
tod_event smo_obscur fire_test temp_ror fire_init fire_indcte time_zone earth_pos reg_val
reg_val_ts volt_ac amp_ac
""".split(), start=1))
SNVT_NAMES.update({143: "turbidity", 144: "turbidity_f", 145: "hvac_type", 146: "elec_kwh_l",
                   147: "temp_diff_p", 148: "ctrl_req", 149: "ctrl_resp", 150: "ptz",
                   151: "privacyzone", 152: "pos_ctrl", 153: "enthalpy", 154: "gfci_status",
                   155: "motor_state", 156: "pumpset_mn", 157: "ex_control", 158: "pumpset_sn",
                   159: "pump_sensor", 160: "abs_humid", 161: "flow_p", 162: "dev_c_mode",
                   163: "valve_mode", 164: "alarm_2", 165: "state_64", 166: "nv_type"})
SNVT_NAMES = {k: "SNVT_" + v for k, v in SNVT_NAMES.items()}


def snvt_name(index):
    if index == 0:
        return None                         # not a standard type
    return SNVT_NAMES.get(index, f"SNVT #{index}")


def _ascii(b):
    t = bytes(b).rstrip(b"\x00")
    return t.decode("ascii") if t and all(32 <= c < 127 for c in t) else None


def _cstring(blob, pos):
    end = blob.find(b"\x00", pos)
    if end < 0:
        end = len(blob)
    return blob[pos:end].decode("latin-1"), end + 1


def decode_program_id(pid):
    out = {"hex": pid.hex()}
    if pid[0] >> 4 in (8, 9):               # LonMark standard program ID
        out.update(format=pid[0] >> 4,
                   manufacturer=((pid[0] & 0x0F) << 16) | (pid[1] << 8) | pid[2],
                   device_class=(pid[3] << 8) | pid[4], usage=pid[5],
                   channel_type=pid[6], model=pid[7])
    else:
        out["text"] = _ascii(pid)
    return out


def service_pin_from_packet(p):
    """Decode a service-pin broadcast (APDU 0x7F + Neuron ID + program ID), else None."""
    pdu = p["pdu"]
    if p["pdufmt"] != PDU_APDU or len(pdu) < 15 or pdu[0] != NM_SERVICE_PIN:
        return None
    return {"neuron_id": pdu[1:7].hex(), "program_id": decode_program_id(pdu[7:15]),
            "subnet": p["src"][0], "node": p["src"][1]}


def decode_state(state):
    return NODE_STATES.get(state, NODE_STATES.get(state & 0x07, f"invalid (0x{state:02x})"))


def decode_reset_cause(rc):
    if rc == 0:
        return "cleared"
    if rc & 0x01:
        return "power-up"
    if rc & 0x03 == 0x02:
        return "external"
    if rc & 0x0F == 0x0C:
        return "watchdog"
    if rc & 0x1F == 0x14:
        return "software"
    return f"unknown (0x{rc:02x})"


def decode_nv_config(cfg, index):
    sel = ((cfg[0] & 0x3F) << 8) | cfg[1]
    b2 = cfg[2] if len(cfg) > 2 else 0x0F
    return {"index": index, "raw": cfg.hex(), "direction": "out" if cfg[0] & 0x40 else "in",
            "priority": bool(cfg[0] & 0x80), "selector": sel, "bound": sel < 0x3000,
            "turnaround": bool(b2 & 0x80), "service": SERVICE_TYPES[(b2 >> 5) & 3],
            "auth": bool(b2 & 0x10), "addr_index": (b2 & 0x0F) if (b2 & 0x0F) != 15 else None}


def decode_address(entry, index):
    t, b1, b2, b3, b4 = entry[:5]
    out = {"index": index, "raw": entry.hex(), "retries": b2 & 0x0F,
           "repeat_timer_ms": REPEAT_TIMER_MS[b2 >> 4], "tx_timer_ms": REPEAT_TIMER_MS[b3 & 0x0F]}
    if t & 0x80:
        out.update(type="group", group=b4, size=t & 0x7F, member=b1 & 0x7F, domain=b1 >> 7,
                   receive_timer_ms=RECEIVE_TIMER_MS[b3 >> 4])
    elif t == 1:
        out.update(type="subnet/node", subnet=b4, node=b1 & 0x7F, domain=b1 >> 7)
    elif t == 3:
        out.update(type="broadcast", subnet=b4, backlog=b1 & 0x3F, domain=b1 >> 7)
    elif t == 0 and b1 == 1:
        out.update(type="turnaround")
    elif t == 0:
        return {"index": index, "raw": entry.hex(), "type": "unused"}
    else:
        out.update(type=f"unknown ({t})")
    return out


MESSAGE_NAMES = {
    0x51: "Query status", 0x52: "Proxy", 0x53: "Clear status", 0x54: "Query transceiver status",
    0x61: "Query ID", 0x62: "Respond to query", 0x63: "Update domain", 0x64: "Leave domain",
    0x65: "Update key", 0x66: "Update address", 0x67: "Query address", 0x68: "Query NV config",
    0x69: "Update group address", 0x6A: "Query domain", 0x6B: "Update NV config",
    0x6C: "Set node mode", 0x6D: "Read memory", 0x6E: "Write memory",
    0x6F: "Checksum recalculate", 0x70: "Wink", 0x71: "Memory refresh", 0x72: "Query SNVT",
    0x73: "NV fetch", 0x7F: "Service pin",
}
TPDU_TYPES = {0: "ackd", 1: "unackd-repeated", 2: "ack", 3: "reminder", 4: "reminder+msg"}
SPDU_TYPES = {0: "request", 2: "response", 4: "reminder", 5: "reminder+msg"}


class PacketDescriber:
    """Turns parsed LPDUs into one-line descriptions; pairs responses with their requests."""

    def __init__(self):
        self._requests = {}                 # (requester, tid) -> request code

    @staticmethod
    def _addr(a):
        if a is None:
            return "?"
        kind, v = a
        if kind == "node":
            return f"{v[0]}/{v[1]}"
        if kind == "nid":
            return v.hex()
        if kind == "broadcast":
            return "all" if v == 0 else f"subnet {v}"
        return f"group {v}"

    def _apdu(self, apdu):
        if not apdu:
            return ""
        c = apdu[0]
        if c & 0x80:
            sel = ((c & 0x3F) << 8) | (apdu[1] if len(apdu) > 1 else 0)
            what = "NV poll" if c & 0x40 else "NV update"
            return f"{what}, selector 0x{sel:04x}" + (f" = {apdu[2:].hex()}" if len(apdu) > 2 else "")
        name = MESSAGE_NAMES.get(c)
        if name:
            return name + (f" {apdu[1:].hex()}" if len(apdu) > 1 else "")
        return f"application message 0x{c:02x}" + (f" {apdu[1:].hex()}" if len(apdu) > 1 else "")

    def describe(self, direction, p):
        pdu, fmt = p["pdu"], p["pdufmt"]
        src = f"{p['src'][0]}/{p['src'][1]}"
        out = {"dir": direction, "src": src, "dst": self._addr(p["dst"]),
               "domain": p["domain"].hex(), "hex": p["raw"].hex(), "crc_ok": p["crc_ok"]}
        tid = pdu[0] & 0x0F
        if fmt == PDU_APDU:
            out.update(type="unackd", info=self._apdu(pdu))
        elif fmt == 0:                      # TPDU
            t = (pdu[0] >> 4) & 7
            out.update(type=TPDU_TYPES.get(t, f"tpdu {t}"), tid=tid,
                       info=self._apdu(pdu[1:]) if t in (0, 1) else "")
            if pdu[0] & 0x80:
                out["info"] += " (authenticated)"
        elif fmt == 1:                      # SPDU
            t = (pdu[0] >> 4) & 7
            out.update(type=SPDU_TYPES.get(t, f"spdu {t}"), tid=tid, info="")
            if t == 0 and len(pdu) > 1:
                self._requests[(src, tid)] = pdu[1]
                if len(self._requests) > 256:
                    self._requests.pop(next(iter(self._requests)))
                out["info"] = self._apdu(pdu[1:])
            elif t == 2 and len(pdu) > 1:
                req, code, data = self._requests.get((out["dst"], tid)), pdu[1], pdu[2:].hex()
                if req is None:
                    out["info"] = f"code 0x{code:02x} {data}".rstrip()
                elif code == (req & 0x1F) | 0x20:
                    out["info"] = f"{MESSAGE_NAMES.get(req, 'request')}: {data or 'ok'}"
                elif code == req & 0x1F:
                    out["info"] = f"{MESSAGE_NAMES.get(req, 'request')}: rejected"
                else:
                    out["info"] = f"code 0x{code:02x} {data}".rstrip()
        else:                               # authentication
            out.update(type="challenge" if (pdu[0] >> 4) & 3 == 0 else "reply", tid=tid,
                       info=pdu[1:].hex())
        return out


class NodeTools:
    def __init__(self, link):
        self.link = link

    # ------------------------------------------------------------ discovery
    def find_devices(self, window=0.8, max_rounds=24, progress=None):
        """
        Find every device that answers on the configured domain.

        Unconfigured devices are asked with Query ID (unconfigured). Configured devices
        are found the way installation tools do it: set "respond to query" on all of them,
        then repeat Query ID (selected), switching each responder off, until nobody answers.
        The flag lives in RAM and is cleared again at the end.
        """
        link, found = self.link, {}

        def take(replies, unconfigured):
            new = []
            for src, r in replies:
                if len(r) < 15 or r[0] != (NM_QUERY_ID & 0x1F) | 0x20:
                    continue
                nid = r[1:7].hex()
                if nid not in found:
                    found[nid] = {"neuron_id": nid, "program_id": decode_program_id(r[7:15]),
                                  "subnet": src[0], "node": src[1], "unconfigured": unconfigured}
                    new.append(nid)
                    if progress:
                        progress(len(found), None)
            return new

        for _ in range(2):
            take(link.broadcast_request(NM_QUERY_ID, bytes([QUERY_ID_UNCONFIGURED]), window), True)
        everyone = ("broadcast", 0)
        link.send_unackd(everyone, bytes([NM_RESPOND_TO_QUERY, 1]), repeat=2)
        try:
            quiet = 0
            for _ in range(max_rounds):
                new = take(link.broadcast_request(NM_QUERY_ID, bytes([QUERY_ID_SELECTED]), window),
                           False)
                for nid in new:
                    try:
                        link.nm_request(LonNode(nid, nid, nid), NM_RESPOND_TO_QUERY, b"\x00")
                    except LonError:
                        pass                  # it will be silenced by the final broadcast
                quiet = 0 if new else quiet + 1
                if quiet >= 2:
                    break
        finally:
            link.send_unackd(everyone, bytes([NM_RESPOND_TO_QUERY, 0]), repeat=2)
        return sorted(found.values(), key=lambda d: (d["subnet"], d["node"], d["neuron_id"]))

    # ------------------------------------------------------------ status / mode
    def status(self, node):
        d = self.link.nm_request(node, ND_QUERY_STATUS, b"")
        if len(d) < 15:
            raise LonError(f"short status response: {d.hex()}")
        w = [(d[i] << 8) | d[i + 1] for i in range(0, 10, 2)]
        return {"neuron_id": node.nid_hex, "subnet": node.subnet, "node": node.node,
                "transmit_errors": w[0], "transaction_timeouts": w[1],
                "receive_transaction_full": w[2], "lost_messages": w[3], "missed_messages": w[4],
                "reset_cause": decode_reset_cause(d[10]), "reset_cause_raw": d[10],
                "state": decode_state(d[11]), "state_raw": d[11],
                "firmware_version": d[12],
                "error_log": ERROR_LOG.get(d[13], f"application error {d[13]}" if d[13] < 128
                                           else f"system error {d[13]}"),
                "error_log_raw": d[13], "model_number": d[14]}

    def clear_status(self, node):
        self.link.nm_request(node, ND_CLEAR_STATUS, b"")
        return {"cleared": True}

    def wink(self, node):
        self.link.nm_ackd(node, NM_WINK)
        return {"wink": "sent"}

    def set_mode(self, node, mode):
        if mode not in NODE_MODES:
            raise ValueError(f"mode must be one of {sorted(NODE_MODES)}")
        try:
            self.link.nm_ackd(node, NM_SET_NODE_MODE, bytes([NODE_MODES[mode]]))
            ack = True
        except LonTimeout:
            if mode != "reset":             # a resetting node may not get its ACK out
                raise
            ack = False
        return {"mode": mode, "acknowledged": ack}

    # ------------------------------------------------------------ memory
    def read_memory(self, node, mode, offset, count):
        """Read `count` bytes; the block size adapts to what the node's buffers allow."""
        mode = MEM_MODES.get(mode, mode)
        if mode not in (0, 1, 2, 3) or not 0 <= offset <= 0xFFFF or not 0 < count <= 4096:
            raise ValueError("bad memory range")
        chunk, out = getattr(node, "mem_chunk", 32), b""
        while len(out) < count:
            n, at = min(chunk, count - len(out)), offset + len(out)
            try:
                d = self.link.nm_request(node, NM_READ_MEMORY, bytes([mode, at >> 8, at & 0xFF, n]))
            except LonRejected:
                if chunk <= 8:
                    raise
                chunk //= 2
                continue
            if len(d) != n:
                raise LonError(f"read memory returned {len(d)} bytes, expected {n}")
            out += d
        node.mem_chunk = chunk
        return out

    def read_only_data(self, node):
        d = self.read_memory(node, 1, 0, 41)
        snvt = (d[11] << 8) | d[12]
        return {
            "neuron_id": d[0:6].hex(), "model_number": d[6], "minor_model_number": d[7] & 0x0F,
            "read_write_protect": bool(d[10] & 0x80), "run_when_unconfigured": bool(d[10] & 0x40),
            "nv_count": d[10] & 0x3F, "snvt_pointer": snvt,
            "program_id": decode_program_id(d[13:21]),
            "two_domains": bool(d[21] & 0x40), "explicit_messages": bool(d[21] & 0x08),
            "state": decode_state(d[21] & 0x07), "state_raw": d[21] & 0x07,
            "address_count": d[22] >> 4, "receive_transaction_count": (d[23] & 0x0F) + 1,
            "alias_count": d[36] & 0x3F, "message_tag_count": d[37] >> 4,
            "buffers": {
                "app_out": {"size": BUFFER_SIZE[d[24] >> 4], "count": BUFFER_COUNT[d[27] >> 4],
                            "priority_count": BUFFER_COUNT[d[26] & 0x0F]},
                "app_in": {"size": BUFFER_SIZE[d[24] & 0x0F], "count": BUFFER_COUNT[d[27] & 0x0F]},
                "net_out": {"size": BUFFER_SIZE[d[25] >> 4], "count": BUFFER_COUNT[d[28] >> 4],
                            "priority_count": BUFFER_COUNT[d[26] >> 4]},
                "net_in": {"size": BUFFER_SIZE[d[25] & 0x0F], "count": BUFFER_COUNT[d[28] & 0x0F]},
            },
            "raw": d.hex(),
        }

    def config_data(self, node):
        d = self.read_memory(node, 2, 0, 25)
        return {"channel_id": (d[0] << 8) | d[1], "location": _ascii(d[2:8]) or d[2:8].hex(),
                "comm_clock": d[8] >> 3, "input_clock": d[8] & 7, "comm_type": d[9] >> 5,
                "preamble_length": d[10], "packet_cycle": d[11], "beta2_control": d[12],
                "transmit_interpacket": d[13], "receive_interpacket": d[14],
                "node_priority": d[15], "channel_priorities": d[16],
                "transceiver_parameters": d[17:24].hex(),
                "non_group_timer_ms": RECEIVE_TIMER_MS[d[24] >> 4],
                "nm_authentication": bool(d[24] & 0x08), "preempt_timeout": d[24] & 7,
                "raw": d.hex()}

    def info(self, node):
        """Application configuration: read-only structure, configuration structure, status."""
        out = {"status": self.status(node), "read_only": self.read_only_data(node)}
        try:
            out["config"] = self.config_data(node)
        except LonError as e:
            out["config_error"] = str(e)
        return out

    # ------------------------------------------------------------ tables
    def domain_table(self, node, count=None):
        if count is None:
            try:
                count = 2 if self.read_only_data(node)["two_domains"] else 1
            except LonError:
                count = 2
        rows = []
        for i in range(count):
            try:
                d = self.link.nm_request(node, NM_QUERY_DOMAIN, bytes([i]))
            except LonRejected:
                break
            if len(d) < 15:
                raise LonError(f"short domain entry: {d.hex()}")
            length = d[8] & 0x07
            used = d[8] != 0xFF and not d[8] & 0x80 and length in (0, 1, 3, 6)
            rows.append({"index": i, "used": used, "raw": d.hex(),
                         "domain_id": d[:length].hex() if used else None,
                         "length": length if used else None,
                         "subnet": d[6] if used else None, "node": d[7] & 0x7F if used else None,
                         "clone": not d[7] & 0x80 if used else None,
                         "key": d[9:15].hex(), "default_key": d[9:15] == b"\xff" * 6})
        return rows

    def address_table(self, node, count=None, progress=None):
        if count is None:
            try:
                count = self.read_only_data(node)["address_count"]
            except LonError:
                count = 15
        rows = []
        for i in range(count):
            try:
                d = self.link.nm_request(node, NM_QUERY_ADDR, bytes([i]))
            except LonRejected:
                break
            if len(d) < 5:
                raise LonError(f"short address entry: {d.hex()}")
            rows.append(decode_address(d, i))
            if progress:
                progress(i + 1, count)
        return rows

    # ------------------------------------------------------------ self-identification
    def si_data(self, node, snvt_pointer=None):
        """
        Self-identification data: SNVT type, name and self-documentation of each NV.
        Returns {"version", "nvs": [...], "self_doc"}; nvs is indexed like the NV table.
        """
        if snvt_pointer is None:
            snvt_pointer = self.read_only_data(node)["snvt_pointer"]
        if snvt_pointer == 0:
            return {"version": None, "nvs": [], "self_doc": None,
                    "note": "device has no self-identification data"}
        if snvt_pointer == 0xFFFF:              # host-based device: Query SNVT
            def read(off, n):
                out = b""
                while len(out) < n:
                    k, at = min(16, n - len(out)), off + len(out)
                    d = self.link.nm_request(node, NM_QUERY_SNVT, bytes([at >> 8, at & 0xFF, k]))
                    if len(d) != k:
                        raise LonError("short SI data response")
                    out += d
                return out
        else:
            def read(off, n):
                return self.read_memory(node, 0, snvt_pointer + off, n)

        hdr = read(0, 6)
        length, count, version = (hdr[0] << 8) | hdr[1], hdr[2], hdr[3]
        if version == 0:
            hdr_len = 5
        elif version == 1:
            hdr_len, count = 6, count | (hdr[4] << 8)
        else:
            return {"version": version, "nvs": [], "self_doc": None,
                    "note": f"self-identification version {version} is not decoded"}
        if not hdr_len + 2 * count <= length <= 16384:
            raise LonError(f"implausible SI data header: {hdr.hex()}")
        blob = hdr + read(6, length - 6)
        pos = hdr_len
        descs = [(blob[pos + 2 * i], blob[pos + 2 * i + 1]) for i in range(count)]
        pos += 2 * count
        self_doc, pos = _cstring(blob, pos)
        nvs = []
        for flags, snvt in descs:
            rec = {"snvt_index": snvt, "snvt": snvt_name(snvt), "name": None, "self_doc": None,
                   "polled": bool(flags & 0x20), "sync": bool(flags & 0x40),
                   "config_class": bool(flags & 0x01)}
            dim = 1
            if flags & 0x80 and pos < len(blob):        # extension record
                ext = blob[pos]
                pos += 1
                if ext & 0x80:
                    rec["max_rate"], pos = blob[pos], pos + 1
                if ext & 0x40:
                    rec["rate"], pos = blob[pos], pos + 1
                if ext & 0x20:
                    rec["name"], pos = _cstring(blob, pos)
                if ext & 0x10:
                    rec["self_doc"], pos = _cstring(blob, pos)
                if ext & 0x08:
                    dim, pos = (blob[pos] << 8) | blob[pos + 1], pos + 2
            if dim > 1:
                for k in range(dim):
                    nvs.append(dict(rec, name=f"{rec['name'] or 'nv'}[{k}]"))
            else:
                nvs.append(rec)
        for i, rec in enumerate(nvs):
            rec["index"] = i
        return {"version": version, "nvs": nvs, "self_doc": self_doc or None}

    def nv_table(self, node, values=True, progress=None):
        """NV configuration table joined with SI data and (optionally) current values."""
        ro = si = None
        notes = []
        try:
            ro = self.read_only_data(node)
            si = self.si_data(node, ro["snvt_pointer"])
            if si.get("note"):
                notes.append(si["note"])
        except LonError as e:
            notes.append(f"self-identification data not available: {e}")
        count = len(si["nvs"]) if si and si["nvs"] else None
        if count is None and ro and 0 < ro["nv_count"] < 63:
            count = ro["nv_count"]              # keeps the scan out of the alias table
        rows, idx, misses = [], 0, 0
        while idx < (count if count is not None else 4096):
            try:
                cfg = self.link.nm_request(node, NM_QUERY_NV_CONFIG, nv_index_bytes(idx))
                misses = 0
            except LonRejected:
                break
            except LonTimeout:
                misses += 1
                if misses >= 2:
                    notes.append(f"device stopped answering at NV {idx}")
                    break
                continue
            row = decode_nv_config(cfg, idx)
            if si and idx < len(si["nvs"]):
                s = si["nvs"][idx]
                row.update(snvt_index=s["snvt_index"], snvt=s["snvt"], name=s["name"],
                           self_doc=s["self_doc"], polled=s["polled"])
            if values:
                try:
                    v = self.link.nv_fetch(node, idx)
                    row.update(length=len(v), value=v.hex(), decoded=self.decode(row.get("snvt"), v))
                except LonError as e:
                    row["value_error"] = str(e)
            rows.append(row)
            idx += 1
            if progress:
                progress(idx, count)
        return {"nvs": rows, "self_doc": si["self_doc"] if si else None,
                "si_version": si["version"] if si else None, "notes": notes}

    def alias_table(self, node):
        ro = self.read_only_data(node)
        si = self.si_data(node, ro["snvt_pointer"])
        base = len(si["nvs"]) or ro["nv_count"]
        rows = []
        for i in range(ro["alias_count"]):
            try:
                d = self.link.nm_request(node, NM_QUERY_NV_CONFIG, nv_index_bytes(base + i))
            except LonRejected:
                break
            row = decode_nv_config(d, i)
            primary = d[3] if len(d) > 3 else 0xFF
            row.update(primary=None if primary == 0xFF else primary,
                       used=primary != 0xFF)
            rows.append(row)
        return rows

    # ------------------------------------------------------------ NV access
    @staticmethod
    def decode(snvt, raw):
        codec = CODECS.get(snvt or "")
        if codec is None or codec.name == "raw" or (codec.size and codec.size != len(raw)):
            return None
        try:
            return codec.decode(raw)
        except Exception:
            return None

    def poll_nv(self, node, index, snvt=None):
        v = self.link.nv_fetch(node, index)
        return {"index": index, "length": len(v), "value": v.hex(), "decoded": self.decode(snvt, v)}

    def update_nv(self, node, index, value, snvt=None):
        """value: hex string, or text understood by the SNVT codec when `snvt` is given."""
        codec = CODECS.get(snvt or "raw") or CODECS["raw"]
        data = codec.encode(codec.parse(str(value)))
        node.nv_info.pop(index, None)           # always use the node's current selector
        self.link.nv_update(node, index, data)
        out = self.poll_nv(node, index, snvt)
        out["written"] = data.hex()
        return out


# ============================================================================
# Command line
# ============================================================================

def _print(obj, indent=0):
    pad = "  " * indent
    if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
        cols = []
        for r in obj:
            cols += [k for k in r if k not in cols and not isinstance(r[k], (dict, list))]
        cols = [c for c in cols if c not in ("raw", "self_doc")]
        cells = [[("" if r.get(c) is None else f"0x{r[c]:04x}" if c == "selector" else str(r.get(c)))
                  for c in cols] for r in obj]
        width = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
        print(pad + "  ".join(c.ljust(w) for c, w in zip(cols, width)))
        for row in cells:
            print(pad + "  ".join(v.ljust(w) for v, w in zip(row, width)))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _print(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    else:
        print(pad + str(obj))


def main(argv=None):
    ap = argparse.ArgumentParser(description="LonWorks node diagnostics (U61 serial interfaces)")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("command", choices=["find", "pins", "status", "info", "domains", "addresses",
                                        "nvs", "aliases", "wink", "online", "offline", "reset",
                                        "clear", "mem"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    cfg = load_config(a.config, strict=False)
    nodes, _ = load_nodes(cfg)
    link = LonLink(cfg["lonworks"])
    link.open()
    tools = NodeTools(link)

    def node():
        if not a.args:
            ap.error(f"{a.command} needs a node (configured id/name or 12-digit Neuron ID)")
        key = a.args[0]
        for n in nodes:
            if key in (n.id, n.name, n.nid_hex):
                return n
        try:
            return LonNode(key, key, key)
        except ValueError:
            sys.exit(f"Unknown node '{key}'")

    try:
        if a.command == "pins":
            def show(p):
                pin = service_pin_from_packet(p)
                if pin:
                    print(time.strftime("%H:%M:%S"), json.dumps(pin))
            link.on_packet.append(show)
            print("Waiting for service-pin messages (Ctrl-C to stop)...")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                return 0
        result = {
            "find": lambda: tools.find_devices(),
            "status": lambda: tools.status(node()),
            "info": lambda: tools.info(node()),
            "domains": lambda: tools.domain_table(node()),
            "addresses": lambda: tools.address_table(node()),
            "nvs": lambda: tools.nv_table(node()),
            "aliases": lambda: tools.alias_table(node()),
            "wink": lambda: tools.wink(node()),
            "clear": lambda: tools.clear_status(node()),
            "online": lambda: tools.set_mode(node(), "online"),
            "offline": lambda: tools.set_mode(node(), "offline"),
            "reset": lambda: tools.set_mode(node(), "reset"),
            "mem": lambda: {"data": tools.read_memory(node(), a.args[1], int(a.args[2], 0),
                                                      int(a.args[3], 0)).hex()},
        }[a.command]()
        if a.json:
            print(json.dumps(result, indent=2))
        else:
            _print(result)
    except (LonError, ValueError, IndexError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
