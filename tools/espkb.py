#!/usr/bin/env python3
"""espkb.py - client library and command-line tool for ESPKB (HTTP API v1).

ESPKB is an ESP32-S3 that receives commands over WiFi and acts as a USB
keyboard + relative mouse on the computer its USB port is plugged into.
This single file has no third-party dependencies (Python 3.8+); copy it
anywhere.

Library::

    from espkb import ESPKB, ESPKBError
    kb = ESPKB("10.42.77.20")            # or ESPKB() to use $ESPKB_HOST / espkb.local
    kb.type("echo hello\\n")
    kb.key("ctrl+alt+t")
    kb.move(120, -40); kb.click("left")
    kb.seq([{"key": "gui+r"}, {"delay": 400}, {"text": "notepad\\n"}])
    try:
        kb.key("nosuchkey")
    except ESPKBError as e:
        print(e.status, e.code, e.message, e.index)

Command line: run ``espkb.py --help``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0"
API_VERSION = 1
DEFAULT_HOST = "espkb.local"
MAX_BODY = 65536
MAX_STEPS = 512
BUTTONS = ("left", "right", "middle", "back", "forward")
INT_CONFIG_FIELDS = ("key_delay_ms",)
CONFIG_FIELDS = ("hostname", "wifi_ssid", "wifi_password", "ap_password", "token", "key_delay_ms")

EXIT_OK = 0
EXIT_DEVICE_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_NOT_READY = 4


class ESPKBError(Exception):
    """An error reported by the device, or a failure to talk to it.

    Attributes:
      status  HTTP status (None when the device could not be reached)
      code    the device's error code ("bad_step", "busy", "timeout", ...) or a
              client-side code: "unreachable", "client_timeout", "bad_response",
              "not_ready"
      message human readable explanation
      body    the full JSON error object (dict), if any
      index   step index for bad_step errors (or None)
      offset  byte offset of an unsupported character in text (or None)
    """

    def __init__(self, message, status=None, code="error", body=None):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body if isinstance(body, dict) else {}
        self.index = self.body.get("index")
        self.offset = self.body.get("offset")

    def __str__(self):
        s = self.code
        if self.status is not None:
            s += " (HTTP %d)" % self.status
        s += ": " + (self.message or "")
        extra = ["%s=%s" % (k, self.body[k]) for k in ("index", "offset") if k in self.body]
        if extra:
            s += " [" + " ".join(extra) + "]"
        return s


class ESPKBConnectionError(ESPKBError):
    """The device could not be reached or did not answer in time."""


def estimate_ms(steps, key_delay_ms=10):
    """Rough upper estimate of how long the device needs to execute steps (ms).

    Used to size HTTP timeouts for synchronous jobs.  key_delay_ms is the
    assumed device default for steps without delay_ms (the factory default is
    5; 10 is used to stay on the safe side).
    """
    per_report = 2.5  # USB polling + firmware overhead per report
    total = 0.0
    for s in steps if isinstance(steps, list) else []:
        if not isinstance(s, dict):
            continue
        try:
            d = s.get("delay_ms", key_delay_ms)
            d = d if isinstance(d, int) else key_delay_ms
            if "text" in s:
                total += 2 * len(str(s["text"])) * (d + per_report)
            elif "key" in s:
                parts = str(s["key"]).count("+") + 1
                rep = s.get("repeat", 1) if isinstance(s.get("repeat", 1), int) else 1
                hold = s.get("hold_ms", d) if isinstance(s.get("hold_ms", d), int) else d
                total += rep * (2 * parts * (d + per_report) + hold)
            elif "move" in s:
                mv = s["move"]
                step = s.get("step", 127) if isinstance(s.get("step", 127), int) and s.get("step", 127) > 0 else 127
                n = math.ceil(max(abs(int(mv[0])), abs(int(mv[1]))) / step)
                total += n * (s.get("delay_ms", 0) + per_report)
            elif "click" in s:
                total += s.get("count", 1) * (s.get("hold_ms", 20) + s.get("interval_ms", 60) + 2 * per_report)
            elif "scroll" in s:
                v = s["scroll"]
                m = abs(int(v)) if isinstance(v, int) else max(abs(int(v[0])), abs(int(v[1])))
                total += math.ceil(m / 127.0) * per_report
            elif "delay" in s:
                total += int(s["delay"])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return int(total)


def _base_url(host):
    h = (host or "").strip()
    if not h:
        raise ValueError("no device host given")
    if "://" not in h:
        h = "http://" + h
    u = urllib.parse.urlsplit(h)
    if not u.netloc:
        raise ValueError("bad host %r" % host)
    return "%s://%s" % (u.scheme, u.netloc)


def split_text(text, max_body=60000):
    """Split text into chunks whose JSON request body stays below max_body bytes.
    Never splits a "\\r\\n" pair (it must stay one Enter)."""
    chunks = []
    cur = []
    size = 12  # {"text":""}
    for i, ch in enumerate(text):
        n = len(json.dumps(ch, ensure_ascii=False).encode("utf-8")) - 2
        if size + n > max_body and cur and not (ch == "\n" and cur[-1] == "\r"):
            chunks.append("".join(cur))
            cur, size = [], 12
        cur.append(ch)
        size += n
    if cur or not chunks:
        chunks.append("".join(cur))
    return chunks


class ESPKB:
    """Client for one ESPKB device.

    host:       IP, hostname or URL ("10.42.77.20", "espkb.local", "http://x:80").
                Defaults to $ESPKB_HOST, then "espkb.local".
    token:      API token if the device has one configured (default $ESPKB_TOKEN).
    timeout:    base timeout in seconds; synchronous action requests add an
                estimate of the job's duration on top of it.
    token_mode: how the token is sent: "bearer" (Authorization header, default),
                "header" (X-Token) or "query" (?token=).

    Action methods take wait=True (default): the call returns after the device
    has executed the job, with {"ok":true,"job":..,"elapsed_ms":..}.  With
    wait=False the job is queued (?async=1) and the call returns
    {"ok":true,"job":..,"queued":true} immediately.
    Every method raises ESPKBError on failure.
    """

    def __init__(self, host=None, token=None, timeout=10.0, token_mode="bearer"):
        self.host = host or os.environ.get("ESPKB_HOST") or DEFAULT_HOST
        self.base_url = _base_url(self.host)
        self.token = token if token is not None else (os.environ.get("ESPKB_TOKEN") or None)
        self.timeout = float(timeout)
        if token_mode not in ("bearer", "header", "query"):
            raise ValueError("token_mode must be bearer, header or query")
        self.token_mode = token_mode
        # Never route device traffic through an HTTP proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self):
        return "ESPKB(%r)" % self.base_url

    # -- transport ---------------------------------------------------------
    def request(self, method, path, body=None, params=None, timeout=None, content_type=None,
                headers=None, auth=True):
        """Send one request and return the decoded JSON object (on success).

        body: dict/list (sent as JSON), str (sent as text/plain unless
        content_type is given) or bytes.  Raises ESPKBError for HTTP errors,
        ok:false answers, non-JSON answers and connection failures.
        """
        params = dict(params or {})
        hdrs = {"Accept": "application/json", "User-Agent": "espkb.py/%s" % __version__}
        if self.token and auth:
            if self.token_mode == "bearer":
                hdrs["Authorization"] = "Bearer " + self.token
            elif self.token_mode == "header":
                hdrs["X-Token"] = self.token
            else:
                params["token"] = self.token
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                hdrs["Content-Type"] = content_type or "application/json"
            elif isinstance(body, str):
                data = body.encode("utf-8")
                hdrs["Content-Type"] = content_type or "text/plain; charset=utf-8"
            else:
                data = bytes(body)
                if content_type:
                    hdrs["Content-Type"] = content_type
        elif method.upper() == "POST":
            data = b""
        if headers:
            hdrs.update(headers)
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        t = self.timeout if timeout is None else float(timeout)
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
        try:
            with self._opener.open(req, timeout=t) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except Exception:
                raw = b""
        except (socket.timeout, TimeoutError) as e:
            raise ESPKBConnectionError("no answer from %s within %.1f s (%s)" % (self.base_url, t, e),
                                       code="client_timeout")
        except urllib.error.URLError as e:
            reason = e.reason
            code = "client_timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "unreachable"
            raise ESPKBConnectionError("cannot reach %s: %s" % (self.base_url, reason), code=code)
        except (OSError, ValueError) as e:
            raise ESPKBConnectionError("cannot reach %s: %s" % (self.base_url, e), code="unreachable")
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            obj = None
        if not isinstance(obj, dict):
            raise ESPKBError("HTTP %d with a non-JSON answer: %r" % (status, raw[:200]), status, "bad_response")
        if status != 200 or obj.get("ok") is not True:
            raise ESPKBError(str(obj.get("message") or obj.get("error") or "request failed"), status,
                             str(obj.get("error") or "error"), obj)
        return obj

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def _action(self, path, body, wait=True, timeout=None, steps=None):
        params = {} if wait else {"async": "1"}
        if timeout is None:
            if wait:
                est = estimate_ms(steps if steps is not None else [body])
                timeout = self.timeout + 1.5 * est / 1000.0 + (5.0 if est > 1000 else 0.0)
            else:
                timeout = self.timeout
        return self.request("POST", path, body, params=params, timeout=timeout)

    # -- read-only endpoints -----------------------------------------------
    def status(self, timeout=None):
        """GET /api/status (device, usb, hid, wifi, ws_clients, auth_required)."""
        return self.get("/api/status", timeout=timeout)

    def info(self):
        """GET /api: self-description (name, api, version, endpoints, steps)."""
        return self.get("/api")

    def keys(self):
        """GET /api/keys: {"keys": {name: usage}, "buttons": [...]}."""
        return self.get("/api/keys")

    def get_config(self):
        """GET /api/config (passwords and token are never returned)."""
        return self.get("/api/config")

    def set_config(self, **fields):
        """POST /api/config with the given fields; returns {"ok":..,"reboot_required":..}."""
        return self.post("/api/config", fields)

    def wifi_scan(self, timeout=20.0):
        """GET /api/wifi/scan: {"aps": [{"ssid","rssi","channel","secure"}]} (takes ~3 s)."""
        return self.get("/api/wifi/scan", timeout=max(timeout, self.timeout))

    # -- actions -----------------------------------------------------------
    def type(self, text, delay_ms=None, enter=False, wait=True, plain=False, timeout=None):
        """Type text on a US layout (\\n = Enter, \\t = Tab, \\b = Backspace, \\x1b = Esc).

        Long texts are split into several jobs automatically so each request
        stays under the 64 KB body limit.  plain=True sends text/plain.
        """
        if enter:
            text += "\n"
        chunks = split_text(text) if not plain else [text]
        res = None
        elapsed = 0
        for chunk in chunks:
            if plain:
                params = {} if wait else {"async": "1"}
                if delay_ms is not None:
                    raise ValueError("delay_ms cannot be combined with plain=True")
                t = timeout or self.timeout + 1.5 * estimate_ms([{"text": chunk}]) / 1000.0
                res = self.request("POST", "/api/type", chunk, params=params, timeout=t,
                                   content_type="text/plain; charset=utf-8")
            else:
                body = {"text": chunk}
                if delay_ms is not None:
                    body["delay_ms"] = delay_ms
                res = self._action("/api/type", body, wait, timeout)
            elapsed += res.get("elapsed_ms", 0) or 0
        if res is not None and len(chunks) > 1 and "elapsed_ms" in res:
            res = dict(res, elapsed_ms=elapsed, jobs=len(chunks))
        return res

    def key(self, key, repeat=None, hold_ms=None, delay_ms=None, wait=True, timeout=None):
        """Tap a key or combo ("enter", "ctrl+alt+del", 40, "0x28"): press in order,
        hold, release in reverse.  repeat 1-1000."""
        body = {"key": key}
        for k, v in (("repeat", repeat), ("hold_ms", hold_ms), ("delay_ms", delay_ms)):
            if v is not None:
                body[k] = v
        return self._action("/api/key", body, wait, timeout)

    def key_down(self, key, ttl_ms=None, wait=True, timeout=None):
        """Press and hold.  HTTP-held keys are released automatically 5000 ms (or
        ttl_ms) after the last HTTP action request."""
        body = {"key": key, "action": "down"}
        if ttl_ms is not None:
            body["ttl_ms"] = ttl_ms
        return self._action("/api/key", body, wait, timeout)

    def key_up(self, key, wait=True, timeout=None):
        return self._action("/api/key", {"key": key, "action": "up"}, wait, timeout)

    def move(self, dx, dy, step=None, delay_ms=None, wait=True, timeout=None):
        """Relative mouse move (dx right, dy down, each within +-32767)."""
        body = {"move": [int(dx), int(dy)]}
        if step is not None:
            body["step"] = step
        if delay_ms is not None:
            body["delay_ms"] = delay_ms
        return self._action("/api/mouse", body, wait, timeout)

    def click(self, button="left", count=None, hold_ms=None, interval_ms=None, wait=True, timeout=None):
        body = {"click": button}
        for k, v in (("count", count), ("hold_ms", hold_ms), ("interval_ms", interval_ms)):
            if v is not None:
                body[k] = v
        return self._action("/api/mouse", body, wait, timeout)

    def button_down(self, button="left", ttl_ms=None, wait=True, timeout=None):
        body = {"button": button, "action": "down"}
        if ttl_ms is not None:
            body["ttl_ms"] = ttl_ms
        return self._action("/api/mouse", body, wait, timeout)

    def button_up(self, button="left", wait=True, timeout=None):
        return self._action("/api/mouse", {"button": button, "action": "up"}, wait, timeout)

    def scroll(self, v, h=0, wait=True, timeout=None):
        """Scroll: v > 0 up, v < 0 down; h > 0 right."""
        body = {"scroll": int(v)} if not h else {"scroll": [int(v), int(h)]}
        return self._action("/api/mouse", body, wait, timeout)

    def seq(self, steps, wait=True, timeout=None):
        """Run a list of steps as one job (max 512 steps, 64 KB)."""
        if isinstance(steps, dict) and "steps" in steps:
            steps = steps["steps"]
        if isinstance(steps, dict):
            steps = [steps]
        return self._action("/api/seq", {"steps": list(steps)}, wait, timeout, steps=list(steps))

    def release(self):
        """Emergency stop: abort the running job, flush the queue, release everything.
        Returns {"ok":true,"aborted":n}."""
        return self.post("/api/release")

    def wake(self):
        """Send USB remote wakeup if the host is suspended (ok otherwise too)."""
        return self.post("/api/wake")

    def reboot(self):
        return self.post("/api/reboot")

    # -- helpers -----------------------------------------------------------
    def wait_ready(self, timeout=30.0, require_usb=True, interval=0.5):
        """Block until /api/status answers (and usb.mounted is true, unless
        require_usb=False).  Returns the status; raises ESPKBError(code
        "not_ready") on timeout, or immediately on "unauthorized"."""
        deadline = time.monotonic() + timeout
        last = "no answer"
        while True:
            left = deadline - time.monotonic()
            try:
                st = self.status(timeout=max(0.5, min(3.0, left)))
                if not require_usb or st.get("usb", {}).get("mounted"):
                    return st
                last = "device answers but USB is not mounted by the host"
            except ESPKBError as e:
                if e.code in ("unauthorized", "forbidden"):
                    raise
                last = str(e)
            if time.monotonic() + interval > deadline:
                raise ESPKBError("device not ready within %.0f s: %s" % (timeout, last), None, "not_ready")
            time.sleep(interval)

    def wait_idle(self, timeout=60.0, interval=0.1):
        """Poll /api/status until no job runs and none is queued."""
        deadline = time.monotonic() + timeout
        while True:
            st = self.status()
            h = st.get("hid", {})
            if not h.get("busy") and not h.get("queued"):
                return st
            if time.monotonic() > deadline:
                raise ESPKBError("device still busy after %.0f s" % timeout, None, "not_ready")
            time.sleep(interval)

    def reboot_and_wait(self, timeout=60.0):
        """Reboot and wait until the device is back (uptime restarted)."""
        try:
            before = self.status().get("device", {}).get("uptime_ms")
        except ESPKBError:
            before = None
        self.reboot()
        time.sleep(1.5)
        deadline = time.monotonic() + timeout
        while True:
            st = self.wait_ready(timeout=max(1.0, deadline - time.monotonic()))
            up = st.get("device", {}).get("uptime_ms")
            if before is None or up is None or up < before:
                return st
            if time.monotonic() > deadline:
                raise ESPKBError("device did not reboot", None, "not_ready")
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

STEP_HELP = """\
Step reference (one JSON object per step, exactly one verb each):
  {"text":"ls -l\\n"}                 type text (US layout; \\n Enter, \\t Tab, \\b Backspace, \\u001b Esc)
                                     optional delay_ms (between reports; default key_delay_ms=5)
  {"key":"ctrl+alt+del"}             tap key/combo; optional hold_ms, repeat (1-1000), delay_ms
  {"key":"shift","action":"down"}    hold ("down") / release ("up"); optional ttl_ms (1-600000)
  {"move":[dx,dy]}                   relative move, +-32767; optional step (1-127), delay_ms
  {"click":"left"}                   click; optional count (1-100), hold_ms (20), interval_ms (60)
  {"button":"left","action":"down"}  hold/release a mouse button (action required)
  {"scroll":3} / {"scroll":[v,h]}    v>0 up, v<0 down, h>0 right; +-32767
  {"delay":250}                      wait 0-60000 ms
  {"release":true}                   release all keys and buttons (queued step)
