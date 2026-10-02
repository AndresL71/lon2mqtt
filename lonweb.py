#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
lonweb - web interface for lon2mqtt.

Runs the LON <-> MQTT bridge and serves a browser UI to:
  * watch and control the configured entities,
  * edit the configuration (serial channel, LON addressing, MQTT, nodes, entities) and
    save it back to the YAML file,
  * run NodeUtil-style diagnostics on any device (see lonutil.py),
  * watch decoded bus traffic and the log.

Only the Python standard library is used for the server (plus pyyaml and paho-mqtt,
which the bridge already needs).

    lonweb.py -c config.yaml [--host 0.0.0.0] [--port 8099]

The interface can switch lights and reset devices, so it listens on 127.0.0.1 unless told
otherwise. When you open it to the network, set web.username / web.password.
"""

import argparse
import base64
import collections
import copy
import glob
import hmac
import json
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

import lon2mqtt
from lon2mqtt import CODECS, ENTITY_TYPES, Bridge, LonError, LonLink, LonNode, load_nodes
from lonutil import NodeTools, PacketDescriber, service_pin_from_packet

log = logging.getLogger("lonweb")
HERE = os.path.dirname(os.path.abspath(__file__))
MASK = "********"

DEFAULT_CONFIG = {
    "mqtt": {"broker": "", "port": 1883, "username": "", "password": "",
             "base_topic": "lon2mqtt", "discovery_prefix": "homeassistant"},
    "lonworks": {"serial_port": "/dev/ttyUSB0", "baudrate": 460800, "domain_id": "",
                 "source_subnet": 1, "source_node": 127, "timeout": 1.5, "retries": 3},
    "web": {"host": "127.0.0.1", "port": 8099},
    "nodes": [],
}


# ============================================================================
# Server-sent events
# ============================================================================

class EventHub:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)

    def publish(self, kind, data):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass                        # slow browser tab: drop rather than block the bus


class HubLogHandler(logging.Handler):
    def __init__(self, app):
        super().__init__(logging.INFO)
        self.app = app

    def emit(self, record):
        try:
            item = {"t": record.created, "level": record.levelname, "msg": record.getMessage()}
            self.app.logs.append(item)
            self.app.hub.publish("log", item)
        except Exception:
            pass


# ============================================================================
# Application
# ============================================================================

class App:
    def __init__(self, config_path):
        self.config_path = os.path.abspath(config_path)
        self.hub = EventHub()
        self.logs = collections.deque(maxlen=400)
        self.packets = collections.deque(maxlen=400)
        self.pins = collections.deque(maxlen=30)
        self.heard = {}                     # "subnet/node" -> {"count", "last"}
        self.discovered = []
        self.adhoc = {}                     # neuron id -> LonNode for devices not in the config
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.config_error = None
        self.bridge = None
        self.thread = None
        self.lock = threading.RLock()
        self.describer = PacketDescriber()

    # ------------------------------------------------------------ configuration
    def load(self):
        if not os.path.exists(self.config_path):
            log.warning("%s does not exist yet; starting with an empty configuration",
                        self.config_path)
            self.cfg = copy.deepcopy(DEFAULT_CONFIG)
            return
        self.cfg = lon2mqtt.load_config(self.config_path, strict=False)

    def public_config(self):
        cfg = copy.deepcopy(self.cfg)
        for section in ("mqtt", "web"):
            if (cfg.get(section) or {}).get("password"):
                cfg[section]["password"] = MASK
        return cfg

    @staticmethod
    def _clean(cfg):
        """Normalise what the browser sent: drop empty values, coerce numbers."""
        def num(v, cast=int):
            return cast(v) if v not in ("", None) else None

        def prune(d):
            return {k: v for k, v in d.items() if v not in ("", None)}

        out = {}
        m = prune(dict(cfg.get("mqtt") or {}))
        if "port" in m:
            m["port"] = int(m["port"])
        out["mqtt"] = m
        lw = prune(dict(cfg.get("lonworks") or {}))
        for k in ("baudrate", "source_subnet", "source_node", "retries"):
            if k in lw:
                lw[k] = int(lw[k])
        if "timeout" in lw:
            lw["timeout"] = float(lw["timeout"])
        if "domain_id" in lw:
            lw["domain_id"] = str(lw["domain_id"]).strip().lower()
        else:
            lw["domain_id"] = ""
        out["lonworks"] = lw
        w = prune(dict(cfg.get("web") or {}))
        if "port" in w:
            w["port"] = int(w["port"])
        if w:
            out["web"] = w
        nodes = []
        for n in cfg.get("nodes") or []:
            n = prune(dict(n))
            ents = []
            for e in n.get("entities") or []:
                e = prune(dict(e))
                if "id" not in e and "name" in e:
                    e["id"] = lon2mqtt.slug(e["name"])
                for k in ("nv_in", "nv_out"):
                    if k in e:
                        e[k] = num(e[k])
                for k in ("polling_interval", "min", "max", "step"):
                    if k in e:
                        v = float(e[k])
                        e[k] = int(v) if v == int(v) else v
                for k in ("invert", "confirm"):
                    if k in e and not e[k]:
                        del e[k]
                if e.get("dimmable", True) is True:
                    e.pop("dimmable", None)
                ents.append(e)
            n["entities"] = ents
            for k in ("subnet", "node"):
                if k in n:
                    n[k] = int(n[k])
            nodes.append(n)
        out["nodes"] = nodes
        for k, v in cfg.items():                # keep sections this UI does not know
            out.setdefault(k, v)
        return out

    def save(self, new):
        new = self._clean(new)
        for section in ("mqtt", "web"):         # keep stored passwords the browser never saw
            if (new.get(section) or {}).get("password") == MASK:
                old = (self.cfg.get(section) or {}).get("password")
                if old:
                    new[section]["password"] = old
                else:
                    del new[section]["password"]
        LonLink(new["lonworks"])                # validates addressing; does not open the port
        load_nodes(new)                         # validates nodes and entities
        ids = [lon2mqtt.slug(n.get("id", n.get("name", n.get("neuron_id")))) for n in new["nodes"]]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique")
        if os.path.exists(self.config_path):
            shutil.copy2(self.config_path, self.config_path + ".bak")
        tmp = self.config_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# lon2mqtt configuration (saved from the web interface)\n")
            yaml.safe_dump(new, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
        os.replace(tmp, self.config_path)
        log.info("Configuration saved to %s", self.config_path)
        self.cfg = new
        self.restart()

    # ------------------------------------------------------------ bridge life cycle
    def start(self):
        with self.lock:
            self.config_error = None
            try:
                bridge = Bridge(self.cfg)
            except Exception as e:              # bad configuration: keep the UI up to fix it
                self.config_error = str(e)
                log.error("Configuration error: %s", e)
                self.bridge = None
                return
            bridge.on_state.append(self._on_state)
            bridge.link.on_traffic.append(self._on_traffic)
            self.bridge = bridge
            self.adhoc.clear()
            self.thread = threading.Thread(target=self._run, args=(bridge,), name="bridge",
                                           daemon=True)
            self.thread.start()

    @staticmethod
    def _run(bridge):
        try:
            bridge.run()
        except Exception:
            log.exception("Bridge stopped unexpectedly")
            bridge.shutdown()

    def stop(self):
        with self.lock:
            if self.bridge:
                self.bridge.stop.set()
                if self.thread:
                    self.thread.join(15)
                self.bridge = None

    def restart(self):
        with self.lock:
            self.stop()
            self.start()

    # ------------------------------------------------------------ live data
    def _on_state(self, e):
        self.hub.publish("state", self._entity_state(e))

    def _on_traffic(self, direction, p):
        try:
            d = self.describer.describe(direction, p)
        except Exception:
            return
        d["t"] = time.time()
        self.packets.append(d)
        self.hub.publish("packet", d)
        if direction == "rx":
            h = self.heard.setdefault(d["src"], {"count": 0})
            h["count"] += 1
            h["last"] = d["t"]
            pin = service_pin_from_packet(p)
            if pin:
                pin["t"] = d["t"]
                self.pins.appendleft(pin)
                self.hub.publish("pin", pin)
                log.info("Service pin from %s", pin["neuron_id"])

    @staticmethod
    def _entity_state(e):
        return {"id": e.id, "state": e.state, "updated": e.updated, "error": e.error}

    def state(self):
        b = self.bridge
        out = {"version": lon2mqtt.__version__, "config_path": self.config_path,
               "config_error": self.config_error, "time": time.time(),
               "pins": list(self.pins), "discovered": self.discovered,
               "heard": [dict(v, address=k) for k, v in sorted(self.heard.items())]}
        lw, m = self.cfg.get("lonworks") or {}, self.cfg.get("mqtt") or {}
        out["link"] = {"port": lw.get("serial_port", "/dev/ttyUSB0"),
                       "domain": str(lw.get("domain_id", "")),
                       "subnet": lw.get("source_subnet", 1), "node": lw.get("source_node", 127),
                       "connected": bool(b and b.link.connected)}
        out["mqtt"] = {"enabled": bool(b and b.mqtt_enabled),
                       "connected": bool(b and b.mqtt_connected),
                       "broker": f"{m.get('broker', '')}:{m.get('port', 1883)}" if m.get("broker") else ""}
        out["bridge"] = {"running": bool(b), "paused": bool(b and b.paused.is_set())}
        nodes = []
        if b:
            raw = {lon2mqtt.slug(e["id"]): e for n in self.cfg["nodes"] for e in n.get("entities") or []}
            now = time.monotonic()
            for n in b.nodes:
                ents = []
                for e in b.entities:
                    if e.node is not n:
                        continue
                    d = self._entity_state(e)
                    d.update(name=e.name, type=e.type, snvt_type=e.codec.name, nv_in=e.nv_in,
                             nv_out=e.nv_out, polling_interval=e.poll_interval,
                             writable=e.writable, dimmable=e.dimmable, unit=e.unit,
                             min=e.min, max=e.max, step=e.step,
                             confirm=bool(raw.get(e.id, {}).get("confirm")))
                    ents.append(d)
                nodes.append({"id": n.id, "name": n.name, "neuron_id": n.nid_hex,
                              "subnet": n.subnet, "node": n.node,
                              "silent": n.fail_count > 0,
                              "retry_in": max(0, round(n.retry_at - now)) if n.fail_count else 0,
                              "entities": ents})
        out["nodes"] = nodes
        return out

    # ------------------------------------------------------------ node tools
    def _node(self, key):
        b = self.bridge
        key = str(key).strip()
        for n in b.nodes:
            if key in (n.id, n.nid_hex):
                return n
        nid = lon2mqtt.parse_neuron_id(key).hex()
        for n in b.nodes:
            if n.nid_hex == nid:
                return n
        if nid not in self.adhoc:
            self.adhoc[nid] = LonNode(nid, nid, nid)
        return self.adhoc[nid]

    def tool(self, name, node=None, args=None):
        b = self.bridge
        if not b:
            raise LonError("the bridge is not running; fix the configuration first")
        if not b.link.connected:
            raise LonError(f"the interface on {b.link.port} is not connected")
        args = args or {}
        tools = NodeTools(b.link)

        def progress(done, total):
            self.hub.publish("progress", {"tool": name, "done": done, "total": total})

        if name == "find":
            self.discovered = tools.find_devices(progress=progress)
            return self.discovered
        n = self._node(node)
        if name == "status":
            return tools.status(n)
        if name == "info":
            return tools.info(n)
        if name == "domains":
            return tools.domain_table(n)
        if name == "addresses":
            return tools.address_table(n, progress=progress)
        if name == "nvs":
            return tools.nv_table(n, values=args.get("values", True), progress=progress)
        if name == "aliases":
            return tools.alias_table(n)
        if name == "wink":
            return tools.wink(n)
        if name == "clear":
            return tools.clear_status(n)
        if name == "mode":
            return tools.set_mode(n, args.get("mode"))
        if name == "poll":
            return tools.poll_nv(n, int(args["index"]), args.get("snvt"))
        if name == "update":
            return tools.update_nv(n, int(args["index"]), args["value"], args.get("snvt"))
        if name == "memory":
            data = tools.read_memory(n, args.get("mode", "abs"), int(str(args["offset"]), 0),
                                     int(str(args["count"]), 0))
            return {"offset": int(str(args["offset"]), 0), "data": data.hex()}
        raise ValueError(f"unknown tool '{name}'")


# ============================================================================
# HTTP
# ============================================================================

def serial_ports():
    ports = sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))
    by_id = {os.path.realpath(p): p for p in glob.glob("/dev/serial/by-id/*")}
    return [{"path": p, "by_id": by_id.get(p)} for p in ports]


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "lonweb"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        if status in (401, 403):
            self.close_connection = True    # the request body may not have been read
        self._json({"error": message}, status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _authorised(self):
        w = self.app.cfg.get("web") or {}
        if not w.get("password"):
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            except Exception:
                user = pw = ""
            if hmac.compare_digest(user, str(w.get("username", "admin"))) and \
                    hmac.compare_digest(pw, str(w["password"])):
                return True
        self.close_connection = True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="lon2mqtt"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._authorised():
            return
        path = self.path.split("?")[0]
        app = self.app
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(app.state())
        if path == "/api/config":
            return self._json({"config": app.public_config(), "path": app.config_path,
                               "ports": serial_ports(), "snvt_types": sorted(CODECS),
                               "entity_types": list(ENTITY_TYPES)})
        if path == "/api/history":
            return self._json({"logs": list(app.logs), "packets": list(app.packets)})
        if path == "/api/events":
            return self._events()
        self._error(404, "not found")

    def _static(self, name, ctype):
        try:
            with open(os.path.join(HERE, "web", name), "rb") as f:
                body = f.read()
        except OSError:
            return self._error(404, "web/index.html is missing next to lonweb.py")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = self.app.hub.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    kind, data = q.get(timeout=15)
                    msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)

    # ------------------------------------------------------------ POST / PUT
    def do_PUT(self):
        self.do_POST()

    def do_POST(self):
        if not self._authorised():
            return
        # A custom header cannot be sent cross-site without a CORS preflight, which this
        # server never grants: other web pages cannot drive the interface.
        if self.headers.get("X-Requested-With") != "lon2mqtt":
            return self._error(403, "missing X-Requested-With header")
        app, path = self.app, self.path.split("?")[0]
        try:
            body = self._body()
            if path == "/api/config":
                app.save(body.get("config") or {})
                return self._json({"saved": True, "config_error": app.config_error})
            if path == "/api/bridge":
                action = body.get("action")
                if action == "restart":
                    app.restart()
                elif action in ("pause", "resume") and app.bridge:
                    (app.bridge.paused.set if action == "pause" else app.bridge.paused.clear)()
                    log.info("Polling %s", "paused" if action == "pause" else "resumed")
                else:
                    return self._error(400, "unknown action")
                return self._json({"ok": True})
            if path == "/api/entity":
                if not app.bridge:
                    return self._error(503, "the bridge is not running")
                payload = body.get("payload")
                app.bridge.command(lon2mqtt.slug(body.get("id", "")),
                                   payload if isinstance(payload, str) else json.dumps(payload))
                return self._json({"queued": True})
            if path == "/api/tool":
                return self._json({"result": app.tool(body.get("tool"), body.get("node"),
                                                      body.get("args"))})
            self._error(404, "not found")
        except KeyError as e:
            self._error(400, f"missing or unknown: {e}")
        except (ValueError, TypeError, yaml.YAMLError) as e:
            self._error(400, str(e))
        except LonError as e:
            self._error(502, str(e))
        except Exception as e:                  # pragma: no cover
            log.exception("request failed")
            self._error(500, str(e))


def make_server(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="Web interface for the lon2mqtt bridge")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--host", help="listen address (default: web.host or 127.0.0.1)")
    ap.add_argument("--port", type=int, help="listen port (default: web.port or 8099)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="run against a simulated interface and devices (no hardware needed)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if a.demo:
        import tempfile
        import lonsim
        sim = lonsim.start_demo()
        app = App(os.path.join(tempfile.gettempdir(), "lon2mqtt-demo.yaml"))
        app.cfg = lonsim.demo_config(sim.path)
        logging.getLogger().addHandler(HubLogHandler(app))
        log.info("Demo mode: simulated interface on %s; changes are saved to %s",
                 sim.path, app.config_path)
    else:
        app = App(a.config)
        logging.getLogger().addHandler(HubLogHandler(app))
        try:
            app.load()
        except Exception as e:
            app.config_error = f"cannot read {app.config_path}: {e}"
            log.error(app.config_error)
    web = app.cfg.get("web") or {}
    host = a.host or web.get("host", "127.0.0.1")
    port = a.port or int(web.get("port", 8099))
    if host not in ("127.0.0.1", "localhost", "::1") and not web.get("password"):
        log.warning("Listening on %s without a password: anyone on that network can control "
                    "your devices. Set web.username and web.password.", host)
    httpd = make_server(app, host, port)
    if not app.config_error:
        app.start()

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    log.info("Web interface on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    finally:
        app.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
