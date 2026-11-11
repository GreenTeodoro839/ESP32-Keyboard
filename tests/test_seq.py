"""API.md sections 1, 3 and 4: /api/seq ordering, delays, release step, atomic validation, limits."""
from __future__ import annotations

import json
import random

import pytest

import hidhost
import support
from hidhost import kbd_bytes, mouse_bytes
from support import RANGE_CODES, TIMING_TOL_MS, assert_nothing_emitted, expect_error, expect_http_error

SAFE_KEYS = [k for k in hidhost.KEY_NAMES
             if hidhost.KEY_NAMES[k] not in (hidhost.USAGE_PRINTSCREEN, hidhost.USAGE_POWER, hidhost.USAGE_CAPSLOCK)]


def run_exact(dev, hid, steps, exact=False):
    exp = hidhost.ReferenceEncoder().steps(steps)
    res, reps = support.do(hid, lambda: dev.seq(steps), min_count=len(exp), timeout=120)
    hidhost.assert_reports_equal(exp, reps, exact=exact)
    return res, reps


def test_order_across_keyboard_and_mouse(dev, hid):
    steps = [{"key": "a"}, {"move": [5, 0]}, {"key": "b"}, {"click": "left"}, {"text": "c"}, {"scroll": 1},
             {"button": "right", "action": "down"}, {"key": "shift+d"}, {"button": "right", "action": "up"}]
    _, reps = run_exact(dev, hid, steps)
    assert [r.ep for r in reps] == ["kbd", "kbd", "mouse", "kbd", "kbd", "mouse", "mouse", "kbd", "kbd", "mouse",
                                    "mouse", "kbd", "kbd", "kbd", "kbd", "mouse"]


def test_raw_array_body(dev, hid):
    steps = [{"key": "x"}, {"move": [1, 2]}]
    exp = hidhost.ReferenceEncoder().steps(steps)
    _, reps = support.do(hid, lambda: dev.post("/api/seq", steps))
    hidhost.assert_reports_equal(exp, reps)


def test_delay_step_timing(dev, hid):
    _, reps = run_exact(dev, hid, [{"key": "a", "delay_ms": 1}, {"delay": 300}, {"key": "b", "delay_ms": 1}])
    gap = (reps[2].t_ns - reps[1].t_ns) / 1e6
    assert 300 - TIMING_TOL_MS <= gap <= 300 + 60, gap


def test_delay_zero_and_elapsed(dev, hid):
    res = dev.seq([{"delay": 0}, {"delay": 250}])
    assert 245 <= res["elapsed_ms"] <= 600


@pytest.mark.parametrize("step", [{"delay": 60001}, {"delay": -1}], ids=str)
def test_delay_out_of_range(dev, hid, step):
    m = hid.mark()
    expect_error(lambda: dev.seq([{"key": "a"}, step]), 400, RANGE_CODES, index=1)
    assert_nothing_emitted(hid, m)


def test_release_step(dev, hid):
    steps = [{"key": "shift", "action": "down"}, {"key": "a", "action": "down"},
             {"button": "left", "action": "down"}, {"move": [3, 3]}, {"release": True}]
    _, reps = support.do(hid, lambda: dev.seq(steps))
    exp_before = hidhost.ReferenceEncoder().steps(steps[:-1])
    hidhost.assert_reports_equal(exp_before, reps[:len(exp_before)])
    tail = reps[len(exp_before):]
    assert support.kbd(tail) and support.kbd(tail)[-1].data == bytes(8)
    assert support.mouse(tail) and support.mouse(tail)[-1].data == mouse_bytes(0)
    assert hid.kbd_state().empty and hid.mouse_buttons() == 0
    st = dev.status()["hid"]
    assert st["keys"] == [] and st["buttons"] == []


def test_release_step_is_queued_not_immediate(dev, hid):
    """{"release":true} inside a seq runs in order (unlike /api/release)."""
    steps = [{"key": "z", "action": "down"}, {"delay": 300}, {"release": True}, {"key": "y"}]
    _, reps = support.do(hid, lambda: dev.seq(steps))
    datas = [r.data for r in reps]
    assert datas[0] == kbd_bytes(0, [0x1d])
    assert datas[1] == bytes(8)
    assert (reps[1].t_ns - reps[0].t_ns) / 1e6 >= 300 - TIMING_TOL_MS
    # API.md §10: the release step also sends an all-released mouse report
    assert datas[2:] == [mouse_bytes(0), kbd_bytes(0, [0x1c]), bytes(8)]


