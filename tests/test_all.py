#!/usr/bin/env python3
"""
End-to-end tests against the simulated interface (lonsim) on a pseudo-terminal.
Run: python3 tests/test_all.py
"""
import base64
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lon2mqtt as L  # noqa: E402
import lonsim  # noqa: E402
import lonutil as U  # noqa: E402
import lonweb  # noqa: E402
import yaml  # noqa: E402

OK = True


def check(cond, msg):
    global OK
    print(("PASS " if cond else "FAIL ") + msg)
    OK &= bool(cond)


def raises(exc, fn, msg):
    try:
        fn()
        check(False, msg)
    except exc:
        check(True, msg)


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


def wait(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ============================================================================
def test_link_and_tools():
    print("\n== link and node tools")
    sim = lonsim.start_demo(activity=False)
    dimmer, weather, garden, spare = sim.nodes
    cfg = lonsim.demo_config(sim.path)
    link = L.LonLink(cfg["lonworks"])
    link.open()
    tools = U.NodeTools(link)
    node = L.LonNode("dim", "Dimmer", "02:a1:b2:c3:d4:00")

    esc = L.umip_encode(0x12, bytes([0x7E, 1, 0x7E]))
    check(L.UmipDecoder().feed(esc) == [(0x12, bytes([0x7E, 1, 0x7E]))], "0x7E escaping round-trip")
    big = bytes(range(256)) * 2
    check(L.UmipDecoder().feed(L.umip_encode(0x1A, big)) == [(0x1A, big)], "extended-length frame")

    found = tools.find_devices(window=0.3)
    ids = {d["neuron_id"]: d for d in found}
    check(len(found) == 4, f"find devices -> {len(found)}")
    check(ids["02a1b2c3d400"]["subnet"] == 1 and ids["02a1b2c3d400"]["node"] == 12, "address of found device")
    check(ids["02ffee001122"]["unconfigured"] and not ids["02c0ffee0101"]["unconfigured"], "unconfigured flag")
    check(ids["02a1b2c3d400"]["program_id"]["text"] == "DIM2CH", "program ID text")
    check(not any(n.respond_to_query for n in sim.nodes), "respond-to-query cleared afterwards")

    st = tools.status(node)
    check(st["state"] == "configured, online" and st["reset_cause"] == "power-up", f"status {st['state']}")
    check((node.subnet, node.node) == (1, 12), "subnet/node learnt from response")

    info = tools.info(node)
    ro = info["read_only"]
    check(ro["nv_count"] == 7 and ro["alias_count"] == 3 and ro["address_count"] == 15, "read-only counts")
    check(ro["buffers"]["net_out"] == {"size": 66, "count": 2, "priority_count": 1}, f"buffers {ro['buffers']['net_out']}")
    check(info["config"]["nm_authentication"] is False and info["config"]["location"] == "SIM", "config data")

    dom = tools.domain_table(node)
    check(len(dom) == 1 and dom[0]["domain_id"] == "0a" and dom[0]["node"] == 12 and dom[0]["default_key"], "domain table")
    adr = tools.address_table(node)
    check(adr[0]["type"] == "subnet/node" and adr[0]["node"] == 9 and adr[0]["subnet"] == 1
          and adr[0]["retries"] == 3 and adr[0]["repeat_timer_ms"] == 32, f"address entry 0 {adr[0]}")
    check(adr[1]["type"] == "group" and adr[1]["group"] == 7 and adr[1]["size"] == 3 and adr[1]["member"] == 1
          and adr[1]["receive_timer_ms"] == 768, "address entry 1 (group)")
    check(adr[2]["type"] == "unused" and len(adr) == 15, "unused address entries")

    t = tools.nv_table(node)
    nvs = t["nvs"]
    check(len(nvs) == 7 and t["self_doc"] == "&3.2@0,3200" and t["si_version"] == 1, "NV table size and self-doc")
    check(nvs[4]["name"] == "nviLamp1" and nvs[4]["snvt"] == "SNVT_switch" and nvs[4]["direction"] == "in"
          and nvs[4]["selector"] == 0x0115 and nvs[4]["bound"], "NV 4 config and SI data")
    check(nvs[1]["decoded"] == {"value": 100.0, "state": 1} and nvs[1]["value"] == "c801", "NV value decoded")
    check(nvs[3]["snvt"] == "SNVT_count" and nvs[3]["decoded"] == 300, "SNVT_count")
    check(nvs[6]["auth"] is True, "authenticated NV flagged")
    al = tools.alias_table(node)
    check(len(al) == 3 and not al[0]["used"], "alias table")

    wnode = L.LonNode("w", "Weather", "02c0ffee0101")
    wt = tools.nv_table(wnode)
    check(wnode.mem_chunk == 16 and len(wt["nvs"]) == 3, "read-memory block size adapts to small buffers")
    check(wt["nvs"][1]["decoded"] == 18.75 and wt["nvs"][0]["decoded"] == 843, "SNVT_temp_p / SNVT_lux decode")

    tools.wink(node)
    check(dimmer.winks == 1, "wink delivered")
    tools.set_mode(node, "offline")
    check(tools.status(node)["state"] == "configured, soft-offline", "set offline")
    tools.set_mode(node, "online")
    check(tools.status(node)["state"] == "configured, online", "set online")
    tools.set_mode(node, "reset")
    check(tools.status(node)["reset_cause"] == "software", "reset")
    tools.clear_status(node)
    check(tools.status(node)["reset_cause"] == "cleared", "clear status")

    r = tools.update_nv(node, 4, "75:on", "SNVT_switch")
    check(r["written"] == "9601" and tools.poll_nv(node, 1, "SNVT_switch")["decoded"] == {"value": 75.0, "state": 1},
          "update NV 4 -> feedback NV 1")
    raises(L.LonAuthRequired, lambda: tools.update_nv(node, 6, "0000"), "authenticated NV refuses update")
    raises(L.LonError, lambda: tools.update_nv(node, 1, "0000"), "update to an output refused")
    raises(L.LonRejected, lambda: tools.read_memory(node, "stat", 0, 8), "rejected read raises LonRejected")
    check(tools.read_memory(node, "ro", 0, 6) == bytes.fromhex("02a1b2c3d400"), "read memory")

    pins = []
    link.on_packet.append(lambda p: pins.append(U.service_pin_from_packet(p)))
    sim.press_service_pin(spare)
    wait(lambda: any(pins))
    pin = next((p for p in pins if p), None)
    check(pin and pin["neuron_id"] == "02ffee001122" and pin["program_id"]["text"] == "DIM2CH", "service pin decoded")

    snode = L.LonNode("s", "Spare", "02ffee001122")
    check(tools.status(snode)["state"].startswith("unconfigured") and snode.subnet is None,
          "unconfigured device answers by Neuron ID")

    garden.silent = True
    raises(L.LonTimeout, lambda: tools.status(L.LonNode("g", "Garden", "02decade0202")), "silent device times out")

    d = U.PacketDescriber()
    seen = []
    link.on_traffic.append(lambda direction, p: seen.append(d.describe(direction, p)))
    link.nv_fetch(node, 1)
    check(seen[0]["dir"] == "tx" and seen[0]["info"].startswith("NV fetch") and seen[0]["dst"] == "02a1b2c3d400",
          f"monitor describes request: {seen[0]['info']}")
    check(seen[1]["dir"] == "rx" and seen[1]["info"].startswith("NV fetch: 01") and seen[1]["src"] == "1/12",
          f"monitor pairs response: {seen[1]['info']}")
    link.close()
    sim.stop()


# ============================================================================
def test_bridge():
    print("\n== bridge")
    sim = lonsim.start_demo(activity=False)
    dimmer, weather, garden, spare = sim.nodes
    garden.silent = True
    b = L.Bridge(lonsim.demo_config(sim.path))
    b.client = FakeMqtt()
    b.mqtt_start = lambda: None
    changes = []
    b.on_state.append(lambda e: changes.append(e.id))
    t = threading.Thread(target=b.run, daemon=True)
    t.start()
    ent = {e.id: e for e in b.entities}
    check(wait(lambda: ent["lamp_1"].state and ent["illuminance"].state and ent["lux_threshold"].state, 8),
          "first readings arrive despite a silent node")
    b._on_connect(b.client, None, None, 0)
    check(ent["lamp_1"].state == {"brightness": 200, "raw": "c801", "state": "ON", "switch_state": 1, "value": 100.0},
          f"lamp_1 state {ent['lamp_1'].state}")
    check(ent["illuminance"].state["value"] == 843 and ent["outdoor_temp"].state["value"] == 18.75, "sensor values")
    gnode = next(n for n in b.nodes if n.id == "garden")
    check(wait(lambda: gnode.fail_count > 0 and ent["garden_light"].error), "silent node backs off and reports an error")
    disc = {tp: json.loads(pl) for tp, pl in b.client.pub if tp.startswith("homeassistant/")}
    check(disc["homeassistant/light/lighting/lamp_1/config"]["brightness_scale"] == 200, "discovery published")

    b.command("lamp_1", json.dumps({"state": "ON", "brightness": 50}))
    check(wait(lambda: ent["lamp_1"].state.get("brightness") == 50) and dimmer.nvs[4].value == bytes([50, 1]),
          "command -> NV update -> feedback state")
    b.command("lamp_1", '{"state":"OFF"}')
    check(wait(lambda: ent["lamp_1"].state["state"] == "OFF"), "lamp off")
    b.command("lamp_1", '{"state":"ON"}')
    check(wait(lambda: dimmer.nvs[4].value == bytes([50, 1])), "ON restores the last level")
    raises(ValueError, lambda: b.command("illuminance", "1"), "read-only entity rejects commands")

    sim.inject_update((1, 9), dimmer, 0x0116, [0x40, 1])
    check(wait(lambda: (ent["lamp_2"].state or {}).get("brightness") == 0x40), "bound update on the bus triggers a refresh")

    b.paused.set()
    weather.nvs[0].value = (10).to_bytes(2, "big")
    ent["illuminance"].next_poll = 0
    time.sleep(1.0)
    check(ent["illuminance"].state["value"] == 843, "paused bridge does not poll")
    b.paused.clear()
    check(wait(lambda: ent["illuminance"].state["value"] == 10), "resumed bridge polls again")

    garden.silent = False
    gnode.retry_at = 0
    for e in b.entities:
        if e.node is gnode:
            e.next_poll = 0
    check(wait(lambda: ent["garden_light"].state and not ent["garden_light"].error and not gnode.unresolved, 6),
          "node that comes back is resolved and read")
    check("lamp_1" in changes and "garden_light" in changes, "state listeners notified")
    b.stop.set()
    t.join(5)
    sim.stop()


# ============================================================================
def http(base, path, body=None, headers=None, raw=False):
    h = {"X-Requested-With": "lon2mqtt", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return r.status, data if raw else json.loads(data)
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except ValueError:
            return e.code, {}


def test_web():
    print("\n== web interface")
    sim = lonsim.start_demo(activity=False)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.yaml")
    cfg = lonsim.demo_config(sim.path)
    cfg["mqtt"]["password"] = "s3cret"
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f)
    app = lonweb.App(path)
    logging.getLogger().addHandler(lonweb.HubLogHandler(app))
    app.load()
    app.start()
    httpd = lonweb.make_server(app, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    code, page = http(base, "/", raw=True)
    check(code == 200 and b"<title>lon2mqtt</title>" in page, "index page served")
    check(wait(lambda: http(base, "/api/state")[1]["nodes"][0]["entities"][0]["state"], 8), "state has live readings")
    code, st = http(base, "/api/state")
    check(st["link"]["connected"] and not st["mqtt"]["enabled"] and len(st["nodes"]) == 3, "state summary")
    check(st["nodes"][0]["subnet"] == 1 and st["nodes"][0]["node"] == 12, "node address in state")

    code, _ = http(base, "/api/tool", {"tool": "find"}, headers={"X-Requested-With": ""})
    check(code == 403, "POST without the custom header is refused")
    code, r = http(base, "/api/tool", {"tool": "find"})
    check(code == 200 and len(r["result"]) == 4, "tool: find")
    code, r = http(base, "/api/tool", {"tool": "nvs", "node": "lighting"})
    check(code == 200 and r["result"]["nvs"][4]["name"] == "nviLamp1", "tool: nvs by node id")
    code, r = http(base, "/api/tool", {"tool": "status", "node": "02ffee001122"})
    check(code == 200 and r["result"]["state"].startswith("unconfigured"), "tool: status by Neuron ID")
    code, r = http(base, "/api/tool", {"tool": "update", "node": "lighting", "args": {"index": 5, "value": "30:on", "snvt": "SNVT_switch"}})
    check(code == 200 and r["result"]["written"] == "3c01", "tool: update NV")
    code, r = http(base, "/api/tool", {"tool": "memory", "node": "lighting", "args": {"mode": "ro", "offset": "0x0", "count": 6}})
    check(code == 200 and r["result"]["data"] == "02a1b2c3d400", "tool: read memory")
    code, r = http(base, "/api/tool", {"tool": "status", "node": "xyz"})
    check(code == 400, "bad node -> 400")
    code, r = http(base, "/api/tool", {"tool": "update", "node": "lighting", "args": {"index": 6, "value": "0000"}})
    check(code == 502 and "authentication" in r["error"], "LON errors -> 502 with message")

    code, r = http(base, "/api/entity", {"id": "lamp_1", "payload": {"state": "ON", "brightness": 80}})
    check(code == 200 and wait(lambda: sim.nodes[0].nvs[4].value == bytes([80, 1])), "entity command from the UI")
    sim.press_service_pin(sim.nodes[3])
    check(wait(lambda: http(base, "/api/state")[1]["pins"]), "service pin shows up in state")
    code, hist = http(base, "/api/history")
    check(hist["packets"] and hist["logs"] and "info" in hist["packets"][0], "history for monitor and log")

    code, c = http(base, "/api/config")
    conf = c["config"]
    check(conf["mqtt"]["password"] == lonweb.MASK and "SNVT_switch" in c["snvt_types"], "password masked in config")
    bad = json.loads(json.dumps(conf))
    bad["nodes"][0]["neuron_id"] = "1234"
    code, r = http(base, "/api/config", {"config": bad})
    check(code == 400 and "Neuron ID" in r["error"], f"invalid config refused: {r.get('error')}")
    bad = json.loads(json.dumps(conf))
    bad["lonworks"]["source_node"] = "200"
    code, r = http(base, "/api/config", {"config": bad})
    check(code == 400, "invalid bridge address refused")
    with open(path) as f:
        check(yaml.safe_load(f)["nodes"][0]["neuron_id"] == "02a1b2c3d400", "file untouched after refused save")

    new = json.loads(json.dumps(conf))
    new["lonworks"]["timeout"] = "0.4"
    new["nodes"][2]["entities"].append({"id": "", "name": "Extra reading", "type": "sensor", "snvt_type": "raw",
                                        "nv_in": "", "nv_out": "0", "polling_interval": "5"})
    new["web"] = {"host": "127.0.0.1", "port": "8099", "username": "admin", "password": "pw"}
    code, r = http(base, "/api/config", {"config": new})
    check(code == 200 and r["saved"], "valid config saved")
    with open(path) as f:
        saved = yaml.safe_load(f)
    check(saved["mqtt"]["password"] == "s3cret", "stored password kept when the browser sent the mask")
    check(saved["lonworks"]["timeout"] == 0.4 and saved["nodes"][2]["entities"][-1] ==
          {"id": "extra_reading", "name": "Extra reading", "type": "sensor", "snvt_type": "raw", "nv_out": 0,
           "polling_interval": 5}, f"values normalised: {saved['nodes'][2]['entities'][-1]}")
    check(os.path.exists(path + ".bak") and oct(os.stat(path).st_mode & 0o777) == "0o600", "backup written, file is private")

    code, _ = http(base, "/api/state")
    check(code == 401, "password now required")
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    check(wait(lambda: http(base, "/api/state", headers=auth)[1].get("nodes") and
               any(e["id"] == "extra_reading" and e["state"] for e in http(base, "/api/state", headers=auth)[1]["nodes"][2]["entities"]), 8),
          "bridge restarted with the new entity")
    code, _ = http(base, "/api/bridge", {"action": "pause"}, headers=auth)
    check(code == 200 and http(base, "/api/state", headers=auth)[1]["bridge"]["paused"], "pause polling")

    httpd.shutdown()
    app.stop()
    sim.stop()


if __name__ == "__main__":
    console = logging.StreamHandler()
    console.setLevel(logging.CRITICAL)
    logging.basicConfig(level=logging.INFO, handlers=[console])
    test_link_and_tools()
    test_bridge()
    test_web()
    print("\nALL PASSED" if OK else "\nSOME TESTS FAILED")
    sys.exit(0 if OK else 1)
