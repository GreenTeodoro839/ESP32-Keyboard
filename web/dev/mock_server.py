#!/usr/bin/env python3
"""Mock ESPKB device for developing and testing web/index.html (dev aid only).

Serves ../index.html at "/" (re-read on every request), implements the HTTP API
subset the page uses plus the /ws channel, following docs/API.md:

  GET  /api/status  /api/config  /api/wifi/scan  /api/keys  /api/ota
  POST /api/config  /api/wake  /api/reboot  /api/release
  POST /api/ota     firmware upload: checks the image header like the device, shows progress in the
                    status pushes, then "reboots" into the uploaded image (its version / sha256)
  WS   /ws          steps, {"steps":[..]}, step arrays, "id" -> ack, ping -> pong,
                    {"release":true}, status pushes (on connect, on change, every 2 s),
                    per-connection 1000 ms keep-alive release, release on close.

Every WebSocket message received (and HTTP API request, and notable events such as
keep-alive releases) is recorded with a timestamp: appended as JSON lines to --log
and kept in memory for GET /mock/log.

Mock-only control endpoints (never require a token):
  GET  /mock/log?since=N   -> {"records":[...], "next":N}
  POST /mock/clear         -> forget recorded records
  POST /mock/state         -> merge {"usb":{..},"wifi":{..},"token":"..","push_error":{..}}
                              into the simulated device and push status;
                              "ota_rollback": true  -> an uploaded firmware does not survive its reboot,
                              "ota_fail": {"error":..,"message":..} -> uploads fail with this error

Binds 127.0.0.1 only. Never talks to real hardware.

  .venv/bin/python web/dev/mock_server.py --port 8765 [--token SECRET] [--ap-mode]
                                          [--usb mounted|unmounted|suspended]
"""
import argparse
import asyncio
import json
import pathlib
import re
import struct
import sys
import time

from aiohttp import WSMsgType, web

HERE = pathlib.Path(__file__).resolve().parent
INDEX = HERE.parent / "index.html"


