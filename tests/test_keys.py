"""API.md sections 3 and 8: key names, aliases, numeric forms, combos, repeat, hold, down/up, 6-key limit."""
from __future__ import annotations

import warnings

import pytest

import hidhost
import support
from hidhost import kbd_bytes
from support import RANGE_CODES, TIMING_TOL_MS, assert_nothing_emitted, expect_error


def tap_steps(names):
    return [{"key": n, "delay_ms": 1} for n in names]


def check_seq(dev, hid, steps, **enc_kw):
    exp = hidhost.ReferenceEncoder(**enc_kw).steps(steps)
    _, reps = support.do(hid, lambda: dev.seq(steps), min_count=len(exp))
    hidhost.assert_reports_equal(exp, reps)
    return reps


def test_every_documented_name_and_alias(dev, hid):
    names = sorted(hidhost.KEY_NAMES)
    check_seq(dev, hid, tap_steps(names))


def test_names_are_case_insensitive(dev, hid):
    names = sorted(hidhost.KEY_NAMES)
    variants = [n.upper() if i % 2 == 0 else n.title() for i, n in enumerate(names)]
    check_seq(dev, hid, tap_steps(variants))


def test_each_key_name_maps_to_its_usage_individually(dev, hid):
    """One request per primary name, so a failure names the exact key."""
    bad = []
    for primary, _aliases, usage in hidhost.KEY_TABLE:
        _, reps = support.do(hid, lambda: dev.key(primary, delay_ms=0), idle_ms=30)
        exp = [hidhost.state_to_kbd([usage]), bytes(8)]
        if [r.data for r in reps] != exp:
            bad.append("%s: expected %s got %s" % (primary, [e.hex() for e in exp], [r.data.hex() for r in reps]))
    assert not bad, "\n".join(bad)


def test_keys_endpoint_matches_spec(dev):
    res = dev.keys()
    keys = {k.lower(): v for k, v in res["keys"].items()}
    missing = {n: u for n, u in hidhost.KEY_NAMES.items() if n not in keys}
    wrong = {n: (keys[n], u) for n, u in hidhost.KEY_NAMES.items() if n in keys and keys[n] != u}
    assert not missing, "documented names missing from /api/keys: %s" % missing
    assert not wrong, "name -> (device, API.md): %s" % wrong
    assert res["buttons"] == ["left", "right", "middle", "back", "forward"]
    extra = sorted(set(keys) - set(hidhost.KEY_NAMES))
    if extra:
        warnings.warn("/api/keys lists names not documented in API.md: %s" % extra)
    assert "+" not in keys


def test_undocumented_names_from_keys_endpoint_work(dev, hid):
    keys = {k.lower(): v for k, v in dev.keys()["keys"].items()}
    extra = {n: u for n, u in keys.items() if n not in hidhost.KEY_NAMES
             and u != hidhost.USAGE_POWER and u != hidhost.USAGE_PRINTSCREEN and "+" not in n}
    if not extra:
        pytest.skip("device lists no extra key names")
    for n, u in sorted(extra.items()):
        _, reps = support.do(hid, lambda: dev.key(n, delay_ms=0), idle_ms=30)
        assert [r.data for r in reps] == [hidhost.state_to_kbd([u]), bytes(8)], n


@pytest.mark.parametrize("key,usage", [(40, 40), ("0x28", 40), (4, 4), ("0x04", 4), ("0x2c", 44),
                                       (255, 255), (115, 115), (224, 224), ("0xe7", 231), (0x39, 0x39)],
                         ids=lambda v: repr(v))
def test_numeric_forms(dev, hid, key, usage):
    _, reps = support.do(hid, lambda: dev.key(key))
    assert [r.data for r in reps] == [hidhost.state_to_kbd([usage]), bytes(8)], \
        "numeric modifiers (224-231) are expected in the modifier bitmap"


