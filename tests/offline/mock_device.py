"""A small stdlib mock of the ESPKB HTTP API for offline tests of tools/espkb.py.

It records every request (method, path, query, headers, raw body, parsed JSON)
and answers with canned API.md-shaped responses, or with responses a test
installs via :meth:`MockDevice.respond`.
"""
from __future__ import annotations

import copy
import json
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

STATUS = {
    "ok": True,
    "device": {"name": "espkb", "api": 1, "version": "1.0.0", "uptime_ms": 123456,
               "heap_free": 250000, "heap_min": 230000, "reset_reason": "poweron",
               "build": "Nov 11 2026 20:14:41", "sha256": "00c0ffee00c0ffee", "partition": "ota_0"},
    "usb": {"mounted": True, "suspended": False, "remote_wakeup": False,
            "leds": {"num": True, "caps": False, "scroll": False},
            "kbd_protocol": "report", "mouse_protocol": "report"},
    "hid": {"busy": False, "queued": 0, "keys": [225, 4], "buttons": [],
            "reports_sent": 1024, "jobs_done": 88, "jobs_failed": 0},
    "wifi": {"sta_connected": True, "ssid": "MyWiFi", "ip": "192.168.1.50", "rssi": -48,
             "ap_active": False, "ap_ssid": "ESPKB-5D8C", "ap_ip": "192.168.4.1", "hostname": "espkb"},
    "ota": {"state": "idle", "received": 0, "total": 0},
    "ws_clients": 1,
    "auth_required": False,
}
CONFIG = {"ok": True, "hostname": "espkb", "wifi_ssid": "MyWiFi", "wifi_password_set": True,
          "ap_ssid": "ESPKB-5D8C", "ap_password_set": True, "token_set": False, "key_delay_ms": 5}
KEYS = {"ok": True, "keys": {"enter": 40, "a": 4, "return": 40, "ctrl": 224},
        "buttons": ["left", "right", "middle", "back", "forward"]}
INFO = {"ok": True, "name": "espkb", "api": 1, "version": "1.0.0",
        "endpoints": [{"method": "POST", "path": "/api/type", "desc": "type text"}],
        "steps": ["{\"text\":\"...\"} type text"]}
SCAN = {"ok": True, "aps": [{"ssid": "ESPKB-TEST", "rssi": -40, "channel": 6, "secure": True},
                            {"ssid": "Open", "rssi": -70, "channel": 1, "secure": False}]}
OTA_INFO = {"ok": True,
            "running": {"version": "1.0.0", "build": "Nov 11 2026 20:14:41", "sha256": "00c0ffee00c0ffee",
                        "partition": "ota_0", "idf": "6.1.0", "state": "valid"},
            "next": {"partition": "ota_1", "size": 4194304},
            "update": {"state": "idle", "received": 0, "total": 0, "message": ""}}
ACTIONS = ("/api/type", "/api/key", "/api/mouse", "/api/seq")


def firmware_image(version="1.2.3", project="espkb", sha=bytes(range(0xA0, 0xC0)), size=4096):
    """A file that starts like an ESP-IDF app image (see espkb.parse_image)."""
    img = bytearray(size)
    img[0] = 0xE9
    struct.pack_into("<H", img, 12, 9)
    struct.pack_into("<I", img, 32, 0xABCD5432)
    img[48:48 + len(version)] = version.encode()
    img[80:80 + len(project)] = project.encode()
    img[112:120] = b"12:34:56"
    img[128:139] = b"Nov 12 2026"
    img[144:149] = b"6.1.0"
    img[176:208] = sha
    return bytes(img)


