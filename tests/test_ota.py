"""API.md section 11: firmware update over WiFi (GET/POST /api/ota).

These tests talk to the device over the network only and never make it emit
keys, so they run without the USB HID harness (marker ``no_hid``).  Tests that
write the firmware slot or reboot the device carry the ``ota`` marker and are
excluded from the default run::

    .venv/bin/python -m pytest tests/test_ota.py            # checks that never write flash
    .venv/bin/python -m pytest tests/test_ota.py -m ota     # real updates (about 2 minutes)

The image is ``$ESPKB_OTA_IMAGE`` or the last build (.pio/build/esp32-s3-devkitc-1/firmware.bin).
It should be the firmware the device runs (or a newer one): every real update leaves
the device running that image.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import time

import pytest

import espkb
import support

pytestmark = pytest.mark.no_hid

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGE = os.path.join(HERE, "..", ".pio", "build", "esp32-s3-devkitc-1", "firmware.bin")
CONFIG_KEYS = ("hostname", "wifi_ssid", "wifi_password_set", "ap_ssid", "ap_password_set", "token_set", "key_delay_ms")
UPDATE_STATES = {"idle", "receiving", "verifying", "done", "failed"}


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kb(device_session):
    return espkb.ESPKB(device_session["host"], token="", timeout=10)


@pytest.fixture(scope="module")
def image():
    path = os.environ.get("ESPKB_OTA_IMAGE") or DEFAULT_IMAGE
    if not os.path.exists(path):
        pytest.skip("no firmware image: build the project (pio run) or set ESPKB_OTA_IMAGE")
    with open(path, "rb") as f:
        data = f.read()
    head = espkb.parse_image(data)
    assert head["project"] == "espkb", "%s is not an espkb firmware" % path
    return data


@pytest.fixture(autouse=True)
def _idle_before_and_after(kb):
    """No upload may be running when a test starts, and none may be left behind."""
    wait_update(kb, lambda u: u["state"] not in ("receiving", "verifying"), 30)
    yield
    kb.wait_ready(timeout=60, require_usb=False)
    wait_update(kb, lambda u: u["state"] not in ("receiving", "verifying"), 30)


def wait_update(kb, pred, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            up = kb.ota_info()["update"]
            if pred(up):
                return up
        except espkb.ESPKBConnectionError:
            up = None
        assert time.monotonic() < deadline, "update state did not settle: %r" % (up,)
        time.sleep(0.3)


def identity(kb):
    d = kb.status()["device"]
    return d["sha256"], d["partition"]


def raw_post(host, headers, body=b"", read_timeout=30.0, keep_open=False):
    """POST /api/ota over a plain socket so the test controls exactly what is sent.
    Returns (status, json) - or the open socket if keep_open.

    The device looks at a request once the first body byte is there, so tests of
    requests it must refuse send the headers and one byte: nothing is left unread
    in the device's socket when it closes, and its answer cannot be lost to a reset."""
    netloc = support.host_only(host)
    name, _, port = netloc.partition(":")
    s = socket.create_connection((name, int(port or 80)), timeout=10)
    head = "POST /api/ota HTTP/1.1\r\nHost: %s\r\n" % netloc
    head += "".join("%s: %s\r\n" % kv for kv in headers.items()) + "\r\n"
    s.sendall(head.encode() + body)
    if keep_open:
        return s
    try:
        return read_response(s, read_timeout)
    finally:
        s.close()


def read_response(s, timeout=30.0):
    s.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        assert chunk, "connection closed before a response arrived (got %r)" % data[:200]
        data += chunk
        if data.startswith(b"HTTP/1.1 100") and b"\r\n\r\n" in data:
            data = data.split(b"\r\n\r\n", 1)[1]
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    m = re.search(rb"content-length:\s*(\d+)", head, re.I)
    want = int(m.group(1)) if m else 0
    while len(body) < want:
        chunk = s.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, json.loads(body.decode("utf-8"))


def patched(image, offset, data):
    b = bytearray(image)
    b[offset:offset + len(data)] = data
    return bytes(b)


def expect_refused(kb, body, status, code, pattern):
    before = identity(kb)
    up0 = kb.status()["device"]["uptime_ms"]
    err = support.expect_error(lambda: kb.ota(body), status, code)
    assert re.search(pattern, err.message), err.message
    st = kb.status()
    assert (st["device"]["sha256"], st["device"]["partition"]) == before
    assert st["device"]["uptime_ms"] >= up0, "the device rebooted"
    return err


# ---------------------------------------------------------------------------
# information
# ---------------------------------------------------------------------------

