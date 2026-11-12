"""API.md section 5: WebSocket message forms, ack, ping/pong, status pushes, errors, limits, auth."""
from __future__ import annotations

import json
import time

import pytest

import hidhost
import support
from support import assert_nothing_emitted

STATUS_KEYS = {"device", "usb", "hid", "wifi", "ws_clients", "auth_required"}


def test_status_pushed_on_connect(ws):
    m = ws.wait(lambda o: "status" in o, timeout=1.5)
    assert m is not None, "no status message within 1.5 s of connecting: %r" % ws.messages
    assert (m.t_ns - ws.connected_ns) / 1e6 < 1000
    st = m.obj["status"]
    assert STATUS_KEYS <= set(st), set(st)
    assert "ok" not in st
    assert st["ws_clients"] >= 1


def test_step_object_executes_without_reply(dev, hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    wm = ws.mark()
    m = hid.mark()
    ws.send({"key": "a"})
    reps = hid.collect(m, idle_ms=150, min_count=2)
    assert [r.data for r in reps] == [hidhost.kbd_bytes(0, [4]), bytes(8)]
    time.sleep(0.3)
    assert ws.non_status(wm) == [], "unexpected reply to a message without id: %r" % ws.non_status(wm)


@pytest.mark.parametrize("ack_id", [7, "job-x", 0, -3, "", 12.5], ids=repr)
def test_id_is_acked_after_completion(hid, ws, ack_id):
    m = hid.mark()
    ws.send({"key": "b", "id": ack_id})
    ack = ws.wait_ack(ack_id, timeout=5)
    assert ack is not None, ws.non_status()
    assert ack.obj == {"ack": ack_id, "ok": True} or (ack.obj["ok"] is True and ack.obj["ack"] == ack_id)
    reps = hid.reports(m)
    assert len(reps) == 2 and ack.t_ns >= reps[-1].t_ns, "ack arrived before the job finished"


def test_steps_form_with_id(hid, ws):
    steps = [{"key": "c"}, {"move": [2, 2]}, {"text": "D"}]
    m = hid.mark()
    ws.send({"steps": steps, "id": "s1"})
    ack = ws.wait_ack("s1", timeout=5)
    assert ack is not None and ack.obj["ok"] is True
    hidhost.assert_reports_equal(hidhost.ReferenceEncoder().steps(steps), hid.collect(m, idle_ms=100))


def test_array_form(hid, ws):
    steps = [{"key": "e"}, {"click": "middle"}]
    m = hid.mark()
    ws.send(steps)
    reps = hid.collect(m, idle_ms=200, min_count=4)
    hidhost.assert_reports_equal(hidhost.ReferenceEncoder().steps(steps), reps)


def test_ack_order_is_fifo(hid, ws):
    for i in range(1, 6):
        ws.send({"key": "f", "id": i})
    acks = [ws.wait_ack(i, timeout=5) for i in range(1, 6)]
    assert all(acks)
    assert [a.t_ns for a in acks] == sorted(a.t_ns for a in acks)


def test_error_ack_with_id(hid, ws):
    m = hid.mark()
    ws.send({"key": "nosuch", "id": 9})
    ack = ws.wait_ack(9, timeout=3)
    assert ack is not None
    o = ack.obj
    assert o["ok"] is False and o["error"] == "bad_step" and isinstance(o.get("message"), str) and o["message"]
    assert_nothing_emitted(hid, m)


def test_error_without_id(hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    wm = ws.mark()
    m = hid.mark()
    ws.send({"key": "nosuch"})
    msg = ws.wait(lambda o: "error" in o and "ack" not in o, timeout=3, start=wm)
    assert msg is not None
    assert msg.obj["error"] == "bad_step" and msg.obj.get("message")
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2", "\"just a string\"", "42", "null"], ids=repr)
def test_malformed_message_error(hid, ws, text):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    wm = ws.mark()
    m = hid.mark()
    ws.send(text)
    msg = ws.wait(lambda o: "error" in o, timeout=3, start=wm)
    assert msg is not None, "no error for %r" % text
    assert msg.obj["error"] in ("bad_request", "bad_step")
    assert_nothing_emitted(hid, m)
    assert ws.ping("still-alive") is not None


