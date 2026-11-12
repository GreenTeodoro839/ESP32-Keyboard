"""API.md section 3 / 9: absolute pointer position (move_to) on the third HID interface."""
from __future__ import annotations

import pytest

import hidhost
import support
from hidhost import ABS_INTF, MOUSE_INTF, abs_bytes, mouse_bytes, pixel_to_abs
from support import assert_nothing_emitted, expect_error


def abs_encoder(hid):
    """Reference encoder that knows the position last reported on the absolute interface
    (the device remembers it until the host re-enumerates; a host starts at (0, 0))."""
    e = hidhost.ReferenceEncoder()
    e.abs_pos = hid.abs_position() or (0, 0)
    return e


def away_from(dev, hid, x, y):
    """Make sure the last absolute position is not (x, y), so the next move_to there is one report."""
    if (hid.abs_position() or (0, 0)) == (x, y):
        support.do(hid, lambda: dev.move_to(x ^ 1, y))


@pytest.mark.parametrize("x,y", [(0, 0), (32767, 32767), (16384, 16384), (1, 32766), (12345, 678)])
def test_move_to_raw_is_one_report(dev, hid, x, y):
    away_from(dev, hid, x, y)
    _, reps = support.do(hid, lambda: dev.move_to(x, y))
    assert [(r.ep, r.data) for r in reps] == [("abs", abs_bytes(x, y))]


SCREENS = [((1920, 1080), (0, 0)), ((1920, 1080), (1919, 1079)), ((1920, 1080), (960, 540)),
           ((2560, 1440), (1279, 719)), ((3840, 2160), (3839, 1)), ((1366, 768), (683, 384)),
           ((1, 1), (0, 0)), ((32767, 32767), (32766, 16383))]


@pytest.mark.parametrize("screen,pt", SCREENS, ids=lambda v: "x".join(map(str, v)))
def test_move_to_screen_pixels(dev, hid, screen, pt):
    enc = abs_encoder(hid)
    exp = enc.move_to(pt[0], pt[1], screen=screen)
    _, reps = support.do(hid, lambda: dev.move_to(pt[0], pt[1], screen=screen))
    hidhost.assert_reports_equal(exp, reps, exact=True)
    a = hidhost.parse_abs(reps[-1].data)
    assert (hidhost.abs_to_pixel(a.x, screen[0]), hidhost.abs_to_pixel(a.y, screen[1])) == pt
    assert (a.x, a.y) == (pixel_to_abs(pt[0], screen[0]), pixel_to_abs(pt[1], screen[1]))


def test_move_to_same_position_twice_goes_through_the_neighbour(dev, hid):
    """A host drops an absolute position equal to the last one; the pointer may have moved since
    (relative mouse, another device), so the device detours over the point next to it."""
    away_from(dev, hid, 500, 600)
    _, reps = support.do(hid, lambda: dev.move_to(500, 600))
    assert [r.data for r in reps] == [abs_bytes(500, 600)]
    _, reps = support.do(hid, lambda: (dev.move(30, -20), dev.move_to(500, 600)))
    assert [(r.ep, r.data) for r in reps] == [("mouse", mouse_bytes(0, 30, -20)), ("abs", abs_bytes(501, 600)),
                                              ("abs", abs_bytes(500, 600))]
    away_from(dev, hid, 32767, 7)
    dev.move_to(32767, 7)
    _, reps = support.do(hid, lambda: dev.move_to(32767, 7))
    assert [r.data for r in reps] == [abs_bytes(32766, 7), abs_bytes(32767, 7)]


def test_move_to_reports_no_buttons_and_drag_works(dev, hid):
    """Buttons stay on the relative mouse interface: a drag is button down (mouse), positions (abs),
    button up (mouse), and the absolute reports never carry a button."""
    enc = abs_encoder(hid)
    exp = enc.move_to(100, 100) + enc.move_to(20000, 9000)
    _, reps = support.do(hid, lambda: dev.seq([{"button": "left", "action": "down"}, {"move_to": [100, 100]},
                                               {"move_to": [20000, 9000]}, {"button": "left", "action": "up"}]))
    assert [r.ep for r in reps] == ["mouse"] + ["abs"] * len(exp) + ["mouse"]
    assert reps[0].data == mouse_bytes(1) and reps[-1].data == mouse_bytes(0)
    assert [r.data for r in reps[1:-1]] == [e.data for e in exp]
    assert all(hidhost.parse_abs(r.data).buttons == 0 for r in reps[1:-1])


