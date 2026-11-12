"""Offline unit tests of the pure logic in tools/hidhost.py (no USB device needed)."""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import threading
import time

import pytest

import hidhost as h
from hidhost import Expected, Report, abs_bytes, kbd_bytes, mouse_bytes

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
API_MD = os.path.join(ROOT, "docs", "API.md")
HIDHOST_PY = os.path.join(ROOT, "tools", "hidhost.py")


def K(*keys, mods=0):
    return kbd_bytes(mods, keys)


SHIFT = 0x02
RSHIFT = 0x20
CTRL = 0x01


# ---------------------------------------------------------------------------
# key table vs API.md section 8
# ---------------------------------------------------------------------------

def _api_md_section8():
    txt = open(API_MD, encoding="utf-8").read()
    start = txt.index("## 8.")
    end = txt.index("## 9.")
    return txt[start:end]


def test_key_table_matches_api_md():
    sec = _api_md_section8()
    found = {}
    # single entries: `name`(`alias`、`alias`) NUMBER   (the grave alias is written `` ` ``)
    for m in re.finditer(r"`([a-z0-9_]+)`(?:\(((?:[^()]|\(\))*?)\))?\s+(\d+)(?![–\d])", sec):
        name, aliases, num = m.group(1), m.group(2), int(m.group(3))
        found[name] = num
        if aliases:
            for a in re.findall(r"``\s(.)\s``|`([^`]+)`", aliases):
                found[a[0] or a[1]] = num
    # ranges: `f1`–`f12` 58–69, `kp_1`–`kp_9` 89–97
    for m in re.finditer(r"`([a-z_]+?)(\d+)`–`[a-z_]+?(\d+)`\s+(\d+)–(\d+)", sec):
        prefix, a, b, u0, u1 = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        assert b - a == u1 - u0
        for i in range(a, b + 1):
            found["%s%d" % (prefix, i)] = u0 + (i - a)
    assert len(found) > 100, "API.md parse failed"
    for name, usage in found.items():
        assert h.KEY_NAMES.get(name) == usage, "%s: table %r, API.md %r" % (name, h.KEY_NAMES.get(name), usage)
    # letters/digits line: a-z 4-29, 1-9 0 30-39
    assert "`a`–`z`（4–29）" in sec and "`0`（30–39）" in sec
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        assert h.KEY_NAMES[c] == 4 + i
    for i, c in enumerate("1234567890"):
        assert h.KEY_NAMES[c] == 30 + i
    # nothing in our table that API.md does not document
    documented = set(found) | set("abcdefghijklmnopqrstuvwxyz1234567890")
    assert set(h.KEY_NAMES) == documented, set(h.KEY_NAMES) ^ documented
    assert "+" not in h.KEY_NAMES
    assert h.USAGE_POWER not in h.KEY_NAMES.values()


def test_parse_key_spec():
    assert h.parse_key_spec("CTRL+Alt+Del") == [224, 226, 76]
    assert h.parse_key_spec(40) == [40]
    assert h.parse_key_spec("0x28") == [40]
    assert h.parse_key_spec("shift+=") == [225, 46]
    assert h.parse_key_spec("ctrl+-") == [224, 45]
    assert h.parse_key_spec("`") == [53]
    for bad in ("nosuch", "", "+", "ctrl+", "a++b", 256, -1, "0x100", "0xzz", True, None, 1.5, [4]):
        with pytest.raises(h.EncodeError):
            h.parse_key_spec(bad)


# ---------------------------------------------------------------------------
# text encode / decode
# ---------------------------------------------------------------------------

def test_char_map_covers_printable_ascii_exactly_once():
    assert len(h.PRINTABLE_ASCII) == 95
    for c in h.PRINTABLE_ASCII:
        assert c in h.CHAR_TO_KEY
    pairs = [h.CHAR_TO_KEY[c] for c in h.PRINTABLE_ASCII]
    assert len(set(pairs)) == 95


@pytest.mark.parametrize("caps", [False, True])
def test_printable_ascii_round_trip(caps):
    text = h.PRINTABLE_ASCII + "\n\t\b\x1b"
    reps = h.ReferenceEncoder(caps=caps).text(text)
    assert len(reps) == 2 * len(text)
    assert h.kbd_text(reps, caps=caps) == text
    for i in range(0, len(reps), 2):
        assert reps[i + 1].data == bytes(8), "every character is press + all-zero release"


def test_shift_and_caps_semantics():
    reps = h.ReferenceEncoder().text("aA1!")
    assert [r.data for r in reps[::2]] == [K(4), K(4, mods=SHIFT), K(30), K(30, mods=SHIFT)]
    reps = h.ReferenceEncoder(caps=True).text("aA1!")
    # CapsLock compensation: Shift inverted for letters only
    assert [r.data for r in reps[::2]] == [K(4, mods=SHIFT), K(4), K(30), K(30, mods=SHIFT)]
    assert h.kbd_text(reps, caps=True) == "aA1!"
    assert h.kbd_text(reps, caps=False) == "Aa1!"


def test_right_shift_decodes():
    reps = [K(4, mods=RSHIFT), K(), K(31, mods=RSHIFT), K()]
    assert h.kbd_text(reps) == "A@"