@pytest.mark.parametrize("key", [256, -1, "0x100", "0xzz", "0x", 1000], ids=lambda v: repr(v))
def test_numeric_out_of_range(dev, hid, key):
    m = hid.mark()
    expect_error(lambda: dev.key(key), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("combo", ["ctrl+shift+esc", "ctrl+alt+del", "shift+=", "gui+r", "ctrl+a+b",
                                   "rctrl+rshift+ralt+rgui+f12", "alt+tab", "shift+ctrl+left"])
def test_combo_press_in_order_release_in_reverse(dev, hid, combo):
    exp = hidhost.ReferenceEncoder().key(combo)
    _, reps = support.do(hid, lambda: dev.key(combo, delay_ms=2))
    hidhost.assert_reports_equal(exp, reps)
    ev = [e for e in hidhost.kbd_events(reps)]
    downs = [e.usage for e in ev if e.kind == "down"]
    ups = [e.usage for e in ev if e.kind == "up"]
    assert downs == hidhost.parse_key_spec(combo)
    assert ups == list(reversed(downs))


def test_shift_equal_types_plus(dev, hid):
    _, reps = support.do(hid, lambda: dev.key("shift+="))
    assert hidhost.kbd_text(reps) == "+"


@pytest.mark.parametrize("key,repeat", [("a", 5), ("ctrl+c", 3), ("enter", 2)])
def test_repeat(dev, hid, key, repeat):
    exp = hidhost.ReferenceEncoder().key(key, repeat=repeat, delay_ms=1)
    _, reps = support.do(hid, lambda: dev.key(key, repeat=repeat, delay_ms=1))
    hidhost.assert_reports_equal(exp, reps)


def test_repeat_1000(dev, hid):
    exp = hidhost.ReferenceEncoder().key("x", repeat=1000, delay_ms=0)
    _, reps = support.do(hid, lambda: dev.key("x", repeat=1000, delay_ms=0), min_count=2000, timeout=120)
    hidhost.assert_reports_equal(exp, reps)
    assert hidhost.kbd_text(reps) == "x" * 1000


@pytest.mark.parametrize("repeat", [0, 1001, -1])
def test_repeat_out_of_range(dev, hid, repeat):
    m = hid.mark()
    expect_error(lambda: dev.key("a", repeat=repeat), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("key,hold", [("a", 250), ("shift+a", 150)])
def test_hold_ms(dev, hid, key, hold):
    _, reps = support.do(hid, lambda: dev.key(key, hold_ms=hold, delay_ms=1))
    n = len(hidhost.parse_key_spec(key))
    gap = (reps[n].t_ns - reps[n - 1].t_ns) / 1e6
    assert hold - TIMING_TOL_MS <= gap <= hold + 60, "press->release gap %.1f ms, hold_ms %d" % (gap, hold)


def test_down_up_single(dev, hid):
    _, reps = support.do(hid, lambda: dev.key_down("shift"))
    assert [r.data for r in reps] == [kbd_bytes(0x02)]
    assert dev.status()["hid"]["keys"] == [225]
    _, reps = support.do(hid, lambda: dev.key_up("shift"))
    assert [r.data for r in reps] == [bytes(8)]
    assert dev.status()["hid"]["keys"] == []


def test_down_up_combo(dev, hid):
    _, reps = support.do(hid, lambda: dev.key_down("ctrl+alt+x"))
    hidhost.assert_reports_equal(hidhost.ReferenceEncoder().key("ctrl+alt+x", "down"), reps)
    assert sorted(dev.status()["hid"]["keys"]) == [27, 224, 226]
    _, reps = support.do(hid, lambda: dev.key_up("ctrl+alt+x"))
    assert len(reps) == 3 and reps[-1].data == bytes(8)
    ups = [e.usage for e in hidhost.kbd_events(reps, initial=kbd_bytes(0x05, [27])) if e.kind == "up"]
    assert sorted(ups) == [27, 224, 226]  # release order of "up" is not specified by API.md


def test_up_of_key_not_held_is_noop(dev, hid):
    m = hid.mark()
    dev.key_up("b")
    assert_nothing_emitted(hid, m)


def test_down_of_already_held_key(dev, hid):
    dev.key_down("a")
    m = hid.mark()
    dev.key_down("a")
    hid.wait_idle(200)
    reps = hid.kbd_reports(m)
    assert len(reps) <= 1 and all(r.data == kbd_bytes(0, [4]) for r in reps), [r.data.hex() for r in reps]
    assert dev.status()["hid"]["keys"] == [4]


def test_six_keys_in_one_combo(dev, hid):
    _, reps = support.do(hid, lambda: dev.key("a+b+c+d+e+f"))
    assert hidhost.parse_kbd(reps[5].data).pressed == frozenset([4, 5, 6, 7, 8, 9])


def test_modifiers_do_not_count_towards_six(dev, hid):
    exp = hidhost.ReferenceEncoder().key("ctrl+shift+alt+gui+a+b+c+d+e+f")
    _, reps = support.do(hid, lambda: dev.key("ctrl+shift+alt+gui+a+b+c+d+e+f", delay_ms=1))
    hidhost.assert_reports_equal(exp, reps)


def test_seventh_key_in_combo_rejected(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.key("a+b+c+d+e+f+g"), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


def test_seventh_key_within_seq_rejected_atomically(dev, hid):
    steps = [{"key": c, "action": "down"} for c in "abcdef"] + [{"key": "g", "action": "down"}]
    m = hid.mark()
    expect_error(lambda: dev.seq(steps), 400, "bad_step", index=6)
    assert_nothing_emitted(hid, m)


def test_seventh_key_across_requests_rejected(dev, hid):
    for c in "abcdef":
        dev.key_down(c, ttl_ms=30000)
    hid.wait_idle(100)
    m = hid.mark()
    expect_error(lambda: dev.key_down("g", ttl_ms=30000), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)
    assert sorted(dev.status()["hid"]["keys"]) == [4, 5, 6, 7, 8, 9]
    # a modifier is still allowed
    _, reps = support.do(hid, lambda: dev.key_down("shift", ttl_ms=30000))
    assert hidhost.parse_kbd(reps[-1].data).pressed == frozenset([4, 5, 6, 7, 8, 9, 225])


@pytest.mark.parametrize("key", ["nosuchkey", "ctrl+nosuch", "", "+", "ctrl+", "plus", "a b", "ctrl++a",
                                 "f25", "kp_10", "lctl"], ids=lambda v: repr(v))
def test_unknown_key_rejected(dev, hid, key):
    m = hid.mark()
    expect_error(lambda: dev.key(key), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("value", [True, None, [4], {"k": 1}, 4.5], ids=lambda v: repr(v))
def test_key_wrong_type_is_bad_step(dev, hid, value):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/key", {"key": value}), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("action", ["press", "DOWNN", "", 1])
def test_bad_action(dev, hid, action):
    m = hid.mark()
    expect_error(lambda: dev.post("/api/key", {"key": "a", "action": action}), 400, ("bad_step",) + RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


def test_key_endpoint_rejects_non_key_step(dev, hid):
    """/api/key takes "a key step"; a mouse step there is not a key step."""
    m = hid.mark()
    expect_error(lambda: dev.post("/api/key", {"move": [1, 1]}), 400, ("bad_step", "bad_request"))
    assert_nothing_emitted(hid, m)
