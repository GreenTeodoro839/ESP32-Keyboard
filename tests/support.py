"""Shared helpers for the ESPKB hardware test suite (imported by conftest.py and tests).

Nothing here touches the network or USB at import time.
"""
from __future__ import annotations

import contextlib
import json
import os
import statistics
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import espkb
import hidhost

HERE = os.path.dirname(os.path.abspath(__file__))
TESTBED_ENV = os.path.join(HERE, "testbed.env")
DEVICE_ENV = os.path.join(HERE, "device.env")

#: tolerance for host-side timing assertions (USB polling + libusb callback latency)
TIMING_TOL_MS = float(os.environ.get("ESPKB_TIMING_TOL_MS", "4"))

ALT_USAGES = {226, 230}
FORBIDDEN_USAGES = {hidhost.USAGE_POWER}


# ---------------------------------------------------------------------------
# bench / device address
# ---------------------------------------------------------------------------

def load_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def bench_env() -> Dict[str, str]:
    """tests/testbed.env, overridable by environment variables of the same names."""
    env = load_env(TESTBED_ENV)
    for k in list(env) + ["ESPKB_SERIAL_PORT", "ESPKB_USB_VID", "ESPKB_USB_PID", "ESPKB_TEST_SSID",
                          "ESPKB_TEST_PSK", "ESPKB_NM_CONN", "ESPKB_NM_IFACE", "ESPKB_HOTSPOT_GW"]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def device_host() -> Optional[str]:
    """ESPKB_HOST from the environment, falling back to tests/device.env."""
    return os.environ.get("ESPKB_HOST") or load_env(DEVICE_ENV).get("ESPKB_HOST") or None


def host_only(host: str) -> str:
    """'http://10.0.0.5:80' -> '10.0.0.5:80'; '10.0.0.5' -> '10.0.0.5'."""
    return espkb._base_url(host).split("://", 1)[1]


def is_ip(host: str) -> bool:
    h = host_only(host).split(":")[0]
    parts = h.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ---------------------------------------------------------------------------
# defence in depth: never emit Power (0x66) or Alt+PrintScreen
# ---------------------------------------------------------------------------

def _steps_of(body: Any) -> List[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if isinstance(body.get("steps"), list):
            return body["steps"]
        return [body]
    return []


def check_forbidden(body: Any, held: Iterable[int] = ()) -> None:
    """Raise AssertionError if a request body could press Power or Alt+PrintScreen
    (also considering keys currently held on the host side)."""
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return
    held = set(held)
    for s in _steps_of(body):
        if not isinstance(s, dict) or "key" not in s:
            continue
        try:
            usages = set(hidhost.parse_key_spec(s["key"]))
        except (hidhost.EncodeError, TypeError, ValueError):
            continue
        pressed = usages | held
        if usages & FORBIDDEN_USAGES:
            raise AssertionError("test tried to send keyboard usage 0x66 (Power): %r" % (s,))
        if hidhost.USAGE_PRINTSCREEN in pressed and pressed & ALT_USAGES:
            raise AssertionError("test tried to send an Alt+PrintScreen combination: %r" % (s,))


def forbidden_in_reports(reports: Iterable[Any]) -> List[str]:
    out = []
    for r in reports:
        if getattr(r, "ep", "kbd") != "kbd" or len(r.data) != 8:
            continue
        p = hidhost.parse_kbd(r.data).pressed
        if p & FORBIDDEN_USAGES:
            out.append("report %d contains usage 0x66 (Power): %s" % (r.seq, r.data.hex(" ")))
        if hidhost.USAGE_PRINTSCREEN in p and p & ALT_USAGES:
            out.append("report %d is an Alt+PrintScreen combination: %s" % (r.seq, r.data.hex(" ")))
    return out


# ---------------------------------------------------------------------------
# guarded HTTP clients
# ---------------------------------------------------------------------------

class GuardedESPKB(espkb.ESPKB):
    """espkb.ESPKB that asserts HID isolation before every non-GET request and
    refuses forbidden key combinations."""

    def __init__(self, host: str, hid: hidhost.HIDHost, **kw: Any) -> None:
        kw.setdefault("token", "")
        super().__init__(host, **kw)
        self.hid = hid

    def request(self, method, path, body=None, *a, **kw):  # type: ignore[override]
        if method.upper() != "GET":
            self.hid.assert_isolated()
            check_forbidden(body, self.hid.kbd_state().pressed)
        return super().request(method, path, body, *a, **kw)


class RawHTTP:
    """requests-based client for tests that need full control over headers,
    bodies and status codes.  Non-GET requests assert HID isolation first."""

    def __init__(self, host: str, dev: GuardedESPKB, hid: hidhost.HIDHost) -> None:
        import requests
        self.base = espkb._base_url(host)
        self.dev = dev
        self.hid = hid
        self.s = requests.Session()
        self.s.trust_env = False  # never use proxies from the environment

    def request(self, method: str, path: str, *, auth: bool = True, headers: Optional[Dict[str, str]] = None,
                data: Any = None, json_body: Any = None, params: Optional[Dict[str, str]] = None,
                timeout: float = 15.0):
        if method.upper() != "GET":
            self.hid.assert_isolated()
            check_forbidden(json_body if json_body is not None else data, self.hid.kbd_state().pressed)
        hdrs = {"Connection": "close"}
        if auth and self.dev.token:
            hdrs["Authorization"] = "Bearer " + self.dev.token
        hdrs.update(headers or {})
        return self.s.request(method, self.base + path, headers=hdrs, data=data, json=json_body,
                              params=params, timeout=timeout)

    def get(self, path: str, **kw: Any):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any):
        return self.request("POST", path, **kw)