def test_decoder_strict_and_tokens():
    reps = [K(6, mods=CTRL), K(), K(58), K(), K(4), K()]
    with pytest.raises(h.DecodeError) as ei:
        h.kbd_text(reps)
    assert ei.value.index == 0
    assert h.kbd_text(reps, strict=False) == "<ctrl+c><f1>a"
    assert h.kbd_text([K(40, mods=SHIFT), K()], strict=False) == "<shift+enter>"


def test_track_capslock():
    reps = [K(4), K(), K(57), K(), K(4), K(), K(57), K(), K(4), K()]
    assert h.kbd_text(reps, track_capslock=True) == "aAa"


def test_overlapping_presses_rollover():
    # a down, b down while a held, a up, b up -> "ab"
    reps = [K(4), K(4, 5), K(5), K()]
    assert h.kbd_text(reps) == "ab"
    evs = [str(e) for e in h.kbd_events(reps)]
    assert evs == ["down a", "down b", "up a", "up b"]
    # slot order changes are not events
    assert [str(e) for e in h.kbd_events([K(4, 5), K(5, 4), K()])] == ["down a", "down b", "up b", "up a"]
    # six keys at once and a seventh replacing one
    reps = [K(4, 5, 6, 7, 8, 9), K(5, 6, 7, 8, 9, 10), K()]
    assert h.kbd_text(reps) == "abcdefg"


def test_error_rollover_report():
    reps = [K(4), bytes([0, 0] + [1] * 6), K()]
    with pytest.raises(h.DecodeError):
        h.kbd_text(reps)
    assert h.kbd_text(reps, strict=False) == "a<rollover>"
    evs = h.kbd_events(reps)
    assert [e.kind for e in evs] == ["down", "rollover", "up"]


def test_modifier_and_key_in_same_report_order():
    evs = h.kbd_events([K(4, mods=SHIFT), K()])
    assert [str(e) for e in evs] == ["down shift", "down a", "up shift", "up a"]
    assert evs[1].mods == SHIFT


def test_text_line_endings_and_offsets():
    assert h.normalize_text("a\r\nb\rc\n\rd") == "a\nb\nc\n\nd"
    reps = h.ReferenceEncoder().text("a\r\nb")
    assert len(reps) == 6 and h.kbd_text(reps) == "a\nb"
    assert h.unsupported_chars("abéc中\U0001F600\x00\x7f") == [
        (2, 2, "é"), (4, 5, "中"), (5, 8, "\U0001F600"), (6, 12, "\x00"), (7, 13, "\x7f")]
    with pytest.raises(h.EncodeError) as ei:
        h.ReferenceEncoder().text("abcé")
    assert ei.value.offset == 3


def test_repeated_characters_have_releases():
    reps = h.ReferenceEncoder().text("aa")
    assert [r.data for r in reps] == [K(4), K(), K(4), K()]
    assert h.kbd_text(reps) == "aa"
    # without the release in between a host would see one keystroke only
    assert h.kbd_text([K(4), K(4), K()]) == "a"


def test_text_gaps():
    reps = h.ReferenceEncoder(key_delay_ms=5).text("ab", delay_ms=40)
    assert [r.gap_ms for r in reps] == [0, 40, 40, 40]


# ---------------------------------------------------------------------------
# key / mouse / step encoding
# ---------------------------------------------------------------------------

def test_key_combo_press_order_release_reverse():
    reps = h.ReferenceEncoder().key("ctrl+shift+esc")
    assert [r.data for r in reps] == [K(mods=1), K(mods=3), K(41, mods=3), K(mods=3), K(mods=1), K()]
    reps = h.ReferenceEncoder().key("a+b")
    assert [r.data for r in reps] == [K(4), K(4, 5), K(4), K()]


def test_key_repeat_hold_delay():
    reps = h.ReferenceEncoder().key("a", repeat=3, hold_ms=100, delay_ms=7)
    assert [r.data for r in reps] == [K(4), K()] * 3
    assert [r.gap_ms for r in reps] == [0, 100, 7, 100, 7, 100]


def test_key_down_up_and_held_state():
    e = h.ReferenceEncoder()
    assert [r.data for r in e.key("shift", "down")] == [K(mods=SHIFT)]
    assert e.held == [225]
    assert [r.data for r in e.text("a")] == [K(4, mods=SHIFT), K(mods=SHIFT)]
    assert [r.data for r in e.key("ctrl+c", "down")] == [K(mods=SHIFT | CTRL), K(6, mods=SHIFT | CTRL)]
    assert [r.data for r in e.key("ctrl+c", "up")] == [K(mods=SHIFT | CTRL), K(mods=SHIFT)]
    assert e.key("b", "up") == []
    # API.md section 10: release always sends an all-released report on both interfaces
    assert [(r.ep, r.data) for r in e.release()] == [("kbd", K()), ("mouse", mouse_bytes(0))]
    assert [(r.ep, r.data) for r in e.release()] == [("kbd", K()), ("mouse", mouse_bytes(0))]


