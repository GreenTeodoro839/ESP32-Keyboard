"""API.md section 6: stuck-key protection (ownership, keep-alive timeouts, release on close)."""
from __future__ import annotations

import time

import pytest

import support

A, B, C = 4, 5, 6


def wait_released(hid, usage, timeout):
    """Wait until `usage` is no longer pressed on the host side; return the release time (ns) or None."""
    return support.wait_kbd_state(hid, lambda s: usage not in s.pressed, timeout)


def ws_hold(ws, hid, key="a", usage=A):
    m = hid.mark()
    t = ws.send({"key": key, "action": "down"})
    hid.wait_until(lambda: usage in hid.kbd_state().pressed, 2)
    assert usage in hid.kbd_state().pressed, "key did not go down: %r" % [r.data.hex() for r in hid.reports(m)]
    return t


def test_ws_keepalive_timeout_releases_after_1s(dev, hid, ws):
    t_send = ws_hold(ws, hid)
    t_rel = wait_released(hid, A, 3.0)
    assert t_rel is not None, "WebSocket-held key not released without keep-alive"
    dt = (t_rel - t_send) / 1e6
    assert 850 <= dt <= 1600, "released %.0f ms after the last message (expected ~1000)" % dt
    assert dev.status()["hid"]["keys"] == []
    assert not ws.closed, "the keep-alive timeout should release keys, not necessarily close the connection"


def test_ws_pings_keep_key_held(dev, hid, ws):
    ws_hold(ws, hid)
    ws.start_keepalive(0.25)
    time.sleep(3.0)
    assert A in hid.kbd_state().pressed, "key released although pings continued every 250 ms"
    assert dev.status()["hid"]["keys"] == [A]
    ws.stop_keepalive()
    t_last = ws.last_send_ns
    t_rel = wait_released(hid, A, 3.0)
    assert t_rel is not None
    dt = (t_rel - t_last) / 1e6
    assert 850 <= dt <= 1600, "released %.0f ms after the last ping" % dt


def test_ws_step_messages_count_as_keepalive(hid, ws):
    ws_hold(ws, hid)
    for _ in range(8):
        time.sleep(0.3)
        ws.send({"delay": 0})
    assert A in hid.kbd_state().pressed, "step messages must also count as keep-alive"
    assert wait_released(hid, A, 3.0) is not None


def test_ws_close_releases_immediately(dev, hid, ws):
    ws_hold(ws, hid)
    ws.start_keepalive(0.25)
    time.sleep(0.5)
    t0 = time.monotonic_ns()
    ws.close()
    t_rel = wait_released(hid, A, 2.0)
    assert t_rel is not None
    assert (t_rel - t0) / 1e6 < 400, "released %.0f ms after close" % ((t_rel - t0) / 1e6)


def test_ws_abrupt_disconnect_releases(dev, hid, ws):
    ws_hold(ws, hid)
    ws.start_keepalive(0.25)
    time.sleep(0.5)
    t0 = time.monotonic_ns()
    ws.abort()
    t_rel = wait_released(hid, A, 3.0)
    assert t_rel is not None, "key still held after the TCP connection was dropped"
    assert (t_rel - t0) / 1e6 < 1700


def test_ws_held_button_released_on_close(dev, hid, ws):
    ws.send({"button": "left", "action": "down"})
    hid.wait_until(lambda: hid.mouse_buttons() == 1, 2)
    assert hid.mouse_buttons() == 1
    ws.close()
    assert hid.wait_until(lambda: hid.mouse_buttons() == 0, 1.0)


def test_ownership_closing_one_ws_keeps_other_owners(dev, hid, ws_factory):
    w1, w2 = ws_factory(), ws_factory()
    ws_hold(w1, hid, "a", A)
    ws_hold(w2, hid, "b", B)
    w1.start_keepalive(0.25)
    w2.start_keepalive(0.25)
    dev.key_down("c", ttl_ms=30000)
    hid.wait_until(lambda: C in hid.kbd_state().pressed, 2)
    w1.close()
    assert wait_released(hid, A, 1.0) is not None
    time.sleep(1.5)
    p = hid.kbd_state().pressed
    assert B in p and C in p, "closing one WebSocket released keys owned by others: %s" % sorted(p)
    assert sorted(dev.status()["hid"]["keys"]) == [B, C]
    w2.close()
    assert wait_released(hid, B, 1.0) is not None
    time.sleep(0.5)
    assert C in hid.kbd_state().pressed, "HTTP-held key released when a WebSocket closed"
    dev.key_up("c")
    assert hid.kbd_state().empty


