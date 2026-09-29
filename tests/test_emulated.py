#!/usr/bin/env python3
"""
End-to-end test with an emulated U61 interface + LON node on a pseudo-terminal.
Run: python3 tests/test_emulated.py
"""
import json
import os
import select
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lon2mqtt as L  # noqa: E402

DOMAIN = bytes.fromhex("0a")
NID = bytes.fromhex("021234567890")
ME = (1, 42)


class FakeNode:
    """Dimmer: NV51 (in, bound sel 0x0428) drives NV29 (out). NV19 SNVT_lux out. NV28 lux in (auth)."""
    def __init__(self):
        self.nv = {  # idx: [is_out, selector, auth, value]
            29: [True, 0x3FFF - 29, False, bytes([0, 0])],
            51: [False, 0x0428, False, bytes([0xC8, 1])],
            19: [True, 0x3FFF - 19, False, (345).to_bytes(2, "big")],
            28: [False, 0x3FFF - 28, True, (100).to_bytes(2, "big")],
            26: [True, 0x3FFF - 26, False, bytes([0, 1])],
        }
        self.count = 67

    def cfg(self, idx):
        o, sel, auth, _ = self.nv.get(idx, [True, 0x3FFF - idx, False, b""])
        return bytes([(0x40 if o else 0) | (sel >> 8), sel & 0xFF, 0x0F | (0x10 if auth else 0)])

    def apply_update(self, sel, val):
        for idx, (o, s, a, v) in self.nv.items():
            if not o and s == sel:
                self.nv[idx][3] = val
                if idx == 51:
                    self.nv[29][3] = val            # feedback


class FakeU61(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        self.dec = L.UmipDecoder()
        self.node = FakeNode()
        self.stop = False
        self.rx_log = []

    def send_l2(self, npdu):
        body = bytes([0x01]) + npdu
        c = L.crc16_lontalk(body)
        os.write(self.master, L.umip_encode(L.NI_L2_INCOMING, body + bytes([c >> 8, c & 0xFF])))

    def reply(self, pdufmt, pdu, to=(1, 127)):
        hdr = (pdufmt << 4) | (2 << 2) | 1
        self.send_l2(bytes([hdr, ME[0], 0x80 | ME[1], to[0], 0x80 | to[1]]) + DOMAIN + pdu)

    def inject_bound_update(self, sel, val):
        """Another device (wall switch 1/9) sends a bound NV update to our dimmer."""
        hdr = (L.PDU_TPDU << 4) | (2 << 2) | 1
        tpdu = bytes([0x03, 0x80 | (sel >> 8), sel & 0xFF]) + val
        self.send_l2(bytes([hdr, 1, 0x89, ME[0], 0x80 | ME[1]]) + DOMAIN + tpdu)
        self.node.apply_update(sel, val)

    def handle(self, cmd, data):
        if cmd == L.NI_LAYER_MODE:
            os.write(self.master, L.umip_encode(L.NI_LAYER_MODE, data))
            return
        assert cmd == L.NI_L2_SEND, hex(cmd)
        c = L.crc16_lontalk(data)
        p = L.parse_lpdu(data + bytes([c >> 8, c & 0xFF]))
        self.rx_log.append(p)
        dst = p["dst"]
        if dst[0] == "nid" and dst[1] != NID or dst[0] == "node" and dst[1] != ME:
            return
        pdu, tid = p["pdu"], p["pdu"][0] & 0x0F
        if p["pdufmt"] == L.PDU_SPDU:
            code, idx = pdu[1], pdu[2]
            if idx >= self.node.count:
                self.reply(L.PDU_SPDU, bytes([0x20 | tid, code & 0x1F]))
            elif code == 0x68:
                self.reply(L.PDU_SPDU, bytes([0x20 | tid, 0x28]) + self.node.cfg(idx))
            elif code == 0x73:
                v = self.node.nv.get(idx, [0, 0, 0, bytes(2)])[3]
                self.reply(L.PDU_SPDU, bytes([0x20 | tid, 0x33, idx]) + v)
        elif p["pdufmt"] == L.PDU_TPDU:
            sel = ((pdu[1] & 0x3F) << 8) | pdu[2]
            if pdu[0] & 0x80:                                 # authenticated -> challenge
                self.reply(L.PDU_AUTH, bytes([tid]) + os.urandom(8))
                return
            auth_required = any(not o and s == sel and a for o, s, a, _ in self.node.nv.values())
            if auth_required:
                self.reply(L.PDU_AUTH, bytes([tid]) + os.urandom(8))
                return
            self.node.apply_update(sel, pdu[3:])
            self.reply(L.PDU_TPDU, bytes([0x20 | tid]))

    def run(self):
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.2)
            if r:
                for cmd, data in self.dec.feed(os.read(self.master, 1024)):
                    self.handle(cmd, data)