ATOMIC = [
    ([{"key": "a"}, {"key": "nosuch"}, {"key": "b"}], 1),
    ([{"text": "ok"}, {"move": [99999, 0]}], 1),
    ([{"click": "left"}, {"button": "left"}], 1),
    ([{"delay": 10}, {}], 1),
    ([{"key": "a", "text": "b"}], 0),
    ([{"key": "a"}, {"key": "b"}, {"text": "fine"}, {"scroll": "x"}], 3),
    ([{"key": "shift", "action": "down"}, {"click": "nope"}], 1),
]


@pytest.mark.parametrize("steps,index", ATOMIC, ids=[str(i) for i in range(len(ATOMIC))])
def test_atomic_validation(dev, hid, steps, index):
    m = hid.mark()
    expect_error(lambda: dev.seq(steps), 400, ("bad_step",) + RANGE_CODES, index=index)
    assert_nothing_emitted(hid, m)
    assert dev.status()["hid"]["keys"] == []


def test_unknown_fields_ignored(dev, hid):
    run_exact(dev, hid, [{"key": "a", "comment": "x", "foo": [1, 2], "id": 5}, {"move": [1, 1], "note": None}])


def test_unknown_top_level_fields_ignored(dev, hid):
    exp = hidhost.ReferenceEncoder().steps([{"key": "q"}])
    _, reps = support.do(hid, lambda: dev.post("/api/seq", {"steps": [{"key": "q"}], "extra": {"a": 1}}))
    hidhost.assert_reports_equal(exp, reps)


@pytest.mark.parametrize("step", [{}, {"foo": 1}, {"key": "a", "move": [1, 1]}, {"text": "a", "delay": 1},
                                  {"release": True, "key": "a"}], ids=str)