def test_six_key_limit():
    e = h.ReferenceEncoder()
    assert len(e.key("a+b+c+d+e+f")) == 12
    assert len(e.key("ctrl+shift+alt+gui+a+b+c+d+e+f")) == 20
    with pytest.raises(h.EncodeError):
        h.ReferenceEncoder().key("a+b+c+d+e+f+g")
    steps = [{"key": c, "action": "down"} for c in "abcdef"] + [{"key": "g", "action": "down"}]
    with pytest.raises(h.EncodeError) as ei:
        h.ReferenceEncoder().steps(steps)
    assert ei.value.index == 6


def test_numeric_modifier_goes_to_bitmap():
    assert [r.data for r in h.ReferenceEncoder().key(224)] == [K(mods=CTRL), K()]
    assert [r.data for r in h.ReferenceEncoder().key("0xe5")] == [K(mods=RSHIFT), K()]


def test_click_and_buttons():
    e = h.ReferenceEncoder()
    reps = e.click("right", count=2, hold_ms=30, interval_ms=100)
    assert [r.data for r in reps] == [mouse_bytes(2), mouse_bytes(0)] * 2
    assert [r.gap_ms for r in reps] == [0, 30, 100, 30]
    assert [r.data for r in e.button("left", "down")] == [mouse_bytes(1)]
    assert [r.data for r in e.move(5, -3)] == [mouse_bytes(1, 5, -3)]
    assert [r.data for r in e.click("middle")] == [mouse_bytes(5), mouse_bytes(1)]
    assert e.held_buttons == ["left"]
    assert [(r.ep, r.data) for r in e.release()] == [("kbd", K()), ("mouse", mouse_bytes(0))]
    for name, bit in h.BUTTON_BITS.items():
        assert h.ReferenceEncoder().click(name)[0].data == mouse_bytes(bit)


def test_boot_mouse_reports_are_3_bytes():
    e = h.ReferenceEncoder(boot_mouse=True)
    assert [r.data for r in e.move(10, -5)] == [bytes([0, 10, 251])]
    assert e.scroll(3) == []
    assert e.move_to(100, 100) == [] and e.abs_pos == (0, 0)


def test_scroll_sign_and_split():
    assert [r.data for r in h.ReferenceEncoder().scroll(3)] == [mouse_bytes(0, 0, 0, 3, 0)]
    assert [r.data for r in h.ReferenceEncoder().scroll(-3, 2)] == [mouse_bytes(0, 0, 0, -3, 2)]
    reps = h.ReferenceEncoder().scroll(1000, -300)
    assert h.check_scroll_split(reps, 1000, -300) == []
    assert h.check_scroll_split([mouse_bytes(0, 0, 0, 3)], -3) != []


def test_steps_mixed_order_and_delay_gap():
    steps = [{"key": "a"}, {"move": [5, 0]}, {"delay": 300}, {"click": "left"}, {"text": "B"}, {"scroll": [1, -1]}]
    reps = h.ReferenceEncoder().steps(steps)
    assert [(r.ep, r.data) for r in reps] == [
        ("kbd", K(4)), ("kbd", K()), ("mouse", mouse_bytes(0, 5, 0)), ("mouse", mouse_bytes(1)),
        ("mouse", mouse_bytes(0)), ("kbd", K(5, mods=SHIFT)), ("kbd", K()), ("mouse", mouse_bytes(0, 0, 0, 1, -1))]
    assert reps[3].gap_ms == 300


def test_move_to_raw_and_screen():
    e = h.ReferenceEncoder()
    assert [(r.ep, r.data) for r in e.move_to(100, 32767)] == [("abs", abs_bytes(100, 32767))]
    assert abs_bytes(0x1234, 0x0201) == bytes([0, 0x34, 0x12, 0x01, 0x02, 0, 0])
    assert h.parse_abs(abs_bytes(5, 32767)).y == 32767
    reps = e.move_to(960, 540, screen=[1920, 1080])
    assert [r.data for r in reps] == [abs_bytes(16392, 16399)]
    assert e.abs_pos == (16392, 16399)


def test_move_to_same_position_goes_through_the_neighbour():
    e = h.ReferenceEncoder()
    # a host starts at (0, 0): moving there first needs the detour as well
    assert [r.data for r in e.move_to(0, 0)] == [abs_bytes(1, 0), abs_bytes(0, 0)]
    assert [r.data for r in e.move_to(32767, 5)] == [abs_bytes(32767, 5)]
    assert [r.data for r in e.move_to(32767, 5)] == [abs_bytes(32766, 5), abs_bytes(32767, 5)]
    # move_to does not touch the relative mouse or the keyboard, and release does not touch move_to
    assert [r.ep for r in e.release()] == ["kbd", "mouse"]
    assert e.abs_pos == (32767, 5)


@pytest.mark.parametrize("n", [1, 2, 3, 7, 640, 1080, 1366, 1920, 2160, 2560, 3840, 7680, 16384, 20000, 32767])
def test_pixel_to_abs_lands_on_the_pixel(n):
    vals = [h.pixel_to_abs(p, n) for p in range(n)]
    assert [h.abs_to_pixel(v, n) for v in vals] == list(range(n))
    assert all(0 <= v <= 32767 for v in vals) and vals == sorted(vals)


def test_abs_report_description():
    assert h.describe("abs", abs_bytes(10, 20)) == "abs x=10 y=20"
    assert "INVALID" in h.describe("abs", bytes(5))