class FakeMqtt:
    def __init__(self):
        self.pub = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pub.append((topic, payload))

        class R:
            def wait_for_publish(self, t):
                pass
        return R()

    def subscribe(self, *a, **k):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def cfg(port):
    return {
        "mqtt": {"base_topic": "lon2mqtt"},
        "lonworks": {"serial_port": port, "domain_id": "0a", "source_subnet": 1, "source_node": 127,
                     "timeout": 0.5, "retries": 2},
        "nodes": [{"id": "mod1", "name": "Module 1", "neuron_id": "02:12:34:56:78:90", "entities": [
            {"id": "dimmer", "name": "Dimmer", "type": "light", "snvt_type": "SNVT_switch",
             "nv_in": 51, "nv_out": 29, "polling_interval": 60},
            {"id": "lux", "name": "Lux", "type": "sensor", "snvt_type": "SNVT_lux",
             "nv_out": 19, "polling_interval": 60},
            {"id": "threshold", "name": "Thr", "type": "number", "snvt_type": "SNVT_lux",
             "nv_in": 28, "polling_interval": 0},
            {"id": "pir", "name": "PIR", "type": "binary_sensor", "snvt_type": "SNVT_switch",
             "device_class": "motion", "nv_out": 26, "polling_interval": 60},
        ]}]}


def main():
    fake = FakeU61()
    fake.start()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= bool(cond)

    # ---- low-level link
    link = L.LonLink(cfg(fake.path)["lonworks"])
    link.open()
    node = L.LonNode("mod1", "Module 1", "021234567890")
    info = link.query_nv_config(node, 51)
    check(info.selector == 0x0428 and not info.is_output and info.bound, f"query NV51 -> {info}")
    check((node.subnet, node.node) == ME, "subnet/node discovered")
    check(link.nv_fetch(node, 29) == bytes(2), "fetch NV29")
    link.nv_update(node, 51, bytes([0x96, 1]))
    check(link.nv_fetch(node, 29) == bytes([0x96, 1]), "update NV51 reflected in NV29")
    try:
        link.nv_update(node, 29, bytes(2))
        check(False, "write to output rejected")
    except L.LonError:
        check(True, "write to output rejected")
    try:
        link.nv_update(node, 28, bytes(2))
        check(False, "auth NV raises LonAuthRequired")
    except L.LonAuthRequired:
        check(True, "auth NV raises LonAuthRequired")
    try:
        link.query_nv_config(node, 70)
        check(False, "out-of-range NV rejected")
    except L.LonError as e:
        check(not isinstance(e, L.LonTimeout), "out-of-range NV rejected (NM failure)")
    esc = L.umip_encode(0x12, bytes([0x7E, 1, 0x7E]))
    check(esc.count(0x7E) == 1 + 4 and L.UmipDecoder().feed(esc) == [(0x12, bytes([0x7E, 1, 0x7E]))],
          "0x7E escaping round-trip")
    link.close()

    # ---- bridge
    fake.node.nv[51][3] = fake.node.nv[29][3] = bytes([0xC8, 1])
    b = L.Bridge(cfg(fake.path))
    b.client = FakeMqtt()
    b.mqtt_start = lambda: None
    t = threading.Thread(target=b.run, daemon=True)
    t.start()
    time.sleep(1.0)
    b._on_connect(b.client, None, None, 0)
    time.sleep(1.5)

    def last_state(eid):
        for topic, payload in reversed(b.client.pub):
            if topic == f"lon2mqtt/{eid}/state":
                return json.loads(payload)

    disc = {tp: json.loads(pl) for tp, pl in b.client.pub if tp.startswith("homeassistant/")}
    check("homeassistant/light/mod1/dimmer/config" in disc, "light discovery published")
    check(disc["homeassistant/light/mod1/dimmer/config"]["brightness_scale"] == 200, "brightness scale 200")
    check(disc["homeassistant/sensor/mod1/lux/config"]["device_class"] == "illuminance", "lux device_class")
    check(last_state("dimmer") == {"brightness": 200, "raw": "c801", "state": "ON",
                                   "switch_state": 1, "value": 100.0}, f"dimmer state {last_state('dimmer')}")
    check(last_state("lux")["value"] == 345, "lux value")
    check(last_state("pir")["state"] == "ON", "pir ON")
    check(last_state("threshold")["value"] == 100, "number read once at start-up")

    class Msg:
        def __init__(s, t, p):
            s.topic, s.payload = t, p.encode()
    b._on_message(b.client, None, Msg("lon2mqtt/dimmer/set", '{"state":"ON","brightness":50}'))
    time.sleep(1.2)
    check(fake.node.nv[51][3] == bytes([50, 1]), "HA brightness 50 -> NV51 = 3201")
    check(last_state("dimmer")["brightness"] == 50, "dimmer state confirmed by NV29")
    b._on_message(b.client, None, Msg("lon2mqtt/dimmer/set", '{"state":"OFF"}'))
    time.sleep(1.2)
    check(last_state("dimmer")["state"] == "OFF", "dimmer OFF")
    b._on_message(b.client, None, Msg("lon2mqtt/dimmer/set", '{"state":"ON"}'))
    time.sleep(1.2)
    check(fake.node.nv[51][3] == bytes([50, 1]), "ON restores last level (25 %)")

    fake.inject_bound_update(0x0428, bytes([0x40, 1]))    # wall switch sets 32 %
    time.sleep(1.2)
    check(last_state("dimmer")["brightness"] == 0x40, "bus NV update triggers refresh")

    b._on_message(b.client, None, Msg("lon2mqtt/threshold/set", "250"))
    time.sleep(1.2)
    check(fake.node.nv[28][3] == (100).to_bytes(2, "big"), "auth-protected write not applied (logged)")

    b.stop.set()
    t.join(3)
    fake.stop = True
    print("\nALL PASSED" if ok else "\nSOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
