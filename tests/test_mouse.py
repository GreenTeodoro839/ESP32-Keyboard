"""API.md section 3: move, click, button, scroll; boot protocol mouse reports."""
from __future__ import annotations

import pytest

import hidhost
import support
from hidhost import MOUSE_INTF, mouse_bytes
from support import RANGE_CODES, TIMING_TOL_MS, assert_nothing_emitted, expect_error, mouse


def test_small_move_single_report(dev, hid):
    _, reps = support.do(hid, lambda: dev.move(10, -5))
    assert [r.data for r in reps] == [mouse_bytes(0, 10, -5)]


MOVES = [(300, -100, 127), (127, 127, 127), (128, 0, 127), (-1000, 1, 127), (5, -5, 1), (100, 37, 10),
         (0, -500, 50), (1, 0, 127), (-127, -128, 127), (32767, -32767, 127), (-32767, 0, 127), (2000, 999, 3)]


@pytest.mark.parametrize("dx,dy,step", MOVES, ids=lambda v: str(v))
def test_move_split(dev, hid, dx, dy, step):
    res, reps = support.do(hid, lambda: dev.move(dx, dy, step=step), timeout=120)
    assert all(r.ep == "mouse" for r in reps)
    problems = hidhost.check_move_split(reps, dx, dy, step)
    assert not problems, "\n".join(problems)
    assert all(r.data[0] == 0 for r in reps), "no button may be pressed during a plain move"


def test_move_default_step_is_127(dev, hid):
    _, reps = support.do(hid, lambda: dev.move(1270, 0))
    assert [hidhost.parse_mouse(r.data).dx for r in reps] == [127] * 10


def test_move_delay_ms(dev, hid):
    _, reps = support.do(hid, lambda: dev.move(50, 0, step=10, delay_ms=30))
    assert len(reps) == 5
    gaps = hidhost.gaps_ms(reps)
    assert min(gaps) >= 30 - TIMING_TOL_MS, gaps


def test_move_zero(dev, hid):
    """API.md does not say whether a zero move emits a report; if it does, it must be all zero."""
    _, reps = support.do(hid, lambda: dev.move(0, 0))
    assert len(reps) <= 1 and all(r.data == mouse_bytes(0) for r in reps)


@pytest.mark.parametrize("body", [
    {"move": [32768, 0]}, {"move": [0, -32768]}, {"move": [1, 1], "step": 0}, {"move": [1, 1], "step": 128},
    {"move": [1, 1], "step": -1}, {"move": [1, 1], "delay_ms": -1}], ids=lambda v: str(v))
def test_move_range_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("body", [{"move": [1]}, {"move": [1, 2, 3]}, {"move": ["1", 2]}, {"move": "10,10"},
                                  {"move": [1.5, 2]}, {"move": None}], ids=lambda v: str(v))
def test_move_type_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


def test_click_default(dev, hid):
    _, reps = support.do(hid, lambda: dev.click())
    assert [r.data for r in reps] == [mouse_bytes(1), mouse_bytes(0)]
    hold = (reps[1].t_ns - reps[0].t_ns) / 1e6
    assert 20 - TIMING_TOL_MS <= hold <= 20 + 40, "default hold %.1f ms, expected ~20" % hold


@pytest.mark.parametrize("name,bit", list(hidhost.BUTTON_BITS.items()))
def test_click_each_button(dev, hid, name, bit):
    _, reps = support.do(hid, lambda: dev.click(name))
    assert [r.data for r in reps] == [mouse_bytes(bit), mouse_bytes(0)]


@pytest.mark.parametrize("count,hold,interval", [(3, 30, 100), (2, None, None), (5, 0, 10)])
def test_click_count_hold_interval(dev, hid, count, hold, interval):
    _, reps = support.do(hid, lambda: dev.click("right", count=count, hold_ms=hold, interval_ms=interval))
    assert [r.data for r in reps] == [mouse_bytes(2), mouse_bytes(0)] * count
    h = 20 if hold is None else hold
    iv = 60 if interval is None else interval
    for i in range(count):
        g = (reps[2 * i + 1].t_ns - reps[2 * i].t_ns) / 1e6
        assert g >= h - TIMING_TOL_MS, "hold %.1f < %d" % (g, h)
    for i in range(count - 1):
        p2p = (reps[2 * i + 2].t_ns - reps[2 * i].t_ns) / 1e6
        r2p = (reps[2 * i + 2].t_ns - reps[2 * i + 1].t_ns) / 1e6
        # interval_ms may be measured release->press or press->press (API.md does not say)
        assert p2p >= iv - TIMING_TOL_MS and r2p >= max(0, iv - h) - TIMING_TOL_MS, (p2p, r2p)
        assert p2p <= iv + h + 60, p2p


def test_click_count_100(dev, hid):
    _, reps = support.do(hid, lambda: dev.click("left", count=100, hold_ms=0, interval_ms=0), timeout=60)
    assert [r.data for r in reps] == [mouse_bytes(1), mouse_bytes(0)] * 100


@pytest.mark.parametrize("body", [{"click": "left", "count": 0}, {"click": "left", "count": 101},
                                  {"click": "left", "hold_ms": -1}, {"click": "left", "interval_ms": -5}],
                         ids=lambda v: str(v))