def test_ota_info_schema(kb):
    info = kb.ota_info()
    st = kb.status()
    run, nxt, up = info["running"], info["next"], info["update"]
    assert run["version"] == st["device"]["version"]
    assert re.fullmatch(r"[0-9a-f]{16}", run["sha256"]) and run["sha256"] == st["device"]["sha256"]
    assert run["partition"] in ("ota_0", "ota_1") and run["partition"] == st["device"]["partition"]
    assert run["build"] == st["device"]["build"] and re.match(r"[A-Z][a-z]{2} [ \d]\d \d{4} \d\d:\d\d:\d\d$", run["build"])
    assert run["state"] in ("valid", "pending_verify") and isinstance(run["idf"], str) and run["idf"]
    assert nxt["partition"] in ("ota_0", "ota_1") and nxt["partition"] != run["partition"]
    assert type(nxt["size"]) is int and nxt["size"] >= 0x100000
    assert up["state"] in UPDATE_STATES and type(up["received"]) is int and type(up["total"]) is int
    assert type(up["message"]) is str
    assert set(st["ota"]) == {"state", "received", "total"} and st["ota"]["state"] == up["state"]


def test_ota_endpoints_described(kb):
    eps = {(e["method"], e["path"]) for e in kb.info()["endpoints"]}
    assert {("GET", "/api/ota"), ("POST", "/api/ota")} <= eps


# ---------------------------------------------------------------------------
# requests refused before anything is written to flash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 100, 287])
def test_body_too_small(kb, size):
    expect_refused(kb, bytes(size), 400, "bad_request", "firmware image")


def test_not_an_image(kb):
    expect_refused(kb, os.urandom(300000).replace(b"\xe9", b"\x00"), 400, "bad_request", "not an ESP application image")
    up = kb.ota_info()["update"]
    assert up["state"] == "failed" and "not an ESP application image" in up["message"]


def test_other_project(kb, image):
    expect_refused(kb, patched(image, 80, b"blink\0"), 400, "bad_request", "not an espkb firmware")


def test_other_chip(kb, image):
    expect_refused(kb, patched(image, 12, struct.pack("<H", 5)), 400, "bad_request", "different chip")


def test_larger_than_the_slot(kb, device_session):
    size = kb.ota_info()["next"]["size"] + 1
    before = identity(kb)
    # the device has to answer without waiting for 4 MB
    status, obj = raw_post(device_session["host"], {"Content-Length": size}, b"\xe9")
    assert status == 413 and obj["error"] == "too_large" and str(size) in obj["message"], obj
    assert identity(kb) == before


def test_token_required(kb, device_session, image):
    host = device_session["host"]
    kb.set_config(token="ota-test-token")
    try:
        hdr = {"Content-Length": len(image)}
        status, obj = raw_post(host, hdr, image[:1])
        assert status == 401 and obj["error"] == "unauthorized", obj
        status, obj = raw_post(host, dict(hdr, Authorization="Bearer wrong"), image[:1])
        assert status == 401, obj
        support.expect_error(lambda: espkb.ESPKB(host, token="").ota_info(), 401, "unauthorized")
        assert espkb.ESPKB(host, token="ota-test-token").ota_info()["ok"] is True
    finally:
        espkb.ESPKB(host, token="ota-test-token").set_config(token="")
    assert kb.status()["auth_required"] is False


def test_cross_origin_refused(kb, device_session, image):
    status, obj = raw_post(device_session["host"], {"Content-Length": len(image), "Origin": "http://evil.example"},
                           image[:1])
    assert status == 403 and obj["error"] == "forbidden", obj


def test_expect_100_continue(kb, device_session):
    """curl announces bodies over 1 MB (firmware images) with "Expect: 100-continue" and sends
    nothing until the device answers "100 Continue"."""
    body = os.urandom(300000).replace(b"\xe9", b"\x00")
    s = raw_post(device_session["host"], {"Content-Length": len(body), "Expect": "100-continue"}, keep_open=True)
    try:
        s.settimeout(3)
        t0 = time.monotonic()
        first = s.recv(4096)
        assert first.startswith(b"HTTP/1.1 100 Continue\r\n\r\n"), first
        assert time.monotonic() - t0 < 2.0
        s.sendall(body)
        status, obj = read_response(s, 30)
        assert status == 400 and obj["error"] == "bad_request" and "not an ESP application image" in obj["message"], obj
    finally:
        s.close()


# ---------------------------------------------------------------------------
# uploads that write the inactive slot
# ---------------------------------------------------------------------------