BAD_STEPS = [
    ({}, "exactly one verb"), ({"foo": 1}, "exactly one verb"), ({"key": "a", "move": [1, 1]}, "exactly one verb"),
    ({"key": True}, "string or integer"), ({"text": 5}, "string"), ({"delay": "5"}, "integer"),
    ({"move": [1]}, "move"), ({"move": [1, 2, 3]}, "move"), ({"move": ["1", 2]}, "move"),
    ({"move": [1.5, 2]}, "move"), ({"move": [32768, 0]}, "range"), ({"move": [1, 1], "step": 0}, "step"),
    ({"move": [1, 1], "step": 128}, "step"), ({"click": "side"}, "button"), ({"click": "left", "count": 0}, "count"),
    ({"click": "left", "count": 101}, "count"), ({"button": "left"}, "action"), ({"button": "left", "action": "tap"}, "action"),
    ({"scroll": "3"}, "scroll"), ({"scroll": [1, 2, 3]}, "scroll"), ({"scroll": -32768}, "range"),
    ({"delay": 60001}, "range"), ({"delay": -1}, "range"), ({"key": "a", "repeat": 0}, "repeat"),
    ({"key": "a", "repeat": 1001}, "repeat"), ({"key": "a", "ttl_ms": 0}, "ttl_ms"),
    ({"key": "a", "ttl_ms": 600001}, "ttl_ms"), ({"key": "a", "action": "press"}, "action"),
    ({"key": 256}, "range"), ({"release": False}, "release"), ({"text": "é"}, "unsupported"),
    ("notadict", "object"),
    ({"move_to": [1]}, "move_to"), ({"move_to": [1.5, 2]}, "move_to"), ({"move_to": "1,2"}, "move_to"),
    ({"move_to": [-1, 0]}, "range"), ({"move_to": [0, 32768]}, "range"),
    ({"move_to": [1, 1], "screen": [0, 10]}, "screen"), ({"move_to": [1, 1], "screen": [10]}, "screen"),
    ({"move_to": [10, 1], "screen": [10, 10]}, "outside"), ({"move_to": [1, 1], "move": [1, 1]}, "exactly one verb"),
]


@pytest.mark.parametrize("step,why", BAD_STEPS, ids=[str(s[0]) for s in BAD_STEPS])
def test_validate_rejects(step, why):
    with pytest.raises(h.EncodeError) as ei:
        h.ReferenceEncoder().steps([{"key": "a"}, step])
    assert ei.value.index == 1
    assert why in str(ei.value)


def test_validate_accepts_boundaries_and_unknown_fields():
    ok = [{"key": "a", "comment": "x"}, {"move": [32767, -32767], "step": 127}, {"move": [1, 1], "step": 1},
          {"click": "forward", "count": 100, "hold_ms": 0, "interval_ms": 0}, {"delay": 0}, {"delay": 60000},
          {"scroll": [32767, -32767]}, {"key": "a", "repeat": 1000}, {"key": "a", "ttl_ms": 600000, "action": "down"},
          {"key": 255}, {"release": True}, {"move_to": [0, 0]}, {"move_to": [32767, 32767]},
          {"move_to": [9, 9], "screen": [10, 10]}, {"move_to": [32766, 0], "screen": [32767, 1]}]
    h.ReferenceEncoder().steps(ok)


def test_atomic_encoding_leaves_state_untouched():
    e = h.ReferenceEncoder()
    with pytest.raises(h.EncodeError):
        e.steps([{"key": "shift", "action": "down"}, {"key": "nosuch"}])
    assert e.held == []


# ---------------------------------------------------------------------------
# move splitting
# ---------------------------------------------------------------------------

def test_split_move_examples():
    assert h.split_move(10, -5) == [(10, -5)]
    assert h.split_move(0, 0) == []
    assert h.split_move(300, -100) == [(100, -33), (100, -34), (100, -33)]
    assert h.split_move(5, -5, step=1) == [(1, -1)] * 5
    assert len(h.split_move(32767, -32767)) == 259


def test_split_move_properties_random():
    rnd = random.Random(42)
    for _ in range(2000):
        dx, dy = rnd.randint(-32767, 32767), rnd.randint(-32767, 32767)
        if rnd.random() < 0.3:
            dx, dy = rnd.randint(-400, 400), rnd.randint(-400, 400)
        step = rnd.choice([1, 2, 7, 10, 50, 100, 126, 127, rnd.randint(1, 127)])
        if abs(dx) + abs(dy) > 5000 and step < 20:
            step = 127
        parts = h.split_move(dx, dy, step)
        reps = [mouse_bytes(0, a, b) for a, b in parts]
        assert h.check_move_split(reps, dx, dy, step, max_extra_reports=0) == [], (dx, dy, step)


def test_check_move_split_detects_problems():
    assert any("sum" in p for p in h.check_move_split([mouse_bytes(0, 10, 0)], 11, 0))
    assert any("exceeds" in p for p in h.check_move_split([mouse_bytes(0, 100, 0)], 100, 0, step=50))
    assert any("against" in p for p in h.check_move_split(
        [mouse_bytes(0, 60, 0), mouse_bytes(0, -10, 0)], 50, 0))
    # x fully first, then y: not a straight line
    reps = [mouse_bytes(0, 100, 0), mouse_bytes(0, 0, 100)]
    assert any("straight line" in p for p in h.check_move_split(reps, 100, 100))
    reps = [mouse_bytes(0, 1, 0)] * 10
    assert any("reports for a move" in p for p in h.check_move_split(reps, 10, 0))