Keys: names (case-insensitive) like a-z 0-9 enter esc tab space backspace f1-f24 up down left right
  home end pageup pagedown insert delete ctrl shift alt gui rctrl rshift ralt rgui menu kp_0-kp_9 ...,
  combos joined with "+", or a HID usage number (40) / "0x28".  "espkb.py keys" lists all names.
  There is no "+" key name: type it with {"text":"+"} or "shift+=".
Buttons: left right middle back forward.  Limits: 512 steps and 64 KB per request."""

EPILOG = """\
examples:
  espkb.py status
  espkb.py type 'echo hello' --enter
  echo 'some text' | espkb.py type          (stdin; its trailing newline becomes Enter)
  espkb.py type -e 'line1\\nline2\\n'          (-e interprets \\n \\t \\b \\e \\\\)
  espkb.py key ctrl+alt+t
  espkb.py key down --repeat 5               (arrow down five times)
  espkb.py down shift ; espkb.py up shift
  espkb.py move 120 -40 ; espkb.py click ; espkb.py click right --count 2
  espkb.py scroll -5                         (scroll down)
  espkb.py seq '[{"key":"gui+r"},{"delay":400},{"text":"notepad\\n"}]'
  espkb.py seq steps.json                    (file, or - for stdin)
  espkb.py release                           (emergency stop: abort jobs, release all keys)
  espkb.py config set key_delay_ms=10 ; espkb.py config get
  espkb.py --json status                     (raw JSON output)