def test_ownership_keepalive_timeout_only_affects_own_keys(dev, hid, ws_factory):
    w1, w2 = ws_factory(), ws_factory()
    ws_hold(w1, hid, "a", A)
    ws_hold(w2, hid, "b", B)
    w2.start_keepalive(0.25)
    assert wait_released(hid, A, 2.5) is not None
    time.sleep(1.5)
    assert B in hid.kbd_state().pressed, "keep-alive timeout of one connection released another's key"
    w2.close()


def test_http_hold_default_5s(dev, hid):
    dev.key_down("a")
    t0 = time.monotonic_ns()
    hid.wait_until(lambda: A in hid.kbd_state().pressed, 1)
    time.sleep(4.0)
    assert A in hid.kbd_state().pressed, "HTTP-held key released before 5 s"
    t_rel = wait_released(hid, A, 3.0)
    assert t_rel is not None, "HTTP-held key not released after 5 s"
    dt = (t_rel - t0) / 1e6
    assert 4800 <= dt <= 6000, "released after %.0f ms (expected ~5000)" % dt
    assert dev.status()["hid"]["keys"] == []


def test_http_ttl_short(dev, hid):
    dev.key_down("a", ttl_ms=1500)
    t0 = time.monotonic_ns()
    t_rel = wait_released(hid, A, 4.0)
    assert t_rel is not None
    dt = (t_rel - t0) / 1e6
    assert 1350 <= dt <= 2300, "released after %.0f ms (ttl_ms 1500)" % dt


@pytest.mark.slow
def test_http_ttl_long(dev, hid):
    dev.key_down("a", ttl_ms=8000)
    t0 = time.monotonic_ns()
    time.sleep(6.5)
    assert A in hid.kbd_state().pressed, "ttl_ms 8000 key released before 6.5 s"
    t_rel = wait_released(hid, A, 4.0)
    assert t_rel is not None
    assert 7800 <= (t_rel - t0) / 1e6 <= 9000


def test_http_action_request_refreshes_deadline(dev, hid):
    dev.key_down("a", ttl_ms=2000)
    t0 = time.monotonic_ns()
    time.sleep(1.2)
    dev.seq([{"delay": 0}])
    t_refresh = time.monotonic_ns()
    t_rel = wait_released(hid, A, 4.0)
    assert t_rel is not None
    assert (t_rel - t0) / 1e6 >= 2900, "released %.0f ms after down despite a refresh" % ((t_rel - t0) / 1e6)
    dt = (t_rel - t_refresh) / 1e6
    assert 1850 <= dt <= 2800, "released %.0f ms after the refreshing request (ttl 2000)" % dt


def test_http_get_does_not_refresh(dev, hid):
    dev.key_down("a", ttl_ms=2000)
    t0 = time.monotonic_ns()
    while A in hid.kbd_state().pressed and (time.monotonic_ns() - t0) < 4e9:
        dev.status()
        time.sleep(0.3)
    t_rel = hid.log.last("kbd").t_ns
    dt = (t_rel - t0) / 1e6
    assert A not in hid.kbd_state().pressed
    assert 1850 <= dt <= 2800, "GET requests must not refresh the hold deadline (released after %.0f ms)" % dt


def test_http_button_hold_timeout(dev, hid):
    dev.button_down("left", ttl_ms=1500)
    t0 = time.monotonic_ns()
    assert hid.wait_until(lambda: hid.mouse_buttons() == 0, 4.0)
    dt = (hid.log.last("mouse").t_ns - t0) / 1e6
    assert 1350 <= dt <= 2300, dt


def test_ws_hold_unaffected_by_http_deadline(dev, hid, ws):
    """A WebSocket-held key follows the WebSocket rules, not the HTTP 5 s deadline."""
    ws_hold(ws, hid)
    ws.start_keepalive(0.25)
    dev.key_down("b", ttl_ms=1000)
    assert wait_released(hid, B, 3.0) is not None
    time.sleep(0.5)
    assert A in hid.kbd_state().pressed
    ws.close()