def test_move_to_with_held_button_and_key(dev, hid):
    dev.key_down("shift")
    dev.button_down("right")
    try:
        enc = abs_encoder(hid)
        exp = enc.move_to(4000, 5000)
        _, reps = support.do(hid, lambda: dev.move_to(4000, 5000))
        hidhost.assert_reports_equal(exp, reps, exact=True)
        assert dev.status()["hid"]["buttons"] == ["right"]
    finally:
        dev.button_up("right")
        dev.key_up("shift")


def test_release_does_not_report_on_the_absolute_interface(dev, hid):
    away_from(dev, hid, 777, 888)
    dev.move_to(777, 888)
    _, reps = support.do(hid, lambda: dev.seq([{"release": True}]))
    assert [r.ep for r in reps] == ["kbd", "mouse"]
    _, reps = support.do(hid, dev.release)
    assert "abs" not in {r.ep for r in reps}
    assert hid.abs_position() == (777, 888)


def test_move_to_skipped_in_boot_mouse_protocol(dev, hid):
    """A boot-protocol host (BIOS) does not read the absolute interface: move_to is skipped like scroll."""
    before = hid.abs_position()
    hid.set_protocol(MOUSE_INTF, "boot")
    support.wait_status(dev, lambda s: s["usb"]["mouse_protocol"] == "boot", timeout=2)
    m = hid.mark()
    res = dev.seq([{"move_to": [3000, 31000]}, {"move": [4, 4]}])
    assert res["ok"] is True
    reps = hid.collect(m, idle_ms=150)
    assert [(r.ep, r.data) for r in reps] == [("mouse", bytes([0, 4, 4]))]
    hid.set_protocol(MOUSE_INTF, "report")
    support.wait_status(dev, lambda s: s["usb"]["mouse_protocol"] == "report", timeout=2)
    assert hid.abs_position() == before
    enc = abs_encoder(hid)
    exp = enc.move_to(3000, 31000)
    _, reps = support.do(hid, lambda: dev.move_to(3000, 31000))
    hidhost.assert_reports_equal(exp, reps, exact=True)


def test_get_report_returns_last_absolute_position(dev, hid):
    away_from(dev, hid, 2222, 3333)
    dev.move_to(2222, 3333)
    hid.wait_idle(100, timeout=3)
    assert hid.get_report(ABS_INTF, length=16) == abs_bytes(2222, 3333)


def test_move_to_in_websocket_message(dev, hid, ws):
    away_from(dev, hid, 1111, 2222)
    m = hid.mark()
    ws.send({"move_to": [1111, 2222], "id": "abs"})
    ack = ws.wait_ack("abs", timeout=5)
    assert ack is not None and ack.obj["ok"] is True, ack
    reps = hid.collect(m, idle_ms=150)
    assert [(r.ep, r.data) for r in reps] == [("abs", abs_bytes(1111, 2222))]


@pytest.mark.parametrize("body", [
    {"move_to": [32768, 0]}, {"move_to": [0, -1]}, {"move_to": [1]}, {"move_to": [1, 2, 3]}, {"move_to": ["1", 2]},
    {"move_to": [1.5, 2]}, {"move_to": "10,10"}, {"move_to": None}, {"move_to": {"x": 1, "y": 2}},
    {"move_to": [1, 1], "screen": [0, 10]}, {"move_to": [1, 1], "screen": [10]}, {"move_to": [1, 1], "screen": [10, 32768]},
    {"move_to": [1, 1], "screen": "1920x1080"}, {"move_to": [1920, 0], "screen": [1920, 1080]},
    {"move_to": [0, 1080], "screen": [1920, 1080]}, {"move_to": [1, 1], "move": [1, 1]}], ids=lambda v: str(v))
def test_move_to_errors(dev, hid, body):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/mouse", body), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


def test_move_to_error_index_in_seq_is_atomic(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.seq([{"move": [5, 5]}, {"move_to": [1, 1]}, {"move_to": [1, 99999]}]), 400,
                 "bad_step", index=2)
    assert_nothing_emitted(hid, m)


def test_key_endpoint_rejects_move_to(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/key", {"move_to": [1, 1]}), 400, ("bad_request", "bad_step"))
    assert_nothing_emitted(hid, m)