def resp_json(resp) -> Dict[str, Any]:
    ct = resp.headers.get("Content-Type", "")
    assert "json" in ct.lower(), "response Content-Type is %r, expected JSON (status %d, body %r)" % (
        ct, resp.status_code, resp.content[:200])
    try:
        obj = resp.json()
    except ValueError:
        raise AssertionError("response is not valid JSON: %r" % resp.content[:200])
    assert isinstance(obj, dict), "response JSON is not an object: %r" % (obj,)
    return obj


def check_error_obj(obj: Dict[str, Any], codes: Iterable[str]) -> None:
    codes = [codes] if isinstance(codes, str) else list(codes)
    assert obj.get("ok") is False, "expected ok:false, got %r" % (obj,)
    assert obj.get("error") in codes, "error code %r, expected one of %s (%r)" % (obj.get("error"), codes, obj)
    assert isinstance(obj.get("message"), str) and obj["message"], "error without a message: %r" % (obj,)


def expect_http_error(resp, status: int, codes: Iterable[str], index: Optional[int] = None,
                      offset: Optional[int] = None) -> Dict[str, Any]:
    obj = resp_json(resp)
    assert resp.status_code == status, "HTTP %d, expected %d: %r" % (resp.status_code, status, obj)
    check_error_obj(obj, codes)
    if index is not None and obj.get("error") == "bad_step":
        assert obj.get("index") == index, "bad_step index %r, expected %d: %r" % (obj.get("index"), index, obj)
    if offset is not None:
        assert obj.get("offset") == offset, "offset %r, expected %d: %r" % (obj.get("offset"), offset, obj)
    return obj


def expect_error(fn: Callable[[], Any], status: int, codes: Iterable[str], index: Optional[int] = None,
                 offset: Optional[int] = None) -> espkb.ESPKBError:
    """Call fn (an espkb client call); assert it raises ESPKBError with this status/code."""
    codes = [codes] if isinstance(codes, str) else list(codes)
    try:
        res = fn()
    except espkb.ESPKBError as e:
        assert e.status == status, "HTTP %r, expected %d: %s" % (e.status, status, e)
        assert e.code in codes, "error %r, expected one of %s: %s" % (e.code, codes, e)
        assert e.message, "error without a message: %s" % e
        if index is not None and e.code == "bad_step":
            assert e.index == index, "bad_step index %r, expected %d: %s" % (e.index, index, e)
        if offset is not None:
            assert e.offset == offset, "offset %r, expected %d: %s" % (e.offset, offset, e)
        return e
    raise AssertionError("expected HTTP %d %s, got success %r" % (status, codes, res))