def test_click_range_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("button", ["side", "", "wheel", 1, None], ids=lambda v: repr(v))
def test_unknown_button(dev, hid, button):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", {"click": button}), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("name,bit", list(hidhost.BUTTON_BITS.items()))
def test_button_down_up(dev, hid, name, bit):
    _, reps = support.do(hid, lambda: dev.button_down(name))
    assert [r.data for r in reps] == [mouse_bytes(bit)]
    assert dev.status()["hid"]["buttons"] == [name]
    _, reps = support.do(hid, lambda: dev.button_up(name))
    assert [r.data for r in reps] == [mouse_bytes(0)]
    assert dev.status()["hid"]["buttons"] == []


def test_drag_with_two_buttons(dev, hid):
    _, reps = support.do(hid, lambda: (dev.button_down("left"), dev.button_down("right"), dev.move(20, 10)))
    assert [r.data for r in reps] == [mouse_bytes(1), mouse_bytes(3), mouse_bytes(3, 20, 10)]
    assert sorted(dev.status()["hid"]["buttons"]) == ["left", "right"]
    _, reps = support.do(hid, lambda: (dev.button_up("left"), dev.button_up("right")))
    assert [r.data for r in reps] == [mouse_bytes(2), mouse_bytes(0)]


def test_click_while_other_button_held(dev, hid):
    dev.button_down("right")
    _, reps = support.do(hid, lambda: dev.click("left"))
    assert [r.data for r in reps] == [mouse_bytes(3), mouse_bytes(2)]
    dev.button_up("right")


@pytest.mark.parametrize("body", [{"button": "left"}, {"button": "left", "action": "tap"},
                                  {"button": "left", "action": "press"}], ids=lambda v: str(v))
def test_button_action_required(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, ("bad_step", "bad_request"), index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("v,h,wheel,pan", [(3, 0, 3, 0), (-3, 0, -3, 0), (0, 2, 0, 2), (0, -2, 0, -2),
                                           (1, -1, 1, -1), (127, 0, 127, 0)])
def test_scroll_signs(dev, hid, v, h, wheel, pan):
    """v > 0 = wheel up = positive wheel value; h > 0 = right = positive AC Pan."""
    _, reps = support.do(hid, lambda: dev.scroll(v, h))
    assert [r.data for r in reps] == [mouse_bytes(0, 0, 0, wheel, pan)]


def test_scroll_vertical_only_form(dev, hid):
    _, reps = support.do(hid, lambda: dev.post("/api/mouse", {"scroll": 2}))
    assert [r.data for r in reps] == [mouse_bytes(0, 0, 0, 2, 0)]


@pytest.mark.parametrize("v,h", [(1000, 0), (-500, 300), (32767, -32767)])
def test_scroll_large_values_split(dev, hid, v, h):
    _, reps = support.do(hid, lambda: dev.scroll(v, h), timeout=60)
    problems = hidhost.check_scroll_split(reps, v, h)
    assert not problems, problems


def test_scroll_zero(dev, hid):
    _, reps = support.do(hid, lambda: dev.scroll(0))
    assert len(reps) <= 1 and all(r.data == mouse_bytes(0) for r in reps)


@pytest.mark.parametrize("body", [{"scroll": 32768}, {"scroll": [0, -32768]}], ids=lambda v: str(v))
def test_scroll_range_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("body", [{"scroll": "3"}, {"scroll": [1, 2, 3]}, {"scroll": [1]}, {"scroll": 1.5}],
                         ids=lambda v: str(v))
def test_scroll_type_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


def test_mouse_endpoint_rejects_key_step(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", {"key": "a"}), 400, ("bad_step", "bad_request"))
    assert_nothing_emitted(hid, m)


def test_boot_protocol_mouse_sends_3_bytes(dev, hid):
    hid.set_protocol(MOUSE_INTF, "boot")
    support.wait_status(dev, lambda s: s["usb"]["mouse_protocol"] == "boot", timeout=2)
    _, reps = support.do(hid, lambda: (dev.move(10, -5), dev.click("right")))
    assert [r.data for r in reps] == [bytes([0, 10, 251]), bytes([2, 0, 0]), bytes([0, 0, 0])]
    hid.set_protocol(MOUSE_INTF, "report")
    support.wait_status(dev, lambda s: s["usb"]["mouse_protocol"] == "report", timeout=2)
    _, reps = support.do(hid, lambda: dev.move(1, 1))
    assert [r.data for r in reps] == [mouse_bytes(0, 1, 1)]


def test_boot_protocol_keyboard_same_8_byte_reports(dev, hid):
    hid.set_protocol(hidhost.KBD_INTF, "boot")
    support.wait_status(dev, lambda s: s["usb"]["kbd_protocol"] == "boot", timeout=2)
    exp = hidhost.ReferenceEncoder().text("Ab1")
    _, reps = support.do(hid, lambda: dev.type("Ab1"))
    hidhost.assert_reports_equal(exp, reps, exact=True)


def test_queued_moves_may_merge_but_sum_is_exact(dev, hid):
    """Adjacent queued pure-move jobs may be merged (API.md 3.2); the total must be exact."""
    m = hid.mark()
    dev.seq([{"delay": 800}], wait=False)
    for _ in range(10):
        dev.move(3, 1, wait=False)
    dev.wait_idle(timeout=10)
    reps = mouse(hid.collect(m, idle_ms=150))
    s = hidhost.mouse_summary(reps)
    assert (s.dx, s.dy) == (30, 10)
    assert 1 <= len(reps) <= 10
    for r in reps:
        p = hidhost.parse_mouse(r.data)
        assert abs(p.dx) <= 127 and abs(p.dy) <= 127
