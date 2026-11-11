"""API.md section 3.2 and 4: sync/async jobs, FIFO across sources, busy, /api/release, timeouts."""
from __future__ import annotations

import threading
import time

import pytest

import espkb
import hidhost
import support
from support import assert_nothing_emitted, expect_error


def mouse_bytes_zero():
    return hidhost.mouse_bytes(0)


def client(dev, timeout=10):
    """A second, independent client (separate connections) to the same device."""
    return support.GuardedESPKB(dev.base_url, dev.hid, timeout=timeout)


def test_sync_returns_after_completion(dev, hid):
    text = "synchronous-job-30-characters!"
    exp = hidhost.ReferenceEncoder().text(text, 10)
    m = hid.mark()
    res = dev.type(text, delay_ms=10)
    t_resp = time.monotonic_ns()
    reps = hid.reports(m)
    if len(reps) < len(exp):  # allow a little USB/host latency, but not a whole job
        time.sleep(0.05)
        reps = hid.reports(m)
    assert len(reps) == len(exp), "response arrived before the job finished (%d/%d reports)" % (len(reps), len(exp))
    assert reps[-1].t_ns <= t_resp + 50_000_000
    assert isinstance(res["job"], int) and isinstance(res["elapsed_ms"], int)
    assert res["elapsed_ms"] >= (len(exp) - 1) * 10 - 5
    assert "queued" not in res or res["queued"] is False


def test_async_returns_immediately(dev, hid):
    text = "a" * 100
    exp = hidhost.ReferenceEncoder().text(text, 20)
    m = hid.mark()
    t0 = time.monotonic()
    res = dev.type(text, delay_ms=20, wait=False)
    dt = time.monotonic() - t0
    assert res.get("queued") is True and isinstance(res["job"], int)
    assert dt < 1.0, "async request took %.2fs" % dt
    assert len(hid.reports(m)) < len(exp) // 2
    st = dev.status()["hid"]
    assert st["busy"] is True
    reps = hid.collect(m, idle_ms=200, min_count=len(exp), timeout=30)
    hidhost.assert_reports_equal(exp, reps)


def test_job_ids_increase(dev):
    ids = [dev.seq([{"delay": 0}])["job"] for _ in range(3)]
    ids.append(dev.seq([{"delay": 0}], wait=False)["job"])
    assert ids == sorted(ids) and len(set(ids)) == 4


def test_fifo_across_http_clients(dev, hid):
    a, b, c = client(dev), client(dev), client(dev)
    m = hid.mark()
    a.type("a" * 20, delay_ms=20, wait=False)
    b.type("b" * 5, delay_ms=1, wait=False)
    c.type("c" * 5, delay_ms=1, wait=False)
    reps = hid.collect(m, idle_ms=200, min_count=60, timeout=30)
    assert hidhost.kbd_text(reps) == "a" * 20 + "b" * 5 + "c" * 5


def test_fifo_across_http_and_websocket(dev, hid, ws):
    m = hid.mark()
    dev.type("a" * 20, delay_ms=20, wait=False)
    wm = ws.mark()
    ws.send({"text": "bbbbb", "delay_ms": 1, "id": "fifo-b"})
    support.wait_status(dev, lambda s: s["hid"]["queued"] >= 1, timeout=2, interval=0.01)
    dev.type("ccccc", delay_ms=1, wait=False)
    reps = hid.collect(m, idle_ms=200, min_count=60, timeout=30)
    assert hidhost.kbd_text(reps) == "a" * 20 + "bbbbb" + "ccccc"
    ack = ws.wait_ack("fifo-b", timeout=5, start=wm)
    assert ack is not None and ack.obj.get("ok") is True
    b_done = [r for r in reps if r.data == bytes(8)][24].t_ns  # release of the 5th 'b'
    assert ack.t_ns >= b_done