# Range errors inside a step: API.md lists both bad_request ("value range") and
# bad_step ("illegal step"); accept either (see the test report).
RANGE_CODES = ("bad_step", "bad_request")


# ---------------------------------------------------------------------------
# HID helpers
# ---------------------------------------------------------------------------

def do(hid: hidhost.HIDHost, action: Callable[[], Any], idle_ms: float = 150, min_count: int = 0,
       timeout: float = 30.0) -> Tuple[Any, List[hidhost.Report]]:
    """Run action and return (its result, all reports it produced)."""
    m = hid.mark()
    res = action()
    reps = hid.collect(m, idle_ms=idle_ms, timeout=timeout, min_count=min_count)
    return res, reps


def assert_nothing_emitted(hid: hidhost.HIDHost, mark: int, wait_ms: float = 300) -> None:
    hid.wait_idle(wait_ms, timeout=wait_ms / 1000.0 + 5)
    reps = hid.reports(mark)
    assert not reps, "expected no HID output, got %d report(s):\n%s" % (
        len(reps), "\n".join("  %s %s %s" % (r.ep, r.data.hex(" "), hidhost.describe(r.ep, r.data)) for r in reps[:20]))


def kbd(reps: Iterable[hidhost.Report]) -> List[hidhost.Report]:
    return [r for r in reps if r.ep == "kbd"]


def mouse(reps: Iterable[hidhost.Report]) -> List[hidhost.Report]:
    return [r for r in reps if r.ep == "mouse"]


def wait_status(dev: espkb.ESPKB, pred: Callable[[Dict[str, Any]], bool], timeout: float = 3.0,
                interval: float = 0.05) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        st = dev.status()
        if pred(st):
            return st
        if time.monotonic() > deadline:
            raise AssertionError("status condition not reached within %.1fs; last status: %s"
                                 % (timeout, json.dumps(st)[:1500]))
        time.sleep(interval)