def test_mouse_summary():
    reps = [mouse_bytes(1), mouse_bytes(1, 5, -3), mouse_bytes(3, 0, 0, 2, -1), mouse_bytes(0)]
    s = h.mouse_summary(reps)
    assert (s.dx, s.dy, s.wheel, s.pan, s.n_reports, s.final_buttons) == (5, -3, 2, -1, 4, 0)
    assert s.button_events == [("down", "left", 0), ("down", "right", 2), ("up", "left", 3), ("up", "right", 3)]
    assert s.clicks == {"left": 1, "right": 1}
    assert h.parse_mouse(bytes([0, 10, 251])).dy == -5


# ---------------------------------------------------------------------------
# comparison / diff
# ---------------------------------------------------------------------------

def test_compare_equal_and_missing_report():
    exp = h.ReferenceEncoder().text("hello")
    assert h.compare_reports(exp, [("kbd", r.data) for r in exp]) == ""
    act = [Report(i, 1000000 * i, "kbd", r.data) for i, r in enumerate(exp) if i not in (2, 3)]
    d = h.compare_reports(exp, act)
    assert "expected 10 reports, got 8" in d
    assert "expected keyboard text: 'hello'" in d and "actual   keyboard text: 'hllo'" in d
    assert re.search(r"^-\s+2\s+\.\s+kbd\s+00 00 08", d, re.M), d


def test_compare_extra_report_and_exact_mode():
    exp = [Expected("kbd", K(4, 5)), Expected("kbd", K())]
    act = [("kbd", K(5, 4)), ("kbd", K())]
    assert h.compare_reports(exp, act) == ""            # slot order ignored semantically
    assert h.compare_reports(exp, act, exact=True) != ""
    act2 = [("kbd", K(4, 5)), ("kbd", K(4, 5)), ("kbd", K())]
    d = h.compare_reports(exp, act2)
    assert re.search(r"^\+\s+\.\s+\d+\s+kbd", d, re.M), d
    with pytest.raises(AssertionError) as ei:
        h.assert_reports_equal(exp, act2, msg="context")
    assert str(ei.value).startswith("context")


def test_compare_mouse_length_and_reserved_byte():
    assert h.compare_reports([("mouse", mouse_bytes(0, 1, 1))], [("mouse", mouse_bytes(0, 1, 1, boot=True))]) != ""
    assert h.compare_reports([("kbd", K(4))], [("kbd", bytes([0, 1, 4, 0, 0, 0, 0, 0]))]) != ""


def test_timing_problems():
    exp = [Expected("kbd", K(4), 0), Expected("kbd", K(), 40)]
    ok = [Report(0, 0, "kbd", K(4)), Report(1, 41_000_000, "kbd", K())]
    bad = [Report(0, 0, "kbd", K(4)), Report(1, 20_000_000, "kbd", K())]
    assert h.timing_problems(exp, ok) == []
    assert h.timing_problems(exp, bad) != []
    assert h.gaps_ms(ok) == [41.0]


# ---------------------------------------------------------------------------
# descriptors
# ---------------------------------------------------------------------------

KBD_RD = bytes.fromhex(
    "05 01 09 06 a1 01 05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02 95 01 75 08 81 01"
    " 95 05 75 01 05 08 19 01 29 05 91 02 95 01 75 03 91 01"
    " 95 06 75 08 15 00 26 ff 00 05 07 19 00 2a ff 00 81 00 c0")
MOUSE_RD = bytes.fromhex(
    "05 01 09 02 a1 01 09 01 a1 00 05 09 19 01 29 05 15 00 25 01 95 05 75 01 81 02"
    " 95 01 75 03 81 01 05 01 09 30 09 31 15 81 25 7f 75 08 95 02 81 06"
    " 09 38 15 81 25 7f 75 08 95 01 81 06 05 0c 0a 38 02 15 81 25 7f 75 08 95 01 81 06 c0 c0")
# as read back from the device (sysfs report_descriptor of interface 2)
ABS_RD = bytes.fromhex(
    "05 01 09 02 a1 01 09 01 a1 00 05 09 19 01 29 03 15 00 25 01 95 03 75 01 81 02"
    " 95 01 75 05 81 01 05 01 09 30 09 31 15 00 26 ff 7f 75 10 95 02 81 02"
    " 09 38 15 81 25 7f 95 01 75 08 81 06 05 0c 0a 38 02 15 81 25 7f 95 01 75 08 81 06 c0 c0")


def test_report_descriptor_keyboard_ok():
    rd = h.parse_report_descriptor(KBD_RD)
    assert rd.bits("Input") == 64 and rd.bits("Output") == 8 and rd.report_ids == []
    assert h.verify_keyboard_report_descriptor(rd) == []


def test_report_descriptor_mouse_ok():
    rd = h.parse_report_descriptor(MOUSE_RD)
    assert rd.bits("Input") == 40
    assert h.verify_mouse_report_descriptor(rd) == []