def test_concurrent_clients_do_not_interleave(dev, hid):
    letters = "qwerty"
    errors = []
    m = hid.mark()

    def work(ch):
        try:
            client(dev, timeout=60).type(ch * 8, delay_ms=1)
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=work, args=(c,)) for c in letters]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    text = hidhost.kbd_text(hid.collect(m, idle_ms=200, min_count=96))
    assert len(text) == 48
    runs = [text[i:i + 8] for i in range(0, 48, 8)]
    assert sorted(r[0] for r in runs) == sorted(letters)
    for r in runs:
        assert r == r[0] * 8, "jobs interleaved: %r" % text


def test_queue_full_returns_busy(dev, hid):
    dev.seq([{"delay": 5000}], wait=False)
    support.wait_status(dev, lambda s: s["hid"]["busy"], timeout=2, interval=0.01)
    accepted = 0
    busy = None
    for _ in range(40):
        try:
            dev.seq([{"delay": 1}], wait=False)
            accepted += 1
        except espkb.ESPKBError as e:
            busy = e
            break
    assert busy is not None, "queue never filled after %d jobs" % accepted
    assert (busy.status, busy.code) == (503, "busy"), busy
    assert accepted in (31, 32), "queue accepted %d jobs behind the running one (capacity 32)" % accepted
    st = dev.status()["hid"]
    assert st["queued"] == accepted
    res = dev.release()
    assert res["aborted"] == accepted + 1, "aborted=%r, expected running + %d queued" % (res["aborted"], accepted)
    st = support.wait_status(dev, lambda s: not s["hid"]["busy"] and s["hid"]["queued"] == 0, timeout=2)
    assert st["hid"]["queued"] == 0


def test_release_aborts_long_job_promptly(dev, hid):
    text = "abcdefghij" * 200
    result = {}
    m = hid.mark()

    def long_job():
        try:
            result["res"] = client(dev, timeout=120).type(text, delay_ms=5)
        except espkb.ESPKBError as e:
            result["err"] = e
        result["t"] = time.monotonic()
    t = threading.Thread(target=long_job)
    t.start()
    hid.wait_reports(20, m, timeout=5)
    time.sleep(0.5)
    t0 = time.monotonic()
    res = client(dev).release()
    t_rel = time.monotonic()
    assert t_rel - t0 < 0.5, "/api/release took %.2fs" % (t_rel - t0)
    assert res["aborted"] >= 1
    t.join(10)
    assert not t.is_alive(), "aborted sync request did not return"
    e = result.get("err")
    assert e is not None and (e.status, e.code) == (409, "aborted"), result
    assert result["t"] - t_rel < 1.0, "aborted request returned %.2fs after release" % (result["t"] - t_rel)
    hid.wait_idle(500, timeout=10)
    reps = support.kbd(hid.reports(m))
    assert reps[-1].data == bytes(8), "last report after abort is not all-released"
    late = [r for r in reps if r.t_ns > t_rel * 1e9 + 200e6]
    assert not late, "%d reports more than 200 ms after /api/release returned" % len(late)
    typed = hidhost.kbd_text(reps)
    assert text.startswith(typed) and 0 < len(typed) < len(text)


def test_release_flushes_queue_and_queued_sync_requests_get_aborted(dev, hid):
    results = {}

    def job(name, steps):
        try:
            results[name] = client(dev, timeout=60).seq(steps)
        except espkb.ESPKBError as e:
            results[name] = e
    ta = threading.Thread(target=job, args=("a", [{"delay": 8000}]))
    ta.start()
    support.wait_status(dev, lambda s: s["hid"]["busy"], timeout=2, interval=0.01)
    tb = threading.Thread(target=job, args=("b", [{"key": "a"}]))
    tb.start()
    support.wait_status(dev, lambda s: s["hid"]["queued"] >= 1, timeout=2, interval=0.01)
    m = hid.mark()
    res = dev.release()
    assert res["aborted"] >= 2
    ta.join(5)
    tb.join(5)
    for name in ("a", "b"):
        e = results.get(name)
        assert isinstance(e, espkb.ESPKBError) and (e.status, e.code) == (409, "aborted"), results
    # API.md §10: /api/release always sends one all-released report per interface, nothing else
    hid.wait_idle(300, timeout=5)
    assert sorted((r.ep, r.data) for r in hid.reports(m)) == [("kbd", bytes(8)), ("mouse", mouse_bytes_zero())]