device address: --host or $ESPKB_HOST (default espkb.local); token: --token or $ESPKB_TOKEN.
Global options (--host --token --json --timeout --async) may be given before or after the command.
Action commands wait until the device has executed the job (synchronous); --async only queues it.

exit codes:
  0 success
  1 the device returned an error (code and message on stderr, e.g. "bad_step (HTTP 400): ...")
  2 usage error (bad arguments)
  3 the device could not be reached or did not answer in time
  4 wait-ready / reboot --wait timed out
"""


def _common_parser():
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("global options")
    g.add_argument("--host", default=argparse.SUPPRESS,
                   help="device address: IP, hostname or URL (default: $ESPKB_HOST or espkb.local)")
    g.add_argument("--token", default=argparse.SUPPRESS, help="API token (default: $ESPKB_TOKEN)")
    g.add_argument("--token-mode", choices=("bearer", "header", "query"), default=argparse.SUPPRESS,
                   help="send the token as Authorization: Bearer (default), X-Token header or ?token=")
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="print the device's raw JSON answer instead of a summary")
    g.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="S",
                   help="seconds to wait (default: automatic, sized to the job; wait-ready: 30)")
    g.add_argument("--async", dest="no_wait", action="store_true", default=argparse.SUPPRESS,
                   help="queue the job and return immediately (?async=1) instead of waiting for it")
    return p


def _unescape(s):
    out = []
    i = 0
    table = {"n": "\n", "t": "\t", "b": "\b", "e": "\x1b", "r": "\r", "\\": "\\", "0": "\0"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in table:
                out.append(table[n])
                i += 2
                continue
            if n == "x" and i + 3 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(c)
        i += 1
    return "".join(out)


def _fmt_uptime(ms):
    try:
        s = int(ms) // 1000
    except (TypeError, ValueError):
        return "?"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return ("%dd" % d if d else "") + ("%dh" % h if h or d else "") + "%dm%02ds" % (m, s)


def format_status(st):
    d = st.get("device", {}) or {}
    u = st.get("usb", {}) or {}
    h = st.get("hid", {}) or {}
    w = st.get("wifi", {}) or {}
    yn = lambda v: "yes" if v else "no"  # noqa: E731
    leds = ",".join(k for k, v in sorted((u.get("leds") or {}).items()) if v) or "none"
    lines = [
        "device : %s %s (api %s), uptime %s, heap free %s (min %s), reset reason %s" % (
            d.get("name", "?"), d.get("version", "?"), d.get("api", "?"), _fmt_uptime(d.get("uptime_ms")),
            d.get("heap_free", "?"), d.get("heap_min", "?"), d.get("reset_reason", "?")),
        "usb    : %s, %s, remote wakeup %s, LEDs %s, protocol kbd=%s mouse=%s" % (
            "mounted" if u.get("mounted") else "NOT MOUNTED", "SUSPENDED" if u.get("suspended") else "awake",
            "allowed" if u.get("remote_wakeup") else "not allowed", leds, u.get("kbd_protocol", "?"),
            u.get("mouse_protocol", "?")),
        "hid    : %s, %s queued, keys held %s, buttons held %s, reports sent %s, jobs %s done / %s failed" % (
            "BUSY" if h.get("busy") else "idle", h.get("queued", "?"), h.get("keys", []), h.get("buttons", []),
            h.get("reports_sent", "?"), h.get("jobs_done", "?"), h.get("jobs_failed", "?")),
        "wifi   : %s, AP %s, hostname %s" % (
            ("connected to %r ip %s rssi %s" % (w.get("ssid"), w.get("ip"), w.get("rssi")))
            if w.get("sta_connected") else "station not connected",
            ("on (%s, %s)" % (w.get("ap_ssid"), w.get("ap_ip"))) if w.get("ap_active") else "off",
            w.get("hostname", "?")),
        "clients: %s websocket, auth required: %s" % (st.get("ws_clients", "?"), yn(st.get("auth_required"))),
    ]
    return "\n".join(lines)


def _format_action(res):
    if res.get("queued"):
        return "queued (job %s)" % res.get("job")
    s = "ok"
    if "job" in res:
        s += " (job %s, %s ms)" % (res.get("job"), res.get("elapsed_ms"))
    if res.get("jobs"):
        s += " in %d jobs" % res["jobs"]
    return s


def _read_source(src):
    """seq source: '-' or None = stdin, inline JSON, or a file path."""
    if src in (None, "-"):
        return sys.stdin.read()
    s = src.lstrip()
    if s.startswith("[") or s.startswith("{"):
        return src
    with open(src, "r", encoding="utf-8") as f:
        return f.read()


def _parse_config_pairs(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError("expected key=value, got %r" % p)
        k, v = p.split("=", 1)
        k = k.strip()
        if k in INT_CONFIG_FIELDS:
            try:
                v = int(v)
            except ValueError:
                raise ValueError("%s needs an integer, got %r" % (k, v))
        out[k] = v
    return out


def build_parser():
    common = _common_parser()
    ap = argparse.ArgumentParser(
        prog="espkb.py", parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Control an ESPKB WiFi USB keyboard/mouse (ESP32-S3) over its HTTP API v1.\n"
                    "Everything typed or clicked happens on the computer the ESPKB's USB port is "
                    "plugged into.",
        epilog=EPILOG + "\n" + STEP_HELP)
    ap.add_argument("--version", action="version", version="espkb.py %s (API %d)" % (__version__, API_VERSION))
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    def add(name, help_, **kw):
        return sub.add_parser(name, parents=[common], help=help_, description=help_,
                              formatter_class=argparse.RawDescriptionHelpFormatter, **kw)

    add("status", "show device status (USB, held keys, WiFi, counters)")
    add("info", "show the device's self-description (GET /api)")
    p = add("type", "type text (argument words joined by spaces, or stdin)")
    p.add_argument("text", nargs="*", help="text to type; omit or use - to read stdin")
    p.add_argument("--enter", action="store_true", help="press Enter after the text")
    p.add_argument("-e", "--escapes", action="store_true", help=r"interpret \n \t \b \e \r \\ \xHH escapes")
    p.add_argument("--chomp", action="store_true", help="drop one trailing newline (useful with stdin)")
    p.add_argument("--delay-ms", type=int, help="ms between reports (default: device key_delay_ms)")
    p.add_argument("--plain", action="store_true", help="send as text/plain instead of JSON")
    p = add("key", "tap a key or combo, e.g. enter, ctrl+alt+del, f5, 40, 0x28",
            epilog="Keys are pressed in order and released in reverse order.  A number with two or more\n"
                   "digits (e.g. 40) is a HID usage code; a single digit is the digit key.\n"
                   "Keys:" + STEP_HELP.split("Keys:", 1)[1])
    p.add_argument("key", help="key name, combo joined with +, HID usage number or 0x..")
    p.add_argument("--repeat", type=int, help="tap N times (1-1000)")
    p.add_argument("--hold-ms", type=int, help="hold time before releasing")
    p.add_argument("--delay-ms", type=int, help="ms between reports")
    p = add("down", "press and hold a key/combo (released by 'up', 'release', or after 5 s without HTTP actions)")
    p.add_argument("key")
    p.add_argument("--ttl-ms", type=int, help="auto-release deadline in ms (1-600000; default 5000)")
    p = add("up", "release a held key/combo")
    p.add_argument("key")
    p = add("move", "move the mouse relatively (dx right, dy down; negative values allowed)")
    p.add_argument("dx", type=int)
    p.add_argument("dy", type=int)
    p.add_argument("--step", type=int, help="max movement per report, 1-127 (default 127)")
    p.add_argument("--delay-ms", type=int, help="ms between reports (default 0)")
    p = add("click", "click a mouse button (default left)")
    p.add_argument("button", nargs="?", default="left", choices=BUTTONS)
    p.add_argument("--count", type=int, help="number of clicks, 1-100 (2 = double click)")
    p.add_argument("--hold-ms", type=int, help="press duration (default 20)")
    p.add_argument("--interval-ms", type=int, help="pause between clicks (default 60)")
    p = add("mdown", "press and hold a mouse button (default left)")
    p.add_argument("button", nargs="?", default="left", choices=BUTTONS)
    p.add_argument("--ttl-ms", type=int, help="auto-release deadline in ms (1-600000; default 5000)")
    p = add("mup", "release a held mouse button (default left)")
    p.add_argument("button", nargs="?", default="left", choices=BUTTONS)
    p = add("scroll", "scroll the wheel: V > 0 up, V < 0 down; optional H > 0 right")
    p.add_argument("v", type=int)
    p.add_argument("h", type=int, nargs="?", default=0)
    p = add("seq", "run a JSON list of steps as one job", epilog=STEP_HELP)
    p.add_argument("source", nargs="?", default="-",
                   help='inline JSON (\'[{"key":"a"}]\'), a file path, or - for stdin (default)')
    add("release", "EMERGENCY STOP: abort running/queued jobs and release all keys and buttons")
    add("wake", "send USB remote wakeup to a suspended host")
    add("keys", "list all key names with their HID usage codes, and the mouse button names")
    p = add("config", "show or change configuration",
            epilog="fields: hostname, wifi_ssid, wifi_password, ap_password, token (\"\" disables auth), "
                   "key_delay_ms (0-1000).\nexamples:  config get   |   config set key_delay_ms=10 hostname=espkb2")
    p.add_argument("action", choices=("get", "set"))
    p.add_argument("pairs", nargs="*", metavar="key=value")
    add("wifi-scan", "scan for WiFi networks (takes a few seconds)")
    p = add("reboot", "reboot the device")
    p.add_argument("--wait", action="store_true", help="wait until it is back (uses --timeout, default 60)")
    p = add("wait-ready", "wait until the device answers and the USB host has mounted it")
    p.add_argument("--no-usb", action="store_true", help="only wait for the device to answer")
    return ap


def _emit(args, obj, human):
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=None if isinstance(obj, dict) else 1))
    else:
        print(human)


def run(args, kb):
    cmd = args.cmd
    wait = not getattr(args, "no_wait", False)
    tmo = getattr(args, "timeout", None)
    if cmd == "status":
        st = kb.status(timeout=tmo)
        _emit(args, st, format_status(st))
    elif cmd == "info":
        d = kb.info()
        lines = ["%s api %s version %s" % (d.get("name"), d.get("api"), d.get("version"))]
        for e in d.get("endpoints", []):
            lines.append("  %-5s %-18s %s" % (e.get("method"), e.get("path"), e.get("desc", "")))
        for s in d.get("steps", []):
            lines.append("  step: %s" % s)
        _emit(args, d, "\n".join(lines))
    elif cmd == "type":
        if not args.text or args.text == ["-"]:
            if sys.stdin is None or sys.stdin.isatty():
                raise UsageError("type: no text given (pass TEXT or pipe it on stdin)")
            text = sys.stdin.read()
        else:
            text = " ".join(args.text)
        if args.escapes:
            text = _unescape(text)
        if args.chomp and text.endswith("\n"):
            text = text[:-2] if text.endswith("\r\n") else text[:-1]
        try:
            res = kb.type(text, delay_ms=args.delay_ms, enter=args.enter, wait=wait, plain=args.plain, timeout=tmo)
        except ESPKBError as e:
            if e.code == "bad_step" and isinstance(e.offset, int):
                b = (text + ("\n" if args.enter else "")).encode("utf-8")
                ch = b[e.offset:e.offset + 4].decode("utf-8", "replace")[:1]
                e.message += " (character %r at byte offset %d; only printable ASCII, \\n \\r \\t \\b and Esc " \
                             "can be typed)" % (ch, e.offset)
            raise
        _emit(args, res, _format_action(res))
    elif cmd == "key":
        res = kb.key(_key_arg(args.key), args.repeat, args.hold_ms, args.delay_ms, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "down":
        res = kb.key_down(_key_arg(args.key), args.ttl_ms, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "up":
        res = kb.key_up(_key_arg(args.key), wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "move":
        res = kb.move(args.dx, args.dy, args.step, args.delay_ms, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "click":
        res = kb.click(args.button, args.count, args.hold_ms, args.interval_ms, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "mdown":
        res = kb.button_down(args.button, args.ttl_ms, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "mup":
        res = kb.button_up(args.button, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "scroll":
        res = kb.scroll(args.v, args.h, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "seq":
        raw = _read_source(args.source)
        try:
            steps = json.loads(raw)
        except ValueError as e:
            raise UsageError("seq: input is not valid JSON: %s" % e)
        if isinstance(steps, dict) and "steps" in steps:
            steps = steps["steps"]
        if isinstance(steps, dict):
            steps = [steps]
        if not isinstance(steps, list):
            raise UsageError("seq: expected a JSON array of steps or {\"steps\": [...]}")
        res = kb.seq(steps, wait=wait, timeout=tmo)
        _emit(args, res, _format_action(res))
    elif cmd == "release":
        res = kb.release()
        _emit(args, res, "released everything (%s job(s) aborted)" % res.get("aborted", 0))
    elif cmd == "wake":
        res = kb.wake()
        _emit(args, res, "ok")
    elif cmd == "keys":
        res = kb.keys()
        by_usage = {}
        for name, u in (res.get("keys") or {}).items():
            by_usage.setdefault(u, []).append(name)
        lines = ["%3s 0x%02x  %s" % (u, u, ", ".join(sorted(n))) for u, n in sorted(by_usage.items(), key=lambda x: x[0])]
        lines.append("mouse buttons: " + ", ".join(res.get("buttons", [])))
        _emit(args, res, "\n".join(lines))
    elif cmd == "config":
        if args.action == "get":
            if args.pairs:
                raise UsageError("config get takes no arguments")
            res = kb.get_config()
            _emit(args, res, "\n".join("%s = %s" % (k, json.dumps(v)) for k, v in res.items() if k != "ok"))
        else:
            if not args.pairs:
                raise UsageError("config set needs key=value pairs (fields: %s)" % ", ".join(CONFIG_FIELDS))
            try:
                fields = _parse_config_pairs(args.pairs)
            except ValueError as e:
                raise UsageError(str(e))
            res = kb.set_config(**fields)
            _emit(args, res, "saved" + (" (reboot required to take effect)" if res.get("reboot_required") else ""))
    elif cmd == "wifi-scan":
        res = kb.wifi_scan(timeout=tmo or 20.0)
        lines = ["%-32s %5s %3s %s" % ("SSID", "RSSI", "CH", "SECURE")]
        for a in sorted(res.get("aps", []), key=lambda a: -(a.get("rssi") or -999)):
            lines.append("%-32s %5s %3s %s" % (a.get("ssid"), a.get("rssi"), a.get("channel"),
                                               "yes" if a.get("secure") else "no"))
        _emit(args, res, "\n".join(lines))
    elif cmd == "reboot":
        if args.wait:
            st = kb.reboot_and_wait(timeout=tmo or 60.0)
            _emit(args, st, "rebooted; device is back (uptime %s)" % _fmt_uptime(st.get("device", {}).get("uptime_ms")))
        else:
            res = kb.reboot()
            _emit(args, res, "rebooting")
    elif cmd == "wait-ready":
        st = kb.wait_ready(timeout=tmo or 30.0, require_usb=not args.no_usb)
        _emit(args, st, "ready: " + format_status(st).splitlines()[1].split(":", 1)[1].strip())
    else:  # pragma: no cover
        raise UsageError("unknown command %s" % cmd)
    return EXIT_OK


def _key_arg(s):
    """CLI key argument: plain decimal numbers become integer usages."""
    if s.isdigit() and len(s) > 1:
        return int(s)
    return s


class UsageError(Exception):
    pass


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        kb = ESPKB(getattr(args, "host", None), getattr(args, "token", None),
                   token_mode=getattr(args, "token_mode", "bearer"))
        if getattr(args, "timeout", None):
            kb.timeout = args.timeout
    except ValueError as e:
        sys.stderr.write("espkb: error: %s\n" % e)
        return EXIT_USAGE
    try:
        return run(args, kb)
    except UsageError as e:
        sys.stderr.write("espkb: error: %s\n" % e)
        return EXIT_USAGE
    except ESPKBError as e:
        if getattr(args, "json", False) and e.body:
            print(json.dumps(e.body, ensure_ascii=False))
        sys.stderr.write("espkb: error: %s\n" % e)
        if e.code == "not_ready":
            return EXIT_NOT_READY
        if isinstance(e, ESPKBConnectionError) or e.status is None:
            return EXIT_UNREACHABLE
        return EXIT_DEVICE_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("espkb: interrupted (a job already sent keeps running; use 'espkb.py release')\n")
        return 130
    except OSError as e:
        sys.stderr.write("espkb: error: %s\n" % e)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