def test_report_descriptor_problems_detected():
    with_id = KBD_RD[:6] + bytes([0x85, 0x01]) + KBD_RD[6:]
    assert any("report IDs" in p for p in h.verify_keyboard_report_descriptor(h.parse_report_descriptor(with_id)))
    small_array = KBD_RD.replace(bytes.fromhex("26 ff 00"), bytes.fromhex("25 65")).replace(
        bytes.fromhex("2a ff 00"), bytes.fromhex("29 65"))
    assert any("F24" in p for p in h.verify_keyboard_report_descriptor(h.parse_report_descriptor(small_array)))
    no_pan = MOUSE_RD[:MOUSE_RD.index(bytes.fromhex("05 0c"))] + bytes.fromhex("c0 c0")
    probs = h.verify_mouse_report_descriptor(h.parse_report_descriptor(no_pan))
    assert any("AC Pan" in p for p in probs) and any("40" in p for p in probs)


def test_report_descriptor_abs_ok():
    rd = h.parse_report_descriptor(ABS_RD)
    assert rd.bits("Input") == 56 and rd.report_ids == []
    assert h.verify_abs_report_descriptor(rd) == []


def test_report_descriptor_abs_problems_detected():
    # TinyUSB's TUD_HID_REPORT_DESC_ABSMOUSE has 5 buttons: Linux then also makes it a joystick
    five = ABS_RD.replace(bytes.fromhex("29 03"), bytes.fromhex("29 05")).replace(
        bytes.fromhex("95 03 75 01 81 02 95 01 75 05"), bytes.fromhex("95 05 75 01 81 02 95 01 75 03"))
    probs = h.verify_abs_report_descriptor(h.parse_report_descriptor(five))
    assert len(probs) == 1 and "joystick" in probs[0]
    narrow = ABS_RD.replace(bytes.fromhex("26 ff 7f"), bytes.fromhex("26 ff 0f"))
    assert any("0..4095" in p for p in h.verify_abs_report_descriptor(h.parse_report_descriptor(narrow)))
    assert h.verify_abs_report_descriptor(h.parse_report_descriptor(MOUSE_RD)) != []


def _config_descriptor(attrs=0xA0, interval=1, protos=(1, 2), abs_itf=True):
    def intf(n, sub, proto, ep, rdlen):
        return (bytes([9, 4, n, 0, 1, 3, sub, proto, 0]) + bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, rdlen, 0])
                + bytes([7, 5, ep, 3, 8, 0, interval]))
    body = intf(0, 1, protos[0], 0x81, len(KBD_RD)) + intf(1, 1, protos[1], 0x82, len(MOUSE_RD))
    if abs_itf:
        body += intf(2, 0, 0, 0x83, len(ABS_RD))
    total = 9 + len(body)
    return bytes([9, 2, total & 0xFF, total >> 8, 3 if abs_itf else 2, 1, 0, attrs, 50]) + body


DEV_DESC = bytes([18, 1, 0x00, 0x02, 0, 0, 0, 64, 0x3a, 0x30, 0x04, 0x40, 0x00, 0x01, 1, 2, 3, 1])


def test_usb_descriptors_verify_ok():
    dev = h.parse_device_descriptor(DEV_DESC)
    cfg = h.parse_config_descriptor(_config_descriptor())
    assert (dev["idVendor"], dev["idProduct"]) == (0x303A, 0x4004)
    assert cfg["remote_wakeup"] and not cfg["self_powered"]
    assert cfg["interfaces"][0]["hid"]["descriptors"][0]["wDescriptorLength"] == len(KBD_RD)
    assert cfg["interfaces"][2]["endpoints"][0]["bEndpointAddress"] == 0x83
    assert h.verify_usb_descriptors(dev, cfg, 2, {0: KBD_RD, 1: MOUSE_RD, 2: ABS_RD}) == []
    old = h.parse_config_descriptor(_config_descriptor(abs_itf=False))
    text = "\n".join(h.verify_usb_descriptors(dev, old, 2))
    assert "bNumInterfaces = 2, expected 3" in text and "interface 2 missing" in text


def test_usb_descriptors_verify_problems():
    dev = h.parse_device_descriptor(DEV_DESC)
    probs = h.verify_usb_descriptors(dev, h.parse_config_descriptor(_config_descriptor(attrs=0xC0, interval=10,
                                                                                         protos=(1, 1))), 3)
    text = "\n".join(probs)
    for needle in ("self-powered", "remote wakeup", "bInterval 10", "3/1/1", "full speed"):
        assert needle in text, text


# ---------------------------------------------------------------------------
# ReportLog
# ---------------------------------------------------------------------------

def test_report_log_order_marks_and_waits():
    log = h.ReportLog()
    m0 = log.mark()
    log.append("kbd", K(4))
    log.event("pause", ep="kbd")
    log.append("mouse", mouse_bytes(1))
    assert [r.seq for r in log.reports(m0)] == [0, 2]
    assert [r.ep for r in log.reports(m0, "mouse")] == ["mouse"]
    assert log.events(kind="pause")[0].seq == 1
    m1 = log.mark()
    assert log.reports(m1) == []

    def producer():
        for i in range(50):
            log.append("kbd", K(4) if i % 2 == 0 else K())
            time.sleep(0.002)
    t = threading.Thread(target=producer)
    t.start()
    got = log.wait_reports(50, m1, "kbd", timeout=5)
    t.join()
    assert len(got) == 50 and [r.seq for r in got] == sorted(r.seq for r in got)
    with pytest.raises(h.HIDTimeout):
        log.wait_reports(51, m1, "kbd", timeout=0.2)
    t0 = time.monotonic()
    assert log.wait_idle(100, timeout=2)
    assert time.monotonic() - t0 >= 0.095
    assert log.kbd_state().empty
    log.clear()
    assert log.reports(0) == [] and log.mark() > m1
    assert log.wait_until(lambda: True, 0.1)
    assert not log.wait_until(lambda: False, 0.1)