class Recorded:
    def __init__(self, method: str, path: str, query: Dict[str, str], headers: Dict[str, str], body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        try:
            self.json = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            self.json = None

    def __repr__(self) -> str:
        return "Recorded(%s %s %r %r)" % (self.method, self.path, self.query, self.body[:80])


class MockDevice:
    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self._custom: Dict[tuple, Dict[str, Any]] = {}
        self._job = 0
        self.status = copy.deepcopy(STATUS)   # what GET /api/status answers; an OTA upload "reboots" it
        self.ota_rollback = False             # True: an uploaded firmware does not survive its reboot
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _handle(self) -> None:
                u = urllib.parse.urlsplit(self.path)
                q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = Recorded(self.command, u.path, q, {k.lower(): v for k, v in self.headers.items()}, body)
                with mock._lock:
                    mock.requests.append(rec)
                status, payload, raw, delay = mock._answer(rec)
                if delay:
                    time.sleep(delay)
                data = raw if raw is not None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if raw is None else "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "MockDevice":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def host(self) -> str:
        return "127.0.0.1:%d" % self.server.server_address[1]

    # -- behaviour ---------------------------------------------------------
    def respond(self, method: str, path: str, status: int = 200, payload: Optional[Dict[str, Any]] = None,
                raw: Optional[bytes] = None, delay: float = 0.0, times: Optional[int] = None) -> None:
        self._custom[(method, path)] = {"status": status, "payload": payload, "raw": raw, "delay": delay,
                                        "times": times}

    def error(self, method: str, path: str, status: int, code: str, message: str = "nope", **extra: Any) -> None:
        payload = {"ok": False, "error": code, "message": message}
        payload.update(extra)
        self.respond(method, path, status, payload)

    def last(self) -> Recorded:
        assert self.requests, "no request was received"
        return self.requests[-1]

    def clear(self) -> None:
        with self._lock:
            self.requests = []

    def _answer(self, rec: Recorded):
        c = self._custom.get((rec.method, rec.path))
        if c is not None:
            if c["times"] is not None:
                c["times"] -= 1
                if c["times"] <= 0:
                    del self._custom[(rec.method, rec.path)]
            return c["status"], c["payload"], c["raw"], c["delay"]
        p = rec.path
        if rec.method == "GET":
            table = {"/api/status": self.status, "/api/config": CONFIG, "/api/keys": KEYS, "/api": INFO,
                     "/api/wifi/scan": SCAN, "/api/ota": OTA_INFO}
            if p in table:
                return 200, table[p], None, 0
        if rec.method == "POST":
            if p in ACTIONS:
                with self._lock:
                    self._job += 1
                    job = self._job
                if rec.query.get("async") == "1":
                    return 200, {"ok": True, "job": job, "queued": True}, None, 0
                return 200, {"ok": True, "job": job, "elapsed_ms": 12}, None, 0
            if p == "/api/release":
                return 200, {"ok": True, "aborted": 0}, None, 0
            if p in ("/api/wake", "/api/reboot"):
                return 200, {"ok": True}, None, 0
            if p == "/api/ota":
                body = rec.body
                if len(body) < 288 or body[0] != 0xE9:
                    return 400, {"ok": False, "error": "bad_request", "message": "not an ESP application image"}, None, 0
                version = body[48:80].split(b"\0")[0].decode()
                reboot = rec.query.get("reboot", "1") in ("1", "true", "yes")
                if reboot:
                    dev = self.status["device"]
                    dev["uptime_ms"] = 1500       # the device restarted ...
                    if not self.ota_rollback:     # ... into the uploaded image
                        dev.update(version=version, sha256=body[176:184].hex(), partition="ota_1")
                return 200, {"ok": True, "bytes": len(body), "partition": "ota_1", "version": version,
                             "reboot": reboot}, None, 0
            if p == "/api/config":
                rr = bool(rec.json) and any(k in rec.json for k in ("hostname", "wifi_ssid", "wifi_password", "ap_password"))
                return 200, {"ok": True, "reboot_required": rr}, None, 0
        known = ACTIONS + ("/api/release", "/api/wake", "/api/reboot", "/api/config", "/api/status",
                           "/api/keys", "/api", "/api/wifi/scan", "/api/ota")
        if p in known:
            return 405, {"ok": False, "error": "method_not_allowed", "message": "wrong method"}, None, 0
        return 404, {"ok": False, "error": "not_found", "message": "no such path"}, None, 0