def test_release_when_idle(dev):
    res = dev.release()
    assert res == {"ok": True, "aborted": 0} or (res["ok"] is True and res["aborted"] == 0)


def test_status_responsive_during_long_job(dev, hid):
    dev.type("x" * 300, delay_ms=10, wait=False)
    lat = []
    busy_seen = False
    for _ in range(20):
        t0 = time.monotonic()
        st = dev.status()
        lat.append(time.monotonic() - t0)
        busy_seen |= st["hid"]["busy"]
        time.sleep(0.05)
    dev.release()
    assert busy_seen
    assert max(lat) < 1.0, "max /api/status latency %.2fs during a job" % max(lat)
    assert support.median(lat) < 0.3, "median /api/status latency %.2fs" % support.median(lat)


def test_busy_and_queued_counts(dev):
    dev.seq([{"delay": 2000}], wait=False)
    dev.seq([{"delay": 1}], wait=False)
    dev.seq([{"delay": 1}], wait=False)
    st = dev.status()["hid"]
    assert st["busy"] is True and st["queued"] == 2
    dev.release()


@pytest.mark.parametrize("ep,action", [("kbd", lambda d: d.key("a")), ("mouse", lambda d: d.click("left"))])
def test_timeout_when_host_stops_polling_and_recovery(dev, hid, ep, action):
    before = dev.status()["hid"]
    hid.pause(ep)
    try:
        t0 = time.monotonic()
        expect_error(lambda: action(dev), 504, "timeout")
        dt = time.monotonic() - t0
        assert 0.9 <= dt <= 3.0, "timeout after %.2fs, expected ~1s" % dt
        # status stays responsive and the internal state is cleared
        t1 = time.monotonic()
        st = dev.status()["hid"]
        assert time.monotonic() - t1 < 1.0
        assert st["keys"] == [] and st["buttons"] == []
        assert st["jobs_failed"] == before["jobs_failed"] + 1
    finally:
        hid.resume(ep)
    # once the host polls again, whatever was pending is delivered and ends released
    hid.wait_idle(300, timeout=5)
    assert hid.kbd_state().empty and hid.mouse_buttons() == 0, \
        "after recovery the host still sees something pressed: %s / 0x%02x" % (
            hidhost.describe_kbd(hid.kbd_state().raw), hid.mouse_buttons())
    _, reps = support.do(hid, lambda: dev.seq([{"key": "b"}, {"click": "right"}]))
    hidhost.assert_reports_equal(hidhost.ReferenceEncoder().steps([{"key": "b"}, {"click": "right"}]), reps)


def test_timeout_reported_over_websocket_without_id(dev, hid, ws):
    hid.pause("kbd")
    try:
        wm = ws.mark()
        ws.send({"key": "a"})
        msg = ws.wait(lambda o: o.get("error") == "timeout", timeout=4, start=wm)
        assert msg is not None, "no timeout error on the WebSocket: %r" % ws.non_status(wm)
        assert msg.obj.get("message")
    finally:
        hid.resume("kbd")
    hid.wait_idle(300)


def test_timeout_ack_with_id(dev, hid, ws):
    hid.pause("mouse")
    try:
        wm = ws.mark()
        ws.send({"click": "left", "id": 41})
        ack = ws.wait_ack(41, timeout=4, start=wm)
        assert ack is not None and ack.obj.get("ok") is False and ack.obj.get("error") == "timeout", ack
    finally:
        hid.resume("mouse")
    hid.wait_idle(300)


def test_status_responsive_while_host_paused(dev, hid):
    hid.pause("kbd")
    try:
        dev.key("a", wait=False)
        lat = []
        for _ in range(5):
            t0 = time.monotonic()
            dev.status()
            lat.append(time.monotonic() - t0)
        assert max(lat) < 1.0, lat
    finally:
        hid.resume("kbd")
    hid.wait_idle(300)


def test_usb_not_ready_and_suspended_paths_not_testable_here():
    pytest.skip("usb_not_ready / usb_suspended / remote wakeup need the host to unconfigure or suspend the "
                "device, which this bench cannot do without root; covered only by status-field checks")