def test_verb_count_must_be_one(dev, hid, step):
    m = hid.mark()
    expect_error(lambda: dev.seq([step]), 400, "bad_step", index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("step", [{"key": True}, {"text": 5}, {"delay": "100"}, {"move": "1,2"}, {"click": 5},
                                  {"scroll": "3"}, {"button": ["left"], "action": "down"}, {"text": None},
                                  {"key": "a", "repeat": "2"}, {"click": "left", "count": 2.5}], ids=str)
def test_field_type_errors_are_bad_step(dev, hid, step):
    m = hid.mark()
    expect_error(lambda: dev.seq([{"key": "a"}, step]), 400, "bad_step", index=1)
    assert_nothing_emitted(hid, m)


VALUE_RANGES = [{"key": "a", "repeat": 0}, {"key": "a", "repeat": 1001}, {"click": "left", "count": 0},
                {"click": "left", "count": 101}, {"move": [1, 1], "step": 0}, {"move": [1, 1], "step": 128},
                {"delay": 60001}, {"key": "a", "action": "down", "ttl_ms": 0},
                {"key": "a", "action": "down", "ttl_ms": 600001}, {"button": "left", "action": "down", "ttl_ms": 0},
                {"move": [32768, 0]}, {"scroll": -32768}, {"key": 256}]


@pytest.mark.parametrize("step", VALUE_RANGES, ids=str)
def test_value_ranges(dev, hid, step):
    m = hid.mark()
    expect_error(lambda: dev.seq([step]), 400, RANGE_CODES, index=0)
    assert_nothing_emitted(hid, m)


@pytest.mark.parametrize("step", [{"delay": 60000}, {"key": "a", "action": "down", "ttl_ms": 600000},
                                  {"key": "a", "action": "down", "ttl_ms": 1}], ids=str)
def test_value_range_upper_bounds_accepted_async(dev, hid, step):
    dev.seq([step], wait=False)
    dev.release()


def test_512_steps_accepted(dev, hid):
    res = dev.seq([{"delay": 0}] * 512)
    assert res["ok"]


def test_513_steps_too_large(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.seq([{"key": "a"}] * 513), 413, "too_large")
    assert_nothing_emitted(hid, m)


def test_512_key_steps_exact(dev, hid):
    rnd = random.Random(3)
    steps = [{"key": rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789"), "delay_ms": 0} for _ in range(512)]
    run_exact(dev, hid, steps)


def _padded_body(total):
    base = {"steps": [{"key": "a"}], "pad": ""}
    n = total - len(json.dumps(base, separators=(",", ":")).encode())
    base["pad"] = "x" * n
    b = json.dumps(base, separators=(",", ":")).encode()
    assert len(b) == total
    return b


def test_body_just_under_limit_accepted(dev, hid, raw):
    body = _padded_body(65000)
    resp, reps = support.do(hid, lambda: raw.post("/api/seq", data=body, headers={"Content-Type": "application/json"}))
    assert resp.status_code == 200, resp.text[:300]
    assert [r.data for r in reps] == [kbd_bytes(0, [4]), bytes(8)]


def test_body_at_limit_accepted(dev, hid, raw):
    body = _padded_body(65536)
    resp = raw.post("/api/seq", data=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200, resp.text[:300]


def test_body_over_limit_too_large(dev, hid, raw):
    body = _padded_body(65537)
    m = hid.mark()
    resp = raw.post("/api/seq", data=body, headers={"Content-Type": "application/json"})
    expect_http_error(resp, 413, "too_large")
    assert_nothing_emitted(hid, m)


def test_body_far_over_limit_too_large(dev, hid, raw):
    body = _padded_body(200000)
    m = hid.mark()
    resp = raw.post("/api/seq", data=body, headers={"Content-Type": "application/json"})
    expect_http_error(resp, 413, "too_large")
    assert_nothing_emitted(hid, m)
    assert dev.status()["ok"]


@pytest.mark.parametrize("body", [{"steps": "abc"}, {"steps": {"key": "a"}}, {"steps": None}, {"nosteps": []}, 5,
                                  "str"], ids=str)
def test_steps_container_errors(dev, hid, raw, body):
    m = hid.mark()
    resp = raw.post("/api/seq", data=json.dumps(body), headers={"Content-Type": "application/json"})
    expect_http_error(resp, 400, ("bad_request", "bad_step"))
    assert_nothing_emitted(hid, m)


def test_empty_steps(dev, hid):
    """API.md does not say whether an empty job is valid; it must not emit anything."""
    m = hid.mark()
    try:
        dev.seq([])
    except Exception as e:
        assert getattr(e, "status", None) == 400, e
    assert_nothing_emitted(hid, m)


def test_randomized_mixed_sequence_exact(dev, hid):
    """Seeded random safe sequence; moves stay within +-127 so every move is one report."""
    rnd = random.Random(2024)
    steps = []
    for _ in range(120):
        kind = rnd.choice(["text", "key", "combo", "move", "click", "scroll", "delay"])
        if kind == "text":
            steps.append({"text": "".join(rnd.choice(hidhost.PRINTABLE_ASCII) for _ in range(rnd.randint(1, 6))),
                          "delay_ms": rnd.choice([0, 1, 2])})
        elif kind == "key":
            steps.append({"key": rnd.choice(SAFE_KEYS), "delay_ms": 0})
        elif kind == "combo":
            mods = rnd.sample(["ctrl", "shift", "gui", "rctrl", "rshift"], rnd.randint(1, 2))
            steps.append({"key": "+".join(mods + [rnd.choice("abcdefghijklmnopqrstuvwxyz")]), "delay_ms": 0})
        elif kind == "move":
            steps.append({"move": [rnd.randint(-127, 127), rnd.randint(-127, 127)]})
        elif kind == "click":
            steps.append({"click": rnd.choice(hidhost.BUTTON_NAMES), "hold_ms": rnd.choice([0, 5]),
                          "interval_ms": 0, "count": rnd.randint(1, 2)})
        elif kind == "scroll":
            steps.append({"scroll": [rnd.randint(-127, 127), rnd.randint(-127, 127)]})
        else:
            steps.append({"delay": rnd.randint(0, 20)})
    for s in steps:
        support.check_forbidden(s)
    run_exact(dev, hid, steps)