def median(xs: List[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def time_of_first(reps: List[hidhost.Report], pred: Callable[[hidhost.Report], bool]) -> Optional[int]:
    for r in reps:
        if pred(r):
            return r.t_ns
    return None


def wait_kbd_state(hid: hidhost.HIDHost, pred: Callable[[hidhost.KbdReport], bool],
                   timeout: float) -> Optional[int]:
    """Wait until the host-side keyboard state satisfies pred; return monotonic_ns when seen."""
    if hid.wait_until(lambda: pred(hid.kbd_state()), timeout):
        r = hid.log.last("kbd")
        return r.t_ns if r else time.monotonic_ns()
    return None


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

def ws_url(host: str, path: str = "/ws", token: Optional[str] = None,
           query: Optional[Dict[str, str]] = None) -> str:
    import urllib.parse
    q = dict(query or {})
    if token is not None:
        q["token"] = token
    url = "ws://" + host_only(host) + path
    if q:
        url += "?" + urllib.parse.urlencode(q)
    return url


class WSMessage:
    __slots__ = ("t_ns", "obj", "raw")

    def __init__(self, t_ns: int, obj: Any, raw: Any) -> None:
        self.t_ns, self.obj, self.raw = t_ns, obj, raw

    def __repr__(self) -> str:
        return "WSMessage(%r)" % (self.obj,)


class WSClient:
    """websocket-client wrapper with a reader thread.

    Every received text frame is parsed as JSON and stored with its receive
    time (time.monotonic_ns).  Helpers wait for acks, pongs, errors and status
    pushes without consuming anything, so a test can inspect the full history.
    """

    def __init__(self, url: str, *, origin: Optional[str] = None, suppress_origin: bool = False,
                 headers: Optional[List[str]] = None, timeout: float = 5.0,
                 guard: Optional[Callable[[Any], None]] = None, connect: bool = True) -> None:
        self.url = url
        self.origin = origin
        self.suppress_origin = suppress_origin
        self.headers = headers
        self.timeout = timeout
        self.guard = guard
        self.ws = None
        self.messages: List[WSMessage] = []
        self.cond = threading.Condition()
        self.closed = True
        self.close_code: Optional[int] = None
        self.close_t_ns: Optional[int] = None
        self.last_send_ns = 0
        self._reader: Optional[threading.Thread] = None
        self._ka_stop = threading.Event()
        self._ka_thread: Optional[threading.Thread] = None
        self.handshake_error: Optional[Exception] = None
        if connect:
            self.connect()

    # -- connection --------------------------------------------------------
    def connect(self) -> "WSClient":
        import websocket
        opts: Dict[str, Any] = {}
        if self.origin is not None:
            opts["origin"] = self.origin
        if self.suppress_origin:
            opts["suppress_origin"] = True
        self.ws = websocket.create_connection(self.url, timeout=self.timeout, header=self.headers,
                                              enable_multithread=True, **opts)
        self.ws.settimeout(None)
        self.closed = False
        self.connected_ns = time.monotonic_ns()
        self._reader = threading.Thread(target=self._read_loop, name="ws-reader", daemon=True)
        self._reader.start()
        return self

    def try_connect(self) -> bool:
        """Connect; return False (and keep the exception) if the handshake fails."""
        try:
            self.connect()
            return True
        except Exception as e:  # websocket.WebSocketBadStatusException, OSError, ...
            self.handshake_error = e
            return False

    @property
    def handshake_status(self) -> Optional[int]:
        return getattr(self.handshake_error, "status_code", None)

    def _read_loop(self) -> None:
        import websocket
        ws = self.ws
        while True:
            try:
                opcode, data = ws.recv_data(control_frame=False)
            except (websocket.WebSocketConnectionClosedException, OSError, ValueError, AttributeError):
                break
            except Exception:
                break
            t = time.monotonic_ns()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                code = int.from_bytes(data[:2], "big") if data and len(data) >= 2 else None
                with self.cond:
                    self.close_code = code
                break
            if opcode == websocket.ABNF.OPCODE_TEXT:
                txt = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
                try:
                    obj = json.loads(txt)
                except ValueError:
                    obj = {"_unparsed": txt}
                msg = WSMessage(t, obj, txt)
            else:
                msg = WSMessage(t, {"_binary": data}, data)
            with self.cond:
                self.messages.append(msg)
                self.cond.notify_all()
        with self.cond:
            self.closed = True
            self.close_t_ns = time.monotonic_ns()
            self.cond.notify_all()

    # -- sending -----------------------------------------------------------
    def send(self, obj: Any) -> int:
        """Send a JSON message (or a raw str).  Returns the send time (monotonic_ns)."""
        if self.guard is not None and not (isinstance(obj, dict) and set(obj) == {"ping"}):
            self.guard(obj)
        txt = obj if isinstance(obj, str) else json.dumps(obj)
        self.ws.send(txt)
        self.last_send_ns = time.monotonic_ns()
        return self.last_send_ns

    def send_binary(self, data: bytes) -> int:
        if self.guard is not None:
            self.guard(data)
        self.ws.send_binary(data)
        self.last_send_ns = time.monotonic_ns()
        return self.last_send_ns

    def ping(self, value: Any = None, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        if value is None:
            value = "p%d" % time.monotonic_ns()
        start = len(self.messages)
        self.send({"ping": value})
        m = self.wait(lambda o: isinstance(o, dict) and "pong" in o and o["pong"] == value, timeout, start)
        return m.obj if m else None

    def start_keepalive(self, interval: float = 0.25) -> None:
        self._ka_stop.clear()

        def run() -> None:
            n = 0
            while not self._ka_stop.wait(interval):
                try:
                    n += 1
                    self.send({"ping": "ka%d" % n})
                except Exception:
                    return
        self._ka_thread = threading.Thread(target=run, name="ws-keepalive", daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        self._ka_stop.set()
        if self._ka_thread:
            self._ka_thread.join(timeout=2)
            self._ka_thread = None

    # -- receiving ---------------------------------------------------------
    def mark(self) -> int:
        with self.cond:
            return len(self.messages)

    def wait(self, pred: Callable[[Any], bool], timeout: float = 2.0, start: int = 0) -> Optional[WSMessage]:
        """First message at index >= start whose JSON object satisfies pred, or None."""
        deadline = time.monotonic() + timeout
        with self.cond:
            i = start
            while True:
                while i < len(self.messages):
                    m = self.messages[i]
                    i += 1
                    try:
                        if pred(m.obj):
                            return m
                    except Exception:
                        pass
                left = deadline - time.monotonic()
                if left <= 0 or (self.closed and i >= len(self.messages)):
                    return None
                self.cond.wait(min(left, 0.1))

    def wait_ack(self, ack_id: Any, timeout: float = 10.0, start: int = 0) -> Optional[WSMessage]:
        return self.wait(lambda o: isinstance(o, dict) and "ack" in o and o["ack"] == ack_id, timeout, start)

    def wait_closed(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.cond:
            while not self.closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 0.1))
            return True

    def select(self, key: str, start: int = 0) -> List[WSMessage]:
        with self.cond:
            return [m for m in self.messages[start:] if isinstance(m.obj, dict) and key in m.obj]

    def statuses(self, start: int = 0) -> List[WSMessage]:
        return self.select("status", start)

    def errors(self, start: int = 0) -> List[WSMessage]:
        """Unsolicited error messages ({"error":..} without "ack")."""
        return [m for m in self.select("error", start) if "ack" not in m.obj]

    def non_status(self, start: int = 0) -> List[WSMessage]:
        with self.cond:
            return [m for m in self.messages[start:]
                    if not (isinstance(m.obj, dict) and "status" in m.obj)
                    and not (isinstance(m.obj, dict) and "pong" in m.obj
                             and str(m.obj.get("pong", "")).startswith("ka"))]

    # -- closing -----------------------------------------------------------
    def close(self, code: int = 1000) -> None:
        self.stop_keepalive()
        if self.ws is not None:
            try:
                self.ws.close(status=code, timeout=1)
            except Exception:
                pass
        if self._reader:
            self._reader.join(timeout=2)

    def abort(self) -> None:
        """Drop the TCP connection without a WebSocket close frame."""
        import socket
        self.stop_keepalive()
        try:
            self.ws.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.ws.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# hotspot control (WiFi tests only)
# ---------------------------------------------------------------------------

def nmcli(*args: str, timeout: float = 45.0) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli"] + list(args), capture_output=True, text=True, timeout=timeout)


def hotspot_active(conn: str) -> bool:
    try:
        r = nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and conn in r.stdout.split("\n")


@contextlib.contextmanager
def hotspot_down(conn: str):
    """Take the test hotspot down for the duration of the block and ALWAYS bring
    it back up (the only network operation this suite performs)."""
    r = nmcli("connection", "down", conn)
    if r.returncode != 0:
        raise RuntimeError("nmcli connection down %s failed: %s" % (conn, r.stderr.strip()))
    try:
        yield
    finally:
        last = None
        for _ in range(3):
            last = nmcli("connection", "up", conn)
            if last.returncode == 0:
                break
            time.sleep(2)
        if last is None or last.returncode != 0:
            raise RuntimeError("could not bring the hotspot %s back up: %s" % (conn, last.stderr if last else ""))
