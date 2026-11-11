"""Shared fixtures for the ESPKB functional test suite.

Hardware fixtures (all session-scoped unless noted):
  bench            dict from tests/testbed.env (+ env overrides)
  device_host      device address from $ESPKB_HOST or tests/device.env (skips if absent)
  device_session   probes the device once (skips everything if unreachable)
  hid              hidhost.HIDHost: claims both HID interfaces, asserts isolation
                   (aborts the whole run if isolation cannot be established)
  dev              support.GuardedESPKB client (asserts isolation before every POST)
  raw              support.RawHTTP (requests) for header/body/status-level tests
  baseline_config  GET /api/config at session start (restored after every test)
  ws_factory       (function) make support.WSClient connections; all closed at teardown
  ws               (function) one connected WSClient
  enc              (function) hidhost.ReferenceEncoder matching the current host state
  serial_console   serialcon.SerialConsole (skips if the port is unusable or silent)
  wifi_restore     (function) re-writes the bench WiFi credentials at teardown
Every test outside tests/offline gets the autouse clean-state fixture: before and
after each test the device is released, verified idle with nothing held, the
report log is drained, and token / config / LEDs / protocols / paused endpoints
are restored.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "tools"), HERE):
    if os.path.abspath(_p) not in [os.path.abspath(x) for x in sys.path]:
        sys.path.insert(0, _p)

import espkb  # noqa: E402
import hidhost  # noqa: E402
import support  # noqa: E402

OFFLINE_DIR = os.path.join(HERE, "offline")


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
        if not os.path.abspath(path).startswith(OFFLINE_DIR + os.sep):
            item.add_marker(pytest.mark.hw)


# ---------------------------------------------------------------------------
# session fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def bench():
    return support.bench_env()


@pytest.fixture(scope="session")
def device_host():
    host = support.device_host()
    if not host:
        pytest.skip("no ESPKB device address: set ESPKB_HOST or create tests/device.env with ESPKB_HOST=<ip>")
    return host


@pytest.fixture(scope="session")
def device_session(device_host):
    """Probe the device once.  If it requires a token and ESPKB_TOKEN is set, the
    token is cleared for the run and restored at the end."""
    kb = espkb.ESPKB(device_host, token="", timeout=4)
    restore_token = None
    try:
        st = kb.status()
    except espkb.ESPKBError as e:
        if e.code == "unauthorized" and os.environ.get("ESPKB_TOKEN"):
            kb.token = os.environ["ESPKB_TOKEN"]
            try:
                kb.set_config(token="")
            except espkb.ESPKBError as e2:
                pytest.skip("device requires a token and ESPKB_TOKEN did not work: %s" % e2)
            restore_token = os.environ["ESPKB_TOKEN"]
            kb.token = ""
            st = kb.status()
        elif e.code == "unauthorized":
            pytest.skip("device at %s requires a token; set ESPKB_TOKEN (it is cleared for the run "
                        "and restored afterwards) or clear it with 'serialcon.py cmd \"token -\"'" % device_host)
        else:
            pytest.skip("ESPKB device not reachable at %s: %s" % (device_host, e))
    yield {"host": device_host, "status": st}
    if restore_token:
        try:
            espkb.ESPKB(device_host, token="", timeout=5).set_config(token=restore_token)
        except espkb.ESPKBError:
            pass


@pytest.fixture(scope="session")
def hid(device_session, bench):
    vid = int(bench.get("ESPKB_USB_VID", "303a"), 16)
    pid = int(bench.get("ESPKB_USB_PID", "4004"), 16)
    h = hidhost.HIDHost(vid, pid, verbose=bool(os.environ.get("ESPKB_HID_VERBOSE")))
    try:
        h.open(timeout=float(os.environ.get("ESPKB_HID_FIND_TIMEOUT", "15")))
    except hidhost.IsolationError as e:
        pytest.exit("REFUSING TO RUN: HID isolation could not be established: %s" % e, returncode=3)
    except hidhost.DeviceNotFound as e:
        pytest.skip("%s - is the native USB port plugged in and the firmware flashed?" % e)
    except hidhost.HIDHostError as e:
        pytest.skip("cannot open the HID device: %s" % e)
    try:
        h.assert_isolated()
    except hidhost.IsolationError as e:
        h.close()
        pytest.exit("REFUSING TO RUN: %s" % e, returncode=3)
    yield h
    h.close()


@pytest.fixture(scope="session")
def dev(device_session, hid):
    return support.GuardedESPKB(device_session["host"], hid, timeout=10)


@pytest.fixture(scope="session")
def raw(device_session, dev, hid):
    return support.RawHTTP(device_session["host"], dev, hid)


@pytest.fixture(scope="session")
def baseline_config(dev):
    return dev.get_config()


@pytest.fixture(scope="session")
def serial_console(bench, device_session):
    # depends on device_session so that a run without a reachable device never opens the port
    import serialcon
    port = bench.get("ESPKB_SERIAL_PORT")
    if not port or not os.path.exists(port):
        pytest.skip("serial port %r not present" % port)
    con = serialcon.SerialConsole(port)
    try:
        con.open()
    except serialcon.SerialConsoleError as e:
        pytest.skip(str(e))
    r = con.command("help", max_s=4.0, first_byte_timeout=3.0)
    if not r.got_data:
        con.close()
        pytest.skip("serial console on %s gave no data for 'help' (wrong UART, line ending, or firmware "
                    "not running); try 'serialcon.py --eol cr cmd help'" % port)
    yield con
    con.close()


# ---------------------------------------------------------------------------
# function fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_factory(device_session, hid):
    made = []

    def guard(obj):
        hid.assert_isolated()
        support.check_forbidden(obj, hid.kbd_state().pressed)

    def make(token=None, origin=None, suppress_origin=False, headers=None, connect=True, timeout=5.0,
             path="/ws", query=None):
        url = support.ws_url(device_session["host"], path, token, query)
        c = support.WSClient(url, origin=origin, suppress_origin=suppress_origin, headers=headers,
                             timeout=timeout, guard=guard, connect=False)
        made.append(c)
        if connect:
            hid.assert_isolated()
            c.connect()
        return c

    yield make
    for c in made:
        c.close()


@pytest.fixture
def ws(ws_factory):
    return ws_factory()


@pytest.fixture
def enc(hid, dev):
    """Reference encoder for the current host state (CapsLock LED, boot mouse protocol)."""
    cfg_delay = 5
    try:
        cfg_delay = dev.get_config().get("key_delay_ms", 5)
    except espkb.ESPKBError:
        pass
    return hidhost.ReferenceEncoder(caps=bool(hid.leds & hidhost.LED_CAPS), key_delay_ms=cfg_delay,
                                    boot_mouse=hid.protocol.get(hidhost.MOUSE_INTF) == hidhost.PROTOCOL_BOOT)


@pytest.fixture
def set_token(dev):
    """set_token(value): POST /api/config token=value and make the clients use it.
    The clean-state fixture clears it again after the test."""
    def _set(value):
        dev.known_tokens = list(getattr(dev, "known_tokens", [])) + [value]
        res = dev.set_config(token=value)
        dev.token = value
        return res
    return _set


@pytest.fixture
def wifi_restore(dev, bench):
    """For tests that write WiFi credentials: always re-write the bench values."""
    yield
    ssid, psk = bench.get("ESPKB_TEST_SSID"), bench.get("ESPKB_TEST_PSK")
    if ssid:
        dev.set_config(wifi_ssid=ssid, wifi_password=psk or "")


# ---------------------------------------------------------------------------
# clean state
# ---------------------------------------------------------------------------

def _clear_token(dev, known_tokens):
    """Make the device open again; returns a problem string or None."""
    for tok in [dev.token] + list(known_tokens):
        if not tok:
            continue
        try:
            espkb.ESPKB(dev.base_url, token=tok, timeout=5).set_config(token="")
            dev.token = ""
            return None
        except espkb.ESPKBError:
            continue
    try:
        import serialcon
        port = support.bench_env().get("ESPKB_SERIAL_PORT")
        with serialcon.SerialConsole(port) as con:
            con.set_token(None)
        dev.token = ""
        dev.get_config()
        return None
    except Exception as e:
        return "device still requires a token and it could not be cleared (%s)" % e


def _restore(dev, hid, baseline, phase):
    problems = []
    hid.resume_all()
    hid.assert_isolated(wait_s=40)
    try:
        dev.wait_ready(timeout=40)
    except espkb.ESPKBError as e:
        if e.code != "unauthorized":
            raise
    # token
    try:
        dev.get_config()
    except espkb.ESPKBError as e:
        if e.code == "unauthorized":
            p = _clear_token(dev, getattr(dev, "known_tokens", []))
            if p:
                pytest.exit("REFUSING TO CONTINUE: " + p, returncode=4)
        else:
            raise
    if dev.token:
        try:
            dev.set_config(token="")
        except espkb.ESPKBError:
            pass
        dev.token = ""
    dev.release()
    hid.wait_idle(60, timeout=5)
    try:
        support.wait_status(dev, lambda s: not s["hid"]["keys"] and not s["hid"]["buttons"]
                            and not s["hid"]["busy"] and not s["hid"]["queued"], timeout=3)
    except AssertionError as e:
        problems.append("device not idle after /api/release: %s" % e)
    hid.wait_idle(100, timeout=5)
    ks = hid.kbd_state()
    if not ks.empty or hid.mouse_buttons():
        problems.append("host-side state after release: keyboard %s, mouse buttons 0x%02x (the device "
                        "did not send a release report)" % (hidhost.describe_kbd(ks.raw), hid.mouse_buttons()))
    forb = support.forbidden_in_reports(hid.reports(0))
    if forb:
        problems += forb
    # LEDs and protocols
    if hid.leds:
        hid.set_leds(0)
    for intf in (hidhost.KBD_INTF, hidhost.MOUSE_INTF):
        if hid.protocol.get(intf) != hidhost.PROTOCOL_REPORT:
            hid.set_protocol(intf, hidhost.PROTOCOL_REPORT)
    # config
    cfg = dev.get_config()
    fix = {}
    for k in ("hostname", "key_delay_ms", "wifi_ssid"):
        if k in baseline and cfg.get(k) != baseline[k]:
            fix[k] = baseline[k]
    if "wifi_ssid" in fix:
        psk = support.bench_env().get("ESPKB_TEST_PSK")
        if fix["wifi_ssid"] == support.bench_env().get("ESPKB_TEST_SSID") and psk:
            fix["wifi_password"] = psk
    if fix:
        dev.set_config(**fix)
    if hid.leds == 0:
        try:
            support.wait_status(dev, lambda s: not any(s["usb"]["leds"].values()), timeout=2)
        except AssertionError as e:
            problems.append("LEDs not cleared: %s" % e)
    if phase == "before":
        try:
            support.wait_status(dev, lambda s: s.get("ws_clients", 0) == 0, timeout=3)
        except AssertionError:
            pass  # lingering WebSocket from a previous test; not fatal here
        hid.wait_idle(50, timeout=5)
        hid.clear()
    return problems


@pytest.fixture
def clean_state(dev, hid, baseline_config):
    problems = _restore(dev, hid, baseline_config, "before")
    if problems:
        pytest.fail("could not establish a clean device state before the test:\n  " + "\n  ".join(problems))
    yield
    problems = _restore(dev, hid, baseline_config, "after")
    if problems:
        pytest.fail("test left the device/host in an unclean state:\n  " + "\n  ".join(problems))


@pytest.fixture(autouse=True)
def _hw_clean_state(request):
    if request.node.get_closest_marker("hw") is None:
        yield
        return
    request.getfixturevalue("clean_state")
    yield