@pytest.mark.ota
def test_corrupt_image_is_not_activated(kb, image):
    """A damaged image is written, fails verification and must never be booted."""
    before = identity(kb)
    bad = patched(image, len(image) // 2, bytes([image[len(image) // 2] ^ 0xFF]))
    err = expect_refused(kb, bad, 400, "bad_request", "verification failed")
    assert err.status == 400
    up = kb.ota_info()["update"]
    assert up["state"] == "failed" and up["received"] == len(image)
    kb.reboot_and_wait(timeout=60, require_usb=False)
    assert identity(kb) == before, "the device booted something else after a failed update"


@pytest.mark.ota
def test_interrupted_upload(kb, device_session, image):
    before = identity(kb)
    s = raw_post(device_session["host"], {"Content-Length": len(image)}, image[:200000], keep_open=True)
    try:
        up = wait_update(kb, lambda u: u["state"] == "receiving" and u["received"] > 0, 10)
        assert up["total"] == len(image)
        assert kb.status()["ota"]["state"] == "receiving"
    finally:
        s.close()
    up = wait_update(kb, lambda u: u["state"] == "failed", 20)
    assert "interrupted" in up["message"], up
    assert identity(kb) == before


@pytest.mark.ota
def test_only_one_upload_at_a_time(kb, device_session, image):
    host = device_session["host"]
    s = raw_post(host, {"Content-Length": len(image)}, image[:100000], keep_open=True)
    try:
        wait_update(kb, lambda u: u["state"] == "receiving", 10)
        status, obj = raw_post(host, {"Content-Length": len(image)}, image[:1])
        assert status == 503 and obj["error"] == "busy", obj
        # the server keeps answering other requests meanwhile
        t0 = time.monotonic()
        assert kb.status()["ok"] is True and time.monotonic() - t0 < 2.0
    finally:
        s.close()
    wait_update(kb, lambda u: u["state"] == "failed", 20)


@pytest.mark.ota
def test_stalled_upload_times_out(kb, device_session, image):
    """A client that stops sending without closing loses the upload after 15 s."""
    s = raw_post(device_session["host"], {"Content-Length": len(image)}, image[:70000], keep_open=True)
    try:
        wait_update(kb, lambda u: u["state"] == "receiving", 10)
        t0 = time.monotonic()
        up = wait_update(kb, lambda u: u["state"] == "failed", 30)
        assert 10 <= time.monotonic() - t0 <= 25 and "interrupted" in up["message"], up
        status, obj = read_response(s, 5)
        assert status == 400 and obj["error"] == "bad_request", obj
    finally:
        s.close()


@pytest.mark.ota
def test_update_without_reboot(kb, image):
    """?reboot=0: the image is stored and verified; it starts at the next reboot."""
    sha, part = identity(kb)
    up0 = kb.status()["device"]["uptime_ms"]
    res = kb.ota(image, reboot=False)
    assert res["ok"] is True and res["reboot"] is False and res["bytes"] == len(image)
    assert res["partition"] != part and res["version"] == espkb.parse_image(image)["version"]
    time.sleep(2.5)
    st = kb.status()
    assert st["device"]["uptime_ms"] > up0 and st["device"]["partition"] == part, "the device rebooted"
    assert st["ota"] == {"state": "done", "received": len(image), "total": len(image)}
    st = kb.reboot_and_wait(timeout=60, require_usb=False)
    assert st["device"]["partition"] == res["partition"]
    assert st["device"]["sha256"] == espkb.parse_image(image)["sha256"]
    assert st["ota"]["state"] == "idle"


@pytest.mark.ota
def test_update_keeps_settings(kb, image):
    """The point of it all: a real update, and nothing of the configuration changes."""
    cfg0 = kb.get_config()
    delay = 7 if cfg0["key_delay_ms"] != 7 else 8          # a value that is not the default
    kb.set_config(key_delay_ms=delay)
    try:
        cfg = kb.get_config()
        wifi = kb.status()["wifi"]
        part = kb.status()["device"]["partition"]
        st = kb.ota_and_wait(image, timeout=90)
        assert st["device"]["sha256"] == espkb.parse_image(image)["sha256"]
        assert st["device"]["partition"] != part and st["device"]["reset_reason"] == "software"
        after = kb.get_config()
        assert {k: after[k] for k in CONFIG_KEYS} == {k: cfg[k] for k in CONFIG_KEYS}
        assert after["key_delay_ms"] == delay
        st = support.wait_status(kb, lambda s: s["wifi"]["sta_connected"] == wifi["sta_connected"], timeout=30)
        assert st["wifi"]["ssid"] == wifi["ssid"] and st["wifi"]["hostname"] == wifi["hostname"]
        if wifi["sta_connected"]:
            assert st["wifi"]["ip"] == wifi["ip"]
        # the new image is on probation at first and confirms itself once the network has been up for 10 s
        deadline = time.monotonic() + 40
        while kb.ota_info()["running"]["state"] != "valid":
            assert time.monotonic() < deadline, "new firmware was not confirmed"
            time.sleep(1)
    finally:
        kb.wait_ready(timeout=60, require_usb=False)
        kb.set_config(key_delay_ms=cfg0["key_delay_ms"])
    assert kb.get_config()["key_delay_ms"] == cfg0["key_delay_ms"]