def build_keys():
    k = {c: 4 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    for i in range(1, 10):
        k[str(i)] = 29 + i
    k["0"] = 39
    pairs = (
        "enter 40 return 40 esc 41 escape 41 backspace 42 bksp 42 tab 43 space 44 spacebar 44 "
        "minus 45 - 45 equal 46 = 46 lbracket 47 [ 47 rbracket 48 ] 48 backslash 49 \\ 49 "
        "semicolon 51 ; 51 quote 52 ' 52 grave 53 ` 53 comma 54 , 54 period 55 . 55 dot 55 "
        "slash 56 / 56 capslock 57 printscreen 70 prtsc 70 scrolllock 71 pause 72 insert 73 "
        "ins 73 home 74 pageup 75 pgup 75 delete 76 del 76 end 77 pagedown 78 pgdn 78 right 79 "
        "left 80 down 81 up 82 numlock 83 kp_divide 84 kp_multiply 85 kp_minus 86 kp_plus 87 "
        "kp_enter 88 kp_dot 99 menu 101 app 101 ctrl 224 lctrl 224 control 224 shift 225 "
        "lshift 225 alt 226 lalt 226 option 226 gui 227 lgui 227 win 227 meta 227 super 227 "
        "cmd 227 rctrl 228 rshift 229 ralt 230 altgr 230 rgui 231 rwin 231"
    ).split()
    for i in range(0, len(pairs), 2):
        k[pairs[i]] = int(pairs[i + 1])
    for i in range(1, 13):
        k[f"f{i}"] = 57 + i
    for i in range(13, 25):
        k[f"f{i}"] = 91 + i
    for i in range(1, 10):
        k[f"kp_{i}"] = 88 + i
    k["kp_0"] = 98
    return k


KEYS = build_keys()
BUTTONS = ["left", "right", "middle", "back", "forward"]
VERBS = ["text", "key", "move", "click", "button", "scroll", "delay", "release"]

ERR_HTTP = {
    "bad_request": 400, "bad_step": 400, "unauthorized": 401, "forbidden": 403,
    "not_found": 404, "method_not_allowed": 405, "aborted": 409, "too_large": 413,
    "busy": 503, "usb_not_ready": 503, "usb_suspended": 503, "timeout": 504, "internal": 500,
}
OTA_SLOT_SIZE = 0x400000
OTA_HEAD_SIZE = 288


def parse_image_head(head):
    """Start of an ESP-IDF app image -> firmware identity, or raise DevError like the device."""
    if len(head) < OTA_HEAD_SIZE or head[0] != 0xE9 or struct.unpack_from("<I", head, 32)[0] != 0xABCD5432:
        raise DevError("bad_request", "not an ESP application image (send the build's firmware.bin)")

    def s(off, n):
        return head[off:off + n].split(b"\0")[0].decode("utf-8", "replace")

    if s(80, 32) != "espkb":
        raise DevError("bad_request", "image is not an espkb firmware")
    if struct.unpack_from("<H", head, 12)[0] != 9:
        raise DevError("bad_request", "image is built for a different chip or chip revision")
    return {"version": s(48, 32), "build": s(128, 16) + " " + s(112, 16), "sha256": head[176:184].hex()}


class DevError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def body(self):
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_key(v, index):
    """Return list of usages for a key value (name, combo, int, "0xNN")."""
    if is_int(v):
        if 0 <= v <= 255:
            return [v]
        raise DevError("bad_step", f"key usage out of range: {v}", index=index)
    if not isinstance(v, str) or not v:
        raise DevError("bad_step", "key must be a name or usage", index=index)
    out = []
    for part in v.lower().split("+"):
        if part in KEYS:
            out.append(KEYS[part])
        elif re.fullmatch(r"0x[0-9a-f]{1,2}", part):
            out.append(int(part, 16))
        else:
            raise DevError("bad_step", f"unknown key name: {part!r}", index=index)
    return out


def check_text(s, index):
    for i, ch in enumerate(s):
        c = ord(ch)
        if not (0x20 <= c <= 0x7E or c in (8, 9, 10, 13, 27)):
            off = len(s[:i].encode("utf-8"))
            raise DevError("bad_step", f"unsupported character U+{c:04X}", index=index, offset=off)


def validate_step(st, index):
    if not isinstance(st, dict):
        raise DevError("bad_step", "step must be an object", index=index)
    verbs = [v for v in VERBS if v in st]
    if len(verbs) != 1:
        raise DevError("bad_step", "step must have exactly one verb", index=index)
    v = verbs[0]
    val = st[v]
    if v == "text":
        if not isinstance(val, str):
            raise DevError("bad_step", "text must be a string", index=index)
        check_text(val, index)
        if "delay_ms" in st and not (is_int(st["delay_ms"]) and 0 <= st["delay_ms"] <= 1000):
            raise DevError("bad_step", "delay_ms out of range", index=index)
    elif v == "key":
        st["_usages"] = parse_key(val, index)
        act = st.get("action", "tap")
        if act not in ("tap", "down", "up"):
            raise DevError("bad_step", "bad action", index=index)
    elif v == "move":
        if not (isinstance(val, list) and len(val) == 2 and all(is_int(x) and -32767 <= x <= 32767 for x in val)):
            raise DevError("bad_step", "move must be [dx,dy] within +-32767", index=index)
    elif v == "click":
        if val not in BUTTONS:
            raise DevError("bad_step", f"unknown button {val!r}", index=index)
    elif v == "button":
        if val not in BUTTONS or st.get("action") not in ("down", "up"):
            raise DevError("bad_step", "button needs a known name and action down/up", index=index)
    elif v == "scroll":
        ok = (is_int(val) and -32767 <= val <= 32767) or (
            isinstance(val, list) and len(val) == 2 and all(is_int(x) and -32767 <= x <= 32767 for x in val))
        if not ok:
            raise DevError("bad_step", "scroll must be int or [v,h] within +-32767", index=index)
    elif v == "delay":
        if not (is_int(val) and 0 <= val <= 60000):
            raise DevError("bad_step", "delay out of range", index=index)
    elif v == "release":
        if val is not True:
            raise DevError("bad_step", "release must be true", index=index)
    return v


class Client:
    seq = 0

    def __init__(self, ws, request):
        Client.seq += 1
        self.id = Client.seq
        self.ws = ws
        self.request = request
        self.last_rx = time.monotonic()
        self.err_times = {}


class Job:
    def __init__(self, steps, jid, owner, client=None, ack_id=None, has_id=False):
        self.steps, self.jid, self.owner = steps, jid, owner
        self.client, self.ack_id, self.has_id = client, ack_id, has_id
        self.future = asyncio.get_event_loop().create_future()


class Device:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.token = args.token or ""
        self.config = {"hostname": "espkb", "wifi_ssid": "" if args.ap_mode else "MyWiFi",
                       "key_delay_ms": 5, "led_quiet": False}
        self.secrets = {"wifi_password": "" if args.ap_mode else "password1",
                        "ap_password": "espkb1234"}
        self.usb = {"mounted": args.usb != "unmounted", "suspended": args.usb == "suspended",
                    "remote_wakeup": False, "leds": {"num": True, "caps": False, "scroll": False},
                    "kbd_protocol": "report", "mouse_protocol": "report"}
        self.wifi = {"sta_connected": not args.ap_mode, "ssid": self.config["wifi_ssid"],
                     "ip": "" if args.ap_mode else "192.168.1.50", "rssi": -48 if not args.ap_mode else 0,
                     "ap_active": bool(args.ap_mode), "ap_ssid": "ESPKB-5D8C", "ap_ip": "192.168.4.1",
                     "hostname": "espkb"}
        self.keys = {}      # usage -> owner
        self.buttons = {}   # name -> owner
        self.clients = {}
        self.queue = []
        self.queue_event = asyncio.Event()
        self.current = None
        self.current_task = None
        self.jid = 0
        self.stats = {"reports_sent": 0, "jobs_done": 0, "jobs_failed": 0}
        self.records = []
        self.logf = open(args.log, "a", encoding="utf-8") if args.log else None
        self.rebooting = False
        self.site = None
        self.runner = None
        self.push_pending = False
        self.fw = {"version": "1.0.0-mock", "build": "Nov 11 2026 20:14:41", "sha256": "00c0ffee00c0ffee",
                   "partition": "ota_0"}
        self.fw_next = None          # identity to boot into at the next reboot
        self.ota = {"state": "idle", "received": 0, "total": 0}
        self.ota_message = ""
        self.ota_rollback = False
        self.ota_fail = None

    # ---- recording -------------------------------------------------------
    def record(self, kind, msg, client=None, **extra):
        rec = {"i": len(self.records), "t": round(time.time(), 4),
               "ms": round((time.monotonic() - self.t0) * 1000, 1), "kind": kind, "msg": msg}
        if client is not None:
            rec["client"] = client
        rec.update(extra)
        self.records.append(rec)
        if self.logf:
            self.logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.logf.flush()

    # ---- status ----------------------------------------------------------
    def uptime(self):
        return int((time.monotonic() - self.t0) * 1000)

    def status(self):
        return {
            "device": {"name": "espkb", "api": 1, "version": self.fw["version"], "uptime_ms": self.uptime(),
                       "heap_free": 250000, "heap_min": 230000, "reset_reason": "poweron",
                       "build": self.fw["build"], "sha256": self.fw["sha256"], "partition": self.fw["partition"]},
            "usb": json.loads(json.dumps(self.usb)),
            "hid": {"busy": self.current is not None, "queued": len(self.queue),
                    "keys": sorted(self.keys), "buttons": [b for b in BUTTONS if b in self.buttons],
                    **self.stats},
            "wifi": dict(self.wifi, hostname=self.config["hostname"]),
            "ota": dict(self.ota),
            "ws_clients": len(self.clients),
            "auth_required": bool(self.token),
        }

    def schedule_push(self):
        if self.push_pending:
            return
        self.push_pending = True
        asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(self._push()))

    async def _push(self):
        self.push_pending = False
        msg = {"status": self.status()}
        for c in list(self.clients.values()):
            await self.send(c, msg)

    async def send(self, client, obj):
        try:
            await client.ws.send_str(json.dumps(obj))
        except Exception:
            pass

    async def report_error(self, client, code, message, **extra):
        now = time.monotonic()
        if now - client.err_times.get(code, 0) < 1.0:
            return
        client.err_times[code] = now
        await self.send(client, {"error": code, "message": message, **extra})

    # ---- HID state -------------------------------------------------------
    def release_owner(self, owner):
        changed = False
        for u in [u for u, o in self.keys.items() if o == owner]:
            del self.keys[u]
            changed = True
        for b in [b for b, o in self.buttons.items() if o == owner]:
            del self.buttons[b]
            changed = True
        if changed:
            self.schedule_push()
        return changed

    def release_all(self):
        if self.keys or self.buttons:
            self.keys.clear()
            self.buttons.clear()
            self.schedule_push()

    async def emergency_release(self):
        n = 0
        for job in self.queue:
            if not job.future.done():
                job.future.set_exception(DevError("aborted", "aborted by release"))
            n += 1
        self.queue.clear()
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()
            n += 1
        self.release_all()
        self.record("event", {"emergency_release": n})
        return n

    # ---- job execution ---------------------------------------------------
    def enqueue(self, job):
        if len(self.queue) >= 32:
            raise DevError("busy", "queue full")
        self.queue.append(job)
        self.queue_event.set()
        self.schedule_push()

    async def worker(self):
        while True:
            while not self.queue:
                self.queue_event.clear()
                await self.queue_event.wait()
            job = self.queue.pop(0)
            if job.future.done():
                continue
            self.current = job
            self.current_task = asyncio.ensure_future(self.run_job(job))
            try:
                await self.current_task
                self.stats["jobs_done"] += 1
                if not job.future.done():
                    job.future.set_result(True)
            except asyncio.CancelledError:
                self.stats["jobs_failed"] += 1
                if not job.future.done():
                    job.future.set_exception(DevError("aborted", "aborted by release"))
            except DevError as e:
                self.stats["jobs_failed"] += 1
                self.release_all()
                if not job.future.done():
                    job.future.set_exception(e)
            finally:
                self.current = None
                self.current_task = None
                self.schedule_push()

    async def run_job(self, job):
        if not self.usb["mounted"]:
            raise DevError("usb_not_ready", "USB not mounted")
        if self.usb["suspended"]:
            if self.usb["remote_wakeup"]:
                await asyncio.sleep(0.2)
                self.usb["suspended"] = False
                self.schedule_push()
            else:
                raise DevError("usb_suspended", "host suspended")
        kd = self.config["key_delay_ms"]
        for idx, st in enumerate(job.steps):
            v = [x for x in VERBS if x in st][0]
            if v == "text":
                d = st.get("delay_ms", kd)
                self.stats["reports_sent"] += 2 * len(st["text"])
                await asyncio.sleep(len(st["text"]) * (2 * d + 2) / 1000.0 * self.args.speed)
            elif v == "key":
                us = st["_usages"]
                act = st.get("action", "tap")
                if act == "tap":
                    self.stats["reports_sent"] += 2 * len(us)
                    await asyncio.sleep((len(us) * 2 * (kd + 1) + st.get("hold_ms", 20)) / 1000.0 * self.args.speed)
                elif act == "down":
                    for u in us:
                        if u not in self.keys and u < 224 and sum(1 for k in self.keys if k < 224) >= 6:
                            raise DevError("bad_step", "more than 6 keys held", index=idx)
                        self.keys[u] = job.owner
                    self.stats["reports_sent"] += 1
                    self.schedule_push()
                else:
                    for u in us:
                        self.keys.pop(u, None)
                    self.stats["reports_sent"] += 1
                    self.schedule_push()
            elif v == "button":
                if st["action"] == "down":
                    self.buttons[st["button"]] = job.owner
                else:
                    self.buttons.pop(st["button"], None)
                self.stats["reports_sent"] += 1
                self.schedule_push()
            elif v == "click":
                cnt = st.get("count", 1)
                await asyncio.sleep(cnt * (st.get("hold_ms", 20) + st.get("interval_ms", 60)) / 1000.0 * self.args.speed)
            elif v == "delay":
                await asyncio.sleep(st["delay"] / 1000.0)
            elif v == "release":
                self.release_all()
            else:  # move / scroll
                self.stats["reports_sent"] += 1

    def parse_task(self, msg):
        """Return (steps, ack_id, has_id) or raise DevError."""
        ack_id, has_id = None, False
        if isinstance(msg, list):
            steps = msg
        elif isinstance(msg, dict):
            has_id = "id" in msg
            ack_id = msg.get("id")
            if "steps" in msg:
                steps = msg["steps"]
                if not isinstance(steps, list):
                    raise DevError("bad_request", "steps must be an array")
            else:
                steps = [msg]
        else:
            raise DevError("bad_request", "message must be an object or array")
        if len(steps) > 512:
            raise DevError("too_large", "too many steps")
        for i, st in enumerate(steps):
            validate_step(st, i)
        return steps, ack_id, has_id

    # ---- websocket -------------------------------------------------------
    async def ws_handler(self, request):
        if len(self.clients) >= 4:
            return web.json_response({"ok": False, "error": "busy", "message": "too many ws clients"}, status=503)
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=8192)
        await ws.prepare(request)
        c = Client(ws, request)
        self.clients[c.id] = c
        self.record("event", {"ws_open": c.id, "query": dict(request.query)}, client=c.id)
        await self.send(c, {"status": self.status()})
        self.schedule_push()
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                c.last_rx = time.monotonic()
                try:
                    msg = json.loads(m.data)
                except Exception:
                    self.record("ws", m.data, client=c.id, invalid=True)
                    await self.report_error(c, "bad_request", "invalid JSON")
                    continue
                self.record("ws", json.loads(m.data), client=c.id)  # copy: validation annotates msg
                await self.on_ws_message(c, msg)
        finally:
            self.clients.pop(c.id, None)
            released = self.release_owner(("ws", c.id))
            self.record("event", {"ws_close": c.id, "released": released}, client=c.id)
            self.schedule_push()
        return ws

    async def on_ws_message(self, c, msg):
        if isinstance(msg, dict) and "ping" in msg and len([k for k in msg if k in VERBS]) == 0:
            await self.send(c, {"pong": msg["ping"], "t": self.uptime()})
            return
        if isinstance(msg, dict) and msg.get("release") is True and len([k for k in msg if k in VERBS]) == 1:
            n = await self.emergency_release()
            if "id" in msg:
                await self.send(c, {"ack": msg["id"], "ok": True, "aborted": n})
            return
        ack_id, has_id = (msg.get("id"), "id" in msg) if isinstance(msg, dict) else (None, False)
        try:
            steps, ack_id, has_id = self.parse_task(msg)
            if not self.usb["mounted"]:
                raise DevError("usb_not_ready", "USB not mounted; task dropped")
            self.jid += 1
            job = Job(steps, self.jid, ("ws", c.id), c, ack_id, has_id)
            self.enqueue(job)
        except DevError as e:
            await self.finish(c, has_id, ack_id, e)
            return
        asyncio.ensure_future(self.await_job(job))

    async def await_job(self, job):
        try:
            await job.future
            err = None
        except DevError as e:
            err = e
        await self.finish(job.client, job.has_id, job.ack_id, err)

    async def finish(self, c, has_id, ack_id, err):
        if has_id:
            if err is None:
                await self.send(c, {"ack": ack_id, "ok": True})
            else:
                await self.send(c, {"ack": ack_id, **err.body()})
        elif err is not None:
            await self.report_error(c, err.code, err.message, **err.extra)

    async def keepalive_loop(self):
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            for c in list(self.clients.values()):
                owner = ("ws", c.id)
                holds = any(o == owner for o in self.keys.values()) or any(o == owner for o in self.buttons.values())
                if holds and now - c.last_rx > 1.0:
                    self.release_owner(owner)
                    self.record("event", {"keepalive_release": c.id, "idle_ms": round((now - c.last_rx) * 1000)},
                                client=c.id)

    async def status_loop(self):
        while True:
            await asyncio.sleep(2.0)
            self.schedule_push()

    # ---- reboot ----------------------------------------------------------
    def other_slot(self):
        return "ota_1" if self.fw["partition"] == "ota_0" else "ota_0"

    async def do_reboot(self, delay=0.5):
        await asyncio.sleep(delay)
        self.rebooting = True
        self.record("event", {"reboot": True})
        for c in list(self.clients.values()):
            try:
                c.request.transport.abort()
            except Exception:
                pass
        self.clients.clear()
        self.keys.clear()
        self.buttons.clear()
        try:
            await self.site.stop()
        except Exception as e:
            print("site.stop:", e, file=sys.stderr)
        await asyncio.sleep(self.args.reboot_secs)
        if self.fw_next and not self.ota_rollback:
            self.fw = self.fw_next
        self.fw_next = None
        self.ota = {"state": "idle", "received": 0, "total": 0}
        self.ota_message = ""
        self.t0 = time.monotonic() - 0.0
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.args.port)
        await self.site.start()
        self.rebooting = False
        self.record("event", {"rebooted": True})