def test_error_rate_limit_once_per_second(hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    time.sleep(1.1)  # make sure no earlier error is within the window
    wm = ws.mark()
    t0 = time.monotonic()
    for _ in range(5):
        ws.send({"key": "nosuch"})
    time.sleep(max(0.0, 0.9 - (time.monotonic() - t0)))
    first = ws.errors(wm)
    assert len(first) == 1, "expected exactly one error in the first second, got %r" % [e.obj for e in first]
    time.sleep(max(0.0, 1.15 - (time.monotonic() - t0)))
    wm2 = ws.mark()
    ws.send({"key": "nosuch"})
    assert ws.wait(lambda o: o.get("error") == "bad_step", timeout=2, start=wm2) is not None, \
        "an error more than 1 s later must be reported again"


def test_rate_limit_is_per_error_kind(hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    time.sleep(1.1)
    wm = ws.mark()
    ws.send({"key": "nosuch"})
    ws.send("{broken")
    time.sleep(0.8)
    kinds = sorted(e.obj["error"] for e in ws.errors(wm))
    assert kinds == ["bad_request", "bad_step"], kinds


def test_acks_are_not_rate_limited(hid, ws):
    for i in range(5):
        ws.send({"key": "nosuch", "id": "e%d" % i})
    for i in range(5):
        ack = ws.wait_ack("e%d" % i, timeout=3)
        assert ack is not None and ack.obj["ok"] is False, "missing ack e%d" % i


@pytest.mark.parametrize("value", [1, 0, -7, "abc", "", {"x": [1, 2]}, [1, "a"], None, True, 3.25], ids=repr)
def test_ping_pong_echoes_value(dev, ws, value):
    wm = ws.mark()
    ws.send({"ping": value})
    msg = ws.wait(lambda o: "pong" in o and o["pong"] == value, timeout=2, start=wm)
    assert msg is not None, ws.non_status(wm)
    assert isinstance(msg.obj["t"], int) and msg.obj["t"] > 0
    up = dev.status()["device"]["uptime_ms"]
    assert abs(up - msg.obj["t"]) < 5000


def test_ws_release_is_immediate(dev, hid, ws):
    m = hid.mark()
    dev.type("r" * 400, delay_ms=5, wait=False)
    hid.wait_reports(10, m, timeout=5)
    t0 = time.monotonic_ns()
    ws.send({"release": True})
    support.wait_status(dev, lambda s: not s["hid"]["busy"] and not s["hid"]["queued"], timeout=1.5, interval=0.02)
    hid.wait_idle(300)
    reps = hid.reports(m)
    assert support.kbd(reps)[-1].data == bytes(8)
    late = [r for r in reps if r.t_ns > t0 + 300e6]
    assert not late, "%d reports more than 300 ms after the WebSocket release" % len(late)
    assert len(reps) < 800


def test_status_push_on_held_key_change(dev, hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    time.sleep(0.2)
    wm = ws.mark()
    t0 = time.monotonic_ns()
    dev.key_down("shift", ttl_ms=10000)
    msg = ws.wait(lambda o: "status" in o and 225 in o["status"]["hid"]["keys"], timeout=1.5, start=wm)
    assert msg is not None, "no status push after a key went down"
    assert (msg.t_ns - t0) / 1e6 < 700, "status push came %.0f ms after the change" % ((msg.t_ns - t0) / 1e6)
    wm = ws.mark()
    dev.key_up("shift")
    msg = ws.wait(lambda o: "status" in o and o["status"]["hid"]["keys"] == [], timeout=1.5, start=wm)
    assert msg is not None


def test_status_push_on_led_change(dev, hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    wm = ws.mark()
    t0 = time.monotonic_ns()
    hid.set_leds(hidhost.LED_CAPS)
    msg = ws.wait(lambda o: "status" in o and o["status"]["usb"]["leds"]["caps"] is True, timeout=1.5, start=wm)
    assert msg is not None, "no status push after a LED change"
    assert (msg.t_ns - t0) / 1e6 < 700


def test_status_pushed_every_2s(ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    wm = ws.mark()
    time.sleep(6.6)
    st = ws.statuses(wm)
    assert len(st) >= 3, "only %d status pushes in 6.6 s" % len(st)
    gaps = [(st[i].t_ns - st[i - 1].t_ns) / 1e9 for i in range(1, len(st))]
    assert all(1.5 <= g <= 2.6 for g in gaps), "periodic push intervals %s" % [round(g, 2) for g in gaps]


def test_frame_within_limit_executes(hid, ws):
    msg = {"key": "g", "id": "big", "pad": ""}
    msg["pad"] = "x" * (8000 - len(json.dumps(msg)))
    assert len(json.dumps(msg)) == 8000
    m = hid.mark()
    ws.send(msg)
    ack = ws.wait_ack("big", timeout=3)
    assert ack is not None and ack.obj["ok"] is True
    assert len(hid.reports(m)) == 2


def test_frame_over_limit_rejected(hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    msg = {"key": "h", "id": "toobig", "pad": "x" * 9000}
    wm = ws.mark()
    m = hid.mark()
    try:
        ws.send(msg)
    except Exception:
        pass  # the device may drop the connection while we are still sending
    got = ws.wait(lambda o: "error" in o or ("ack" in o and o.get("ok") is False), timeout=2, start=wm)
    closed = ws.wait_closed(0.1)
    assert got is not None or closed, "oversized frame neither rejected nor connection closed"
    if got is not None:
        assert (got.obj.get("error") in ("too_large", "bad_request")), got.obj
    assert_nothing_emitted(hid, m)


def test_binary_frame_not_executed(hid, ws):
    ws.wait(lambda o: "status" in o, timeout=1.5)
    m = hid.mark()
    ws.send_binary(b'{"key":"i"}')
    time.sleep(0.5)
    assert_nothing_emitted(hid, m)
    if not ws.closed:
        assert ws.ping("after-binary") is not None, "connection unusable after a binary frame but not closed"


def test_close_lets_client_close_tcp_first(device_host, dev):
    """The device must not be the active TCP closer after a WebSocket close.

    If it is, the 4-tuple stays in TIME_WAIT for 2*TCP_MSL on the device while
    the client is already fully closed and free to reuse its source port; a
    new SYN then gets a challenge ACK instead of SYN-ACK (docs/known-issues.md).
    """
    import select
    import socket

    import websocket

    url = support.ws_url(device_host, token=(dev.token or None))
    ws = websocket.create_connection(url, timeout=5.0)
    try:
        # The device pushes a status frame right after the handshake.
        while True:
            opcode, _ = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_TEXT:
                break
        ws.send_close(status=1000)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            opcode, _ = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
        else:
            raise AssertionError("device did not echo the WebSocket close frame")

        sock = ws.sock
        readable, _, _ = select.select([sock], [], [], 0.2)
        assert not readable, (
            "device sent FIN before the client closed the TCP connection; that puts the "
            "4-tuple in TIME_WAIT on the device and can stall a reused client port")
        sock.shutdown(socket.SHUT_WR)  # client FIN, keep the read side open
        sock.settimeout(2.0)
        assert sock.recv(1) == b"", "device did not close after the client's FIN"
    finally:
        try:
            ws.shutdown()
        except Exception:
            pass


def test_connection_limit_4(dev, ws_factory):
    support.wait_status(dev, lambda s: s["ws_clients"] == 0, timeout=3)
    conns = [ws_factory() for _ in range(4)]
    support.wait_status(dev, lambda s: s["ws_clients"] == 4, timeout=3)
    fifth = ws_factory(connect=False)
    ok = fifth.try_connect()
    if ok:
        status = fifth.wait(lambda o: "status" in o, timeout=1.5)
        closed = fifth.wait_closed(2.0)
        assert closed and status is None, "a 5th WebSocket connection was accepted and served"
    assert dev.status()["ws_clients"] == 4
    conns[0].close()
    support.wait_status(dev, lambda s: s["ws_clients"] == 3, timeout=3)
    again = ws_factory()
    assert again.wait(lambda o: "status" in o, timeout=1.5) is not None
    assert again.ping("ok") is not None


def test_ws_clients_count(dev, ws_factory):
    support.wait_status(dev, lambda s: s["ws_clients"] == 0, timeout=3)
    a = ws_factory()
    b = ws_factory()
    support.wait_status(dev, lambda s: s["ws_clients"] == 2, timeout=3)
    a.close()
    b.close()
    support.wait_status(dev, lambda s: s["ws_clients"] == 0, timeout=3)


def test_ws_auth_via_query_token(dev, hid, ws_factory, set_token):
    tok = "wsTok3n-%d" % (time.time() * 1000 % 100000)
    set_token(tok)
    for t in (None, "wrong-token"):
        c = ws_factory(token=t, connect=False)
        if c.try_connect():
            assert c.wait(lambda o: "status" in o, timeout=1.0) is None and c.wait_closed(2.0), \
                "WebSocket accepted with token %r" % t
        else:
            assert c.handshake_status in (401, None), c.handshake_error
    c = ws_factory(token=tok)
    assert c.wait(lambda o: "status" in o, timeout=1.5) is not None
    m = hid.mark()
    c.send({"key": "j", "id": 1})
    assert c.wait_ack(1, timeout=3).obj["ok"] is True
    assert len(hid.reports(m)) == 2


def test_ws_origin_check(dev, ws_factory, device_host):
    bad = ws_factory(origin="http://evil.example", connect=False)
    if bad.try_connect():
        assert bad.wait(lambda o: "status" in o, timeout=1.0) is None and bad.wait_closed(2.0), \
            "WebSocket with a foreign Origin was accepted"
    else:
        assert bad.handshake_status in (403, None), bad.handshake_error
    good = ws_factory(origin="http://" + support.host_only(device_host))
    assert good.wait(lambda o: "status" in o, timeout=1.5) is not None
    none = ws_factory(suppress_origin=True)
    assert none.wait(lambda o: "status" in o, timeout=1.5) is not None


def test_usb_not_ready_drop_not_testable_here():
    pytest.skip("'USB not ready -> WebSocket job dropped with usb_not_ready' needs an unconfigured device; "
                "not reproducible on this bench without root")