def test_report_log_collect_waits_for_idle():
    log = h.ReportLog()
    m = log.mark()

    def producer():
        for _ in range(5):
            time.sleep(0.03)
            log.append("mouse", mouse_bytes(0, 1, 0))
    t = threading.Thread(target=producer)
    t.start()
    reps = log.collect(m, idle_ms=80, timeout=5, min_count=1)
    t.join()
    assert len(reps) == 5


def test_wait_idle_times_out_under_continuous_traffic():
    log = h.ReportLog()
    stop = threading.Event()

    def producer():
        while not stop.is_set():
            log.append("kbd", K())
            time.sleep(0.005)
    t = threading.Thread(target=producer)
    t.start()
    try:
        assert log.wait_idle(100, timeout=0.3) is False
    finally:
        stop.set()
        t.join()


# ---------------------------------------------------------------------------
# sysfs isolation logic (fake sysfs tree)
# ---------------------------------------------------------------------------

def make_sysfs(root, drivers=("usbhid", None, None), hid_child=False, missing_intf=False):
    os.makedirs(os.path.join(root, "drivers", "usbhid"))
    os.makedirs(os.path.join(root, "drivers", "usbfs"))
    dev = os.path.join(root, "3-2")
    os.makedirs(dev)
    open(os.path.join(dev, "idVendor"), "w").write("303a\n")
    open(os.path.join(dev, "idProduct"), "w").write("4004\n")
    other = os.path.join(root, "3-1")
    os.makedirs(other)
    open(os.path.join(other, "idVendor"), "w").write("1a86\n")
    open(os.path.join(other, "idProduct"), "w").write("55d3\n")
    for n, drv in enumerate(drivers):
        if missing_intf and n == 1:
            continue
        d = os.path.join(root, "3-2:1.%d" % n)
        os.makedirs(d)
        open(os.path.join(d, "bInterfaceClass"), "w").write("03\n")
        open(os.path.join(d, "bInterfaceNumber"), "w").write("%02x\n" % n)
        if drv:
            os.symlink(os.path.join(root, "drivers", drv), os.path.join(d, "driver"))
        if hid_child and n == 0:
            os.makedirs(os.path.join(d, "0003:303A:4004.0001"))
    return root


def test_isolation_detects_usbhid(tmp_path):
    root = make_sysfs(str(tmp_path), drivers=("usbhid", None, None))
    assert h.find_sysfs_devices(0x303A, 0x4004, root) == ["3-2"]
    probs = h.isolation_problems("3-2", root)
    assert len(probs) == 1 and "usbhid" in probs[0]


def test_isolation_ok_unbound_and_claimed(tmp_path):
    root = make_sysfs(str(tmp_path / "a"), drivers=(None, None, None))
    assert h.isolation_problems("3-2", root) == []
    assert len(h.isolation_problems("3-2", root, require_claimed=True)) == 3
    root = make_sysfs(str(tmp_path / "b"), drivers=("usbfs", "usbfs", "usbfs"))
    assert h.isolation_problems("3-2", root, require_claimed=True) == []
    # the absolute mouse interface counts like the others
    root = make_sysfs(str(tmp_path / "c"), drivers=("usbfs", "usbfs", "usbhid"))
    assert any("3-2:1.2" in p and "usbhid" in p for p in h.isolation_problems("3-2", root))
    root = make_sysfs(str(tmp_path / "d"), drivers=("usbfs", "usbfs"))
    assert any("interface 2" in p for p in h.isolation_problems("3-2", root))


def test_isolation_other_cases(tmp_path):
    root = make_sysfs(str(tmp_path / "a"), drivers=("usbfs", "usbfs", "usbfs"), hid_child=True)
    assert any("HID children" in p for p in h.isolation_problems("3-2", root))
    root = make_sysfs(str(tmp_path / "b"), drivers=("usbfs", "usbfs", "usbfs"), missing_intf=True)
    assert any("interface 1" in p for p in h.isolation_problems("3-2", root))
    assert h.isolation_problems("9-9", root) != []
    root = make_sysfs(str(tmp_path / "c"), drivers=("usbfs", "hid-generic", "usbfs"))
    assert any("hid-generic" in p for p in h.isolation_problems("3-2", root))