def make_app(dev):
    allowed_hosts = {"127.0.0.1", "localhost", "espkb.local", "espkb", "192.168.4.1", "192.168.1.50"}

    def token_ok(request):
        if not dev.token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer " + dev.token:
            return True
        if request.headers.get("X-Token") == dev.token:
            return True
        return request.query.get("token") == dev.token

    @web.middleware
    async def mw(request, handler):
        path = request.path
        if path.startswith("/mock/"):
            return await handler(request)
        if dev.rebooting:
            try:
                request.transport.abort()
            except Exception:
                pass
            raise web.HTTPServiceUnavailable()
        if path.startswith("/api"):
            body = None
            if path == "/api/ota" and request.method == "POST":
                body = {"binary": request.content_length}   # streamed by the handler
            elif request.can_read_body:
                raw = await request.text()
                try:
                    body = json.loads(raw)
                except Exception:
                    body = raw
            dev.record("http", {"method": request.method, "path": path, "query": dict(request.query),
                                "authorization": request.headers.get("Authorization"),
                                "origin": request.headers.get("Origin"), "body": body})
        host = request.headers.get("Host", "")
        origin = request.headers.get("Origin")
        if origin and path != "/":
            o = re.sub(r"^[a-z]+://", "", origin).rstrip("/")
            if o != host:
                return web.json_response({"ok": False, "error": "forbidden", "message": "origin mismatch"}, status=403)
        if path.startswith("/api") or path == "/ws":
            if not token_ok(request):
                return web.json_response({"ok": False, "error": "unauthorized", "message": "token required"},
                                         status=401)
            if not dev.token and host.split(":")[0] not in allowed_hosts:
                return web.json_response({"ok": False, "error": "forbidden", "message": "bad Host"}, status=403)
        return await handler(request)

    async def index(request):
        return web.Response(body=INDEX.read_bytes(), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    def ok(**kw):
        return web.json_response({"ok": True, **kw})

    def err(code, message, **extra):
        return web.json_response({"ok": False, "error": code, "message": message, **extra},
                                 status=ERR_HTTP.get(code, 400))

    async def status(request):
        return ok(**dev.status())

    async def get_config(request):
        return ok(hostname=dev.config["hostname"], wifi_ssid=dev.config["wifi_ssid"],
                  wifi_password_set=bool(dev.secrets["wifi_password"]), ap_ssid="ESPKB-5D8C",
                  ap_password_set=bool(dev.secrets["ap_password"]), token_set=bool(dev.token),
                  key_delay_ms=dev.config["key_delay_ms"], led_quiet=dev.config["led_quiet"])

    async def post_config(request):
        try:
            b = await request.json()
        except Exception:
            return err("bad_request", "invalid JSON")
        if not isinstance(b, dict):
            return err("bad_request", "object expected")

        def blen(s):
            return len(s.encode("utf-8"))

        checks = {
            "hostname": lambda v: isinstance(v, str) and re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", v),
            "wifi_ssid": lambda v: isinstance(v, str) and blen(v) <= 32,
            "wifi_password": lambda v: isinstance(v, str) and (blen(v) == 0 or 8 <= blen(v) <= 63),
            "ap_password": lambda v: isinstance(v, str) and 8 <= blen(v) <= 63,
            "token": lambda v: isinstance(v, str) and re.fullmatch(r"[\x21-\x7e]{0,64}", v),
            "key_delay_ms": lambda v: is_int(v) and 0 <= v <= 1000,
            "led_quiet": lambda v: isinstance(v, bool),
        }
        for k, fn in checks.items():
            if k in b and not fn(b[k]):
                return err("bad_request", f"invalid value for {k}")
        reboot = False
        for k in ("hostname", "wifi_ssid"):
            if k in b:
                dev.config[k] = b[k]
                reboot = True
        for k in ("wifi_password", "ap_password"):
            if k in b:
                dev.secrets[k] = b[k]
                reboot = True
        if "key_delay_ms" in b:
            dev.config["key_delay_ms"] = b["key_delay_ms"]
        if "led_quiet" in b:
            dev.config["led_quiet"] = b["led_quiet"]
        if "token" in b:
            dev.token = b["token"]
            dev.schedule_push()
        return ok(reboot_required=reboot)

    async def scan(request):
        await asyncio.sleep(dev.args.scan_secs)
        return ok(aps=[
            {"ssid": "MyWiFi", "rssi": -45, "channel": 6, "secure": True},
            {"ssid": "Office-5G", "rssi": -63, "channel": 11, "secure": True},
            {"ssid": "MyWiFi", "rssi": -80, "channel": 1, "secure": True},
            {"ssid": "Guest Open", "rssi": -71, "channel": 3, "secure": False},
            {"ssid": "", "rssi": -60, "channel": 9, "secure": True},
            {"ssid": "邻居家的网络", "rssi": -88, "channel": 13, "secure": True},
        ])

    async def wake(request):
        if dev.usb["suspended"]:
            async def later():
                await asyncio.sleep(0.3)
                dev.usb["suspended"] = False
                dev.schedule_push()
            asyncio.ensure_future(later())
        return ok()

    async def reboot(request):
        asyncio.ensure_future(dev.do_reboot())
        return ok()

    async def release(request):
        n = await dev.emergency_release()
        return ok(aborted=n)

    async def ota_get(request):
        return ok(running=dict(dev.fw, idf="6.1.0", state="valid"),
                  next={"partition": dev.other_slot(), "size": OTA_SLOT_SIZE},
                  update=dict(dev.ota, message=dev.ota_message))

    async def ota_post(request):
        n = request.content_length or 0
        if n < OTA_HEAD_SIZE:
            return err("bad_request", "send the firmware image (firmware.bin) as the raw request body with a Content-Length")
        if n > OTA_SLOT_SIZE:
            return err("too_large", f"image of {n} bytes does not fit the {OTA_SLOT_SIZE} byte firmware slot")
        if dev.ota["state"] in ("receiving", "verifying"):
            return err("busy", "another firmware update is in progress")
        dev.ota = {"state": "receiving", "received": 0, "total": n}
        dev.ota_message = ""
        dev.schedule_push()
        head, got, failure = b"", 0, None
        try:
            async for chunk in request.content.iter_chunked(16384):
                if len(head) < OTA_HEAD_SIZE:
                    head += chunk[:OTA_HEAD_SIZE - len(head)]
                got += len(chunk)
                if failure is None and len(head) >= OTA_HEAD_SIZE and got - len(chunk) < OTA_HEAD_SIZE:
                    try:
                        fw = parse_image_head(head)
                    except DevError as e:
                        failure = e          # like the device: keep reading, answer at the end
                if failure is None:
                    dev.ota["received"] = got
                    dev.schedule_push()
                if dev.args.ota_rate > 0:
                    await asyncio.sleep(len(chunk) / dev.args.ota_rate)
        except asyncio.CancelledError:      # the client went away and the server cancelled this handler
            dev.ota["state"] = "failed"
            dev.ota_message = f"upload interrupted after {got} of {n} bytes"
            dev.schedule_push()
            dev.record("event", {"ota_failed": dev.ota_message})
            raise
        except Exception:
            pass
        if got != n:
            failure = DevError("bad_request", f"upload interrupted after {got} of {n} bytes")
        if failure is None and dev.ota_fail:
            failure = DevError(dev.ota_fail.get("error", "internal"), dev.ota_fail.get("message", "writing flash failed"))
        if failure is None:
            dev.ota["state"] = "verifying"
            dev.schedule_push()
            await asyncio.sleep(0.3)
        if failure is not None:
            dev.ota["state"] = "failed"
            dev.ota_message = failure.message
            dev.schedule_push()
            dev.record("event", {"ota_failed": failure.message})
            return err(failure.code, failure.message)
        reboot = request.query.get("reboot", "1").lower() in ("1", "true", "yes")
        slot = dev.other_slot()
        dev.ota["state"] = "done"
        dev.fw_next = dict(fw, partition=slot)
        dev.schedule_push()
        dev.record("event", {"ota_done": fw, "bytes": n, "reboot": reboot})
        if reboot:
            asyncio.ensure_future(dev.do_reboot(1.0))
        return ok(bytes=n, partition=slot, version=fw["version"], reboot=reboot)

    async def keys(request):
        return ok(keys={k: v for k, v in KEYS.items()}, buttons=BUTTONS)

    async def mock_log(request):
        since = int(request.query.get("since", "0"))
        return web.json_response({"records": dev.records[since:], "next": len(dev.records)})

    async def mock_clear(request):
        dev.records.clear()
        return web.json_response({"ok": True})

    async def mock_state(request):
        b = await request.json()
        for k in ("usb", "wifi"):
            if k in b:
                for kk, vv in b[k].items():
                    if isinstance(vv, dict) and isinstance(getattr(dev, k).get(kk), dict):
                        getattr(dev, k)[kk].update(vv)
                    else:
                        getattr(dev, k)[kk] = vv
        if "token" in b:
            dev.token = b["token"]
        if "ota_rollback" in b:
            dev.ota_rollback = bool(b["ota_rollback"])
        if "ota_fail" in b:
            dev.ota_fail = b["ota_fail"]
        if "push_error" in b:
            for c in list(dev.clients.values()):
                await dev.send(c, b["push_error"])
        dev.schedule_push()
        return web.json_response({"ok": True, "status": dev.status()})

    app = web.Application(middlewares=[mw], client_max_size=65536)
    app.router.add_get("/", index)
    app.router.add_get("/ws", dev.ws_handler)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/config", post_config)
    app.router.add_get("/api/wifi/scan", scan)
    app.router.add_post("/api/wake", wake)
    app.router.add_post("/api/reboot", reboot)
    app.router.add_post("/api/release", release)
    app.router.add_get("/api/keys", keys)
    app.router.add_get("/api/ota", ota_get)
    app.router.add_post("/api/ota", ota_post)
    app.router.add_get("/mock/log", mock_log)
    app.router.add_post("/mock/clear", mock_clear)
    app.router.add_post("/mock/state", mock_state)
    return app


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="")
    ap.add_argument("--ap-mode", action="store_true", help="simulate first-time setup (no STA connection)")
    ap.add_argument("--usb", choices=["mounted", "unmounted", "suspended"], default="mounted")
    ap.add_argument("--log", default=str(HERE / "mock_ws.log"), help="JSONL record file ('' to disable)")
    ap.add_argument("--speed", type=float, default=1.0, help="multiplier for simulated typing time")
    ap.add_argument("--scan-secs", type=float, default=1.5)
    ap.add_argument("--reboot-secs", type=float, default=3.0)
    ap.add_argument("--ota-rate", type=float, default=250000, help="simulated firmware upload speed in bytes/s (0 = unlimited)")
    args = ap.parse_args()
    if args.port < 1024:
        sys.exit("use a high port")
    dev = Device(args)
    app = make_app(dev)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    dev.runner = runner
    dev.site = web.TCPSite(runner, "127.0.0.1", args.port)
    await dev.site.start()
    print(f"mock ESPKB on http://127.0.0.1:{args.port}/ token={'set' if args.token else 'none'}", flush=True)
    asyncio.ensure_future(dev.worker())
    asyncio.ensure_future(dev.keepalive_loop())
    asyncio.ensure_future(dev.status_loop())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
