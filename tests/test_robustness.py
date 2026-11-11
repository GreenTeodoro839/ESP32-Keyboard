"""Short robustness smoke tests (the heavy stress suite is separate)."""
from __future__ import annotations

import random
import time

import pytest

import hidhost
import support

LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"


def test_rapid_sequential_key_requests(dev, hid):
    rnd = random.Random(11)
    keys = [rnd.choice(LETTERS) for _ in range(200)]
    m = hid.mark()
    for k in keys:
        dev.key(k, delay_ms=0)
    reps = hid.collect(m, idle_ms=200, min_count=400)
    assert hidhost.kbd_text(reps) == "".join(keys)
    assert len(reps) == 400


def test_rapid_status_polls(dev):
    lat = []
    for _ in range(300):
        t0 = time.monotonic()
        assert dev.status()["ok"] is True
        lat.append(time.monotonic() - t0)
    lat.sort()
    assert lat[int(len(lat) * 0.99) - 1] < 1.0, "p99 status latency %.2fs" % lat[int(len(lat) * 0.99) - 1]


def test_rapid_mouse_moves(dev, hid):
    rnd = random.Random(12)
    moves = [(rnd.randint(-50, 50), rnd.randint(-50, 50)) for _ in range(100)]
    m = hid.mark()
    for dx, dy in moves:
        dev.move(dx, dy)
    reps = hid.collect(m, idle_ms=200)
    s = hidhost.mouse_summary(reps)
    assert (s.dx, s.dy) == (sum(d[0] for d in moves), sum(d[1] for d in moves))
    zero = sum(1 for d in moves if d == (0, 0))
    assert len(reps) >= 100 - zero - 0


def test_mixed_rapid_requests(dev, hid):
    rnd = random.Random(13)
    m = hid.mark()
    expected = []
    for i in range(150):
        kind = i % 3
        if kind == 0:
            k = rnd.choice(LETTERS)
            dev.key(k, delay_ms=0)
            expected.append(k)
        elif kind == 1:
            dev.status()
        else:
            dev.scroll(1)
    reps = hid.collect(m, idle_ms=200)
    assert hidhost.kbd_text(support.kbd(reps)) == "".join(expected)
    assert hidhost.mouse_summary(support.mouse(reps)).wheel == 50


def test_reconnecting_websockets(dev, hid, ws_factory):
    for i in range(25):
        c = ws_factory()
        assert c.wait(lambda o: "status" in o, timeout=2) is not None, "no status on connection %d" % i
        assert c.ping(i) is not None
        c.close()
    support.wait_status(dev, lambda s: s["ws_clients"] == 0, timeout=5)
    c = ws_factory()
    m = hid.mark()
    c.send({"key": "w", "id": "end"})
    assert c.wait_ack("end", timeout=3).obj["ok"] is True
    assert len(hid.reports(m)) == 2


def test_ws_paced_burst_in_order(dev, hid, ws):
    """200 WebSocket jobs, at most 16 in flight (queue capacity is 32): all executed in order."""
    rnd = random.Random(14)
    keys = [rnd.choice(LETTERS) for _ in range(200)]
    m = hid.mark()
    for i, k in enumerate(keys):
        if i >= 16:
            assert ws.wait_ack(i - 16, timeout=10) is not None, "missing ack %d" % (i - 16)
        ws.send({"key": k, "delay_ms": 0, "id": i})
    for i in range(184, 200):
        a = ws.wait_ack(i, timeout=10)
        assert a is not None and a.obj["ok"] is True
    busy = [e for e in ws.select("ack") if not e.obj.get("ok")]
    assert not busy, busy[:3]
    reps = hid.collect(m, idle_ms=200, min_count=400)
    assert hidhost.kbd_text(reps) == "".join(keys)


def test_many_async_jobs_complete_in_order(dev, hid):
    m = hid.mark()
    text = []
    for i in range(30):
        ch = LETTERS[i % len(LETTERS)]
        dev.key(ch, delay_ms=0, wait=False)
        text.append(ch)
    dev.wait_idle(timeout=20)
    reps = hid.collect(m, idle_ms=200, min_count=60)
    assert hidhost.kbd_text(reps) == "".join(text)


@pytest.mark.wifi
@pytest.mark.slow
def test_wifi_down_up_recovery(dev, hid, ws_factory, bench):
    conn = bench.get("ESPKB_NM_CONN", "espkb-test")
    if not support.hotspot_active(conn):
        pytest.skip("hotspot connection %s is not active" % conn)
    w = ws_factory()
    w.send({"key": "a", "action": "down"})
    hid.wait_until(lambda: 4 in hid.kbd_state().pressed, 2)
    w.start_keepalive(0.25)
    dev.key_down("b", ttl_ms=3000)
    hid.wait_until(lambda: 5 in hid.kbd_state().pressed, 2)
    t0 = time.monotonic_ns()
    with support.hotspot_down(conn):
        t_down = time.monotonic_ns()
        # the WebSocket-held key must be released ~1 s after the device last heard from us
        released_a = support.wait_kbd_state(hid, lambda s: 4 not in s.pressed, 6.0)
        assert released_a is not None, "WebSocket-held key not released after WiFi loss"
        assert (released_a - t_down) / 1e6 < 1700, "released %.0f ms after the hotspot went down" % (
            (released_a - t_down) / 1e6)
        released_b = support.wait_kbd_state(hid, lambda s: 5 not in s.pressed, 6.0)
        assert released_b is not None and (released_b - t0) / 1e6 < 3800, "HTTP ttl not honoured without WiFi"
        time.sleep(3)  # stay below the 20 s after which the device would open its own AP
        assert hid.kbd_state().empty
    w.stop_keepalive()
    st = dev.wait_ready(timeout=90)
    assert st["wifi"]["sta_connected"] is True
    assert st["hid"]["keys"] == []
    _, reps = support.do(hid, lambda: dev.type("back online"))
    assert hidhost.kbd_text(reps) == "back online"


@pytest.mark.reboot
def test_reboot_recovery(dev, hid):
    up_before = dev.status()["device"]["uptime_ms"]
    gen = hid.generation
    assert dev.reboot()["ok"] is True
    t0 = time.monotonic()
    assert hid.wait_disconnected(10), "USB did not disconnect after /api/reboot"
    assert time.monotonic() - t0 >= 0.3, "device rebooted before ~500 ms"
    assert hid.wait_generation(gen + 1, 40), "harness did not re-acquire the device after reboot"
    hid.assert_isolated()
    st = dev.wait_ready(timeout=60)
    assert st["device"]["uptime_ms"] < up_before or st["device"]["uptime_ms"] < 60000
    assert isinstance(st["device"]["reset_reason"], str)
    assert st["usb"]["leds"] == {"num": False, "caps": False, "scroll": False}
    _, reps = support.do(hid, lambda: dev.type("rebooted OK\n"))
    assert hidhost.kbd_text(reps) == "rebooted OK\n"
    assert hid.log.events(kind="connect")