def test_hidhost_check_cli_exit_codes(tmp_path):
    env = dict(os.environ)
    for name, drivers, rc in (("bound", ("usbhid", None, None), 1), ("free", (None, None, None), 0)):
        root = make_sysfs(str(tmp_path / name), drivers=drivers)
        r = subprocess.run([sys.executable, HIDHOST_PY, "check", "--sysfs", root], capture_output=True,
                           text=True, env=env, timeout=30)
        assert r.returncode == rc, r.stdout + r.stderr
    empty = tmp_path / "empty"
    empty.mkdir()
    r = subprocess.run([sys.executable, HIDHOST_PY, "check", "--sysfs", str(empty)], capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 2 and "not present" in r.stdout


def test_hidhost_help():
    r = subprocess.run([sys.executable, HIDHOST_PY, "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    for sub in ("monitor", "descriptors", "leds", "check"):
        assert sub in r.stdout


def test_hidhost_refuses_when_not_running():
    host = h.HIDHost()
    with pytest.raises(h.IsolationError):
        host.assert_isolated(wait_s=0.1)
    with pytest.raises(h.HIDHostError):
        host.set_leds(2)


def test_libusb_transfer_struct_allocation():
    """Loads libusb through pyusb (no device is opened) and checks the async
    transfer plumbing: struct layout and alloc/free."""
    import ctypes
    la = h._LibusbAsync()
    s = h._Slot(la, "kbd")
    t = s.ptr.contents
    T = type(t)
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        # struct libusb_transfer on LP64: handle(8) flags endpoint type pad timeout status length
        # actual_length pad callback user_data buffer num_iso_packets
        assert (T.endpoint.offset, T.timeout.offset, T.status.offset, T.actual_length.offset,
                T.callback.offset, T.buffer.offset) == (9, 12, 16, 24, 32, 48)
    s.fill(0, la.cb_type(lambda tp: None))
    assert (t.endpoint, t.type, t.length, t.timeout) == (0x81, 3, 64, 0)
    s.free()
    assert s.ptr is None
    assert la.handle_events(0.001) == 0


def test_transfer_length_is_endpoint_max_packet():
    """An interrupt IN transfer only completes on a short packet or a full buffer: requesting more than
    wMaxPacketSize would merge consecutive 8-byte keyboard reports when wMaxPacketSize is 8."""
    la = h._LibusbAsync()
    s = h._Slot(la, "kbd")
    cb = la.cb_type(lambda tp: None)
    s.fill(0, cb, 8)
    assert s.ptr.contents.length == 8
    s.fill(0, cb, 16)
    assert s.ptr.contents.length == 16
    s.fill(0, cb, None)
    assert s.ptr.contents.length == 64
    s.free()


def test_endpoint_max_packet_from_descriptors():
    class E:
        def __init__(self, addr, mps):
            self.bEndpointAddress, self.wMaxPacketSize = addr, mps

    class Dev:
        def get_active_configuration(self):
            return [[E(0x81, 8)], [E(0x82, 0x0010), E(0x02, 64)], [E(0x83, 8)]]

    host = h.HIDHost()
    assert host._endpoint_max_packet(Dev()) == {"kbd": 8, "mouse": 16, "abs": 8}
    assert not any(e.kind == "error" for e in host.log.events())

    class Partial:
        def get_active_configuration(self):
            return [[E(0x81, 0x1008)]]  # high-bandwidth bits are masked off
    assert host._endpoint_max_packet(Partial()) == {"kbd": 8, "mouse": 8, "abs": 8}
    assert any(e.kind == "error" for e in host.log.events())


def _callback_rig():
    host = h.HIDHost()
    la = h._LibusbAsync()
    host._la = la
    slot = h._Slot(la, "kbd")
    host._slots = {"kbd": [slot], "mouse": []}
    host._slot_by_key = {slot.key: slot}
    host._running = True
    host._connected = True
    submitted = []
    host._submit_slot = lambda sl: submitted.append(sl) or True
    return host, slot, submitted


def _complete(host, slot, status, data=b""):
    t = slot.ptr.contents
    for i, b in enumerate(data):
        slot.buf[i] = b
    t.actual_length = len(data)
    t.status = status
    slot.in_flight = True
    host._on_transfer(slot.ptr)


def test_callback_records_and_resubmits():
    host, slot, submitted = _callback_rig()
    _complete(host, slot, 0, K(4))
    assert [r.data for r in host.log.reports()] == [K(4)]
    assert submitted == [slot] and slot.in_flight is False
    _complete(host, slot, 2)  # timed out without data: just poll again
    assert len(host.log.reports()) == 1 and len(submitted) == 2
    slot.free()


def test_callback_keeps_data_of_cancelled_transfer_and_respects_pause():
    host, slot, submitted = _callback_rig()
    _complete(host, slot, 3, K(5))  # cancelled, but the packet had already arrived
    assert [r.data for r in host.log.reports()] == [K(5)] and submitted == []
    host._paused["kbd"] = True
    _complete(host, slot, 0, K())
    assert len(host.log.reports()) == 2 and submitted == [], "a paused endpoint must not be polled again"
    slot.free()


def test_callback_marks_device_lost():
    host, slot, submitted = _callback_rig()
    _complete(host, slot, 5)  # LIBUSB_TRANSFER_NO_DEVICE
    assert host._lost.is_set() and not host.connected and submitted == []
    host2, slot2, submitted2 = _callback_rig()
    _complete(host2, slot2, 1)  # error and the device is gone from sysfs (devname None)
    assert host2._lost.is_set() and submitted2 == []
    slot.free()
    slot2.free()
