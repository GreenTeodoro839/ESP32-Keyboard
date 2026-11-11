"""API.md section 3.1: text steps (US layout, control characters, rejection, CapsLock, timing)."""
from __future__ import annotations

import random

import pytest

import hidhost
import support
from support import TIMING_TOL_MS, assert_nothing_emitted, expect_error, expect_http_error, kbd


def type_and_check(dev, hid, text, caps=False, delay_ms=None, **kw):
    expected = hidhost.ReferenceEncoder(caps=caps, key_delay_ms=5).text(text, delay_ms)
    res, reps = support.do(hid, lambda: dev.type(text, delay_ms=delay_ms, **kw), min_count=len(expected),
                           timeout=60 + len(expected) * 0.02)
    hidhost.assert_reports_equal(expected, reps, caps=caps)
    assert hidhost.kbd_text(reps, caps=caps) == hidhost.normalize_text(text)
    return res, reps


def test_printable_ascii_round_trip(dev, hid):
    res, reps = type_and_check(dev, hid, hidhost.PRINTABLE_ASCII)
    assert isinstance(res["job"], int) and isinstance(res["elapsed_ms"], int)
    assert all(len(r.data) == 8 for r in reps)


def test_printable_ascii_shuffled(dev, hid):
    chars = list(hidhost.PRINTABLE_ASCII * 2)
    random.Random(7).shuffle(chars)
    type_and_check(dev, hid, "".join(chars), delay_ms=1)


@pytest.mark.parametrize("text", ["\n", "\t", "\b", "\x1b", "a\r\nb", "a\rb", "a\n\rb", "\r\r", "\r\n\r\n",
                                  "x\r", "tab\there\nnew line\x1besc\bbs"],
                         ids=lambda t: repr(t))
def test_control_characters(dev, hid, text):
    type_and_check(dev, hid, text)


def test_crlf_counts_as_one_enter(dev, hid):
    _, reps = type_and_check(dev, hid, "1\r\n2")
    assert sum(1 for e in hidhost.kbd_events(reps) if e.kind == "down" and e.usage == 40) == 1


@pytest.mark.parametrize("text", ["aaaa", "AAAA", "    ", "1111", "!!!!", "\n\n\n", "abba", "Aa" * 10,
                                  "\b\b\b", "zZzZ"])
def test_repeated_characters(dev, hid, text):
    _, reps = type_and_check(dev, hid, text)
    assert len(reps) == 2 * len(text), "each character must be its own press + release"


def test_text_plain_body(dev, hid, raw):
    text = "plain Body: {\"x\": 1}\n"
    exp = hidhost.ReferenceEncoder().text(text)
    resp, reps = support.do(hid, lambda: raw.post("/api/type", data=text.encode(),
                                                  headers={"Content-Type": "text/plain"}), min_count=len(exp))
    assert resp.status_code == 200, resp.text
    assert support.resp_json(resp)["ok"] is True
    hidhost.assert_reports_equal(exp, reps)


def test_per_request_delay_ms_timing(dev, hid):
    d = 40
    res, reps = type_and_check(dev, hid, "abcdef", delay_ms=d)
    gaps = hidhost.gaps_ms(reps)
    assert min(gaps) >= d - TIMING_TOL_MS, "gaps %s" % [round(g, 1) for g in gaps]
    assert support.median(gaps) <= d + 15, "gaps %s" % [round(g, 1) for g in gaps]
    assert res["elapsed_ms"] >= (len(reps) - 1) * d - 5


def test_delay_ms_zero_is_fast_and_exact(dev, hid):
    res, reps = type_and_check(dev, hid, "the quick brown fox jumps over the lazy dog", delay_ms=0)
    assert res["elapsed_ms"] < 2000


def test_config_key_delay_ms_is_the_default(dev, hid):
    assert dev.set_config(key_delay_ms=25)["reboot_required"] is False
    _, reps = type_and_check(dev, hid, "abcd")
    gaps = hidhost.gaps_ms(reps)
    assert min(gaps) >= 25 - TIMING_TOL_MS, gaps
    # an explicit delay_ms still wins
    _, reps = type_and_check(dev, hid, "abcd", delay_ms=0)
    assert support.median(hidhost.gaps_ms(reps)) < 15


UNSUPPORTED = ["héllo", "abc中文", "ok\u0000x", "a\x7f", "\x01", "tab\x0bvt", "ff\x0c",
               "emoji \U0001F600", " ", "fine until here ", "​"]


@pytest.mark.parametrize("text", UNSUPPORTED, ids=lambda t: repr(t))
def test_unsupported_characters_rejected(dev, hid, text):
    _, off, _ = hidhost.unsupported_chars(text)[0]
    m = hid.mark()
    expect_error(lambda: dev.type(text), 400, "bad_step", index=0, offset=off)
    assert_nothing_emitted(hid, m)


def test_unsupported_in_later_seq_step(dev, hid):
    m = hid.mark()
    expect_error(lambda: dev.seq([{"text": "fine"}, {"key": "a"}, {"text": "x€"}]), 400, "bad_step",
                 index=2, offset=1)
    assert_nothing_emitted(hid, m)


def test_unsupported_text_plain(dev, hid, raw):
    m = hid.mark()
    r = raw.post("/api/type", data="naïve".encode("utf-8"), headers={"Content-Type": "text/plain; charset=utf-8"})
    expect_http_error(r, 400, "bad_step", index=0, offset=2)
    assert_nothing_emitted(hid, m)


def test_unsupported_raw_utf8_json(dev, hid, raw):
    """Non-ASCII sent as raw UTF-8 inside JSON (not \\u escapes): offset is still in bytes."""
    m = hid.mark()
    body = '{"text":"abééc"}'.encode("utf-8")
    r = raw.post("/api/type", data=body, headers={"Content-Type": "application/json"})
    expect_http_error(r, 400, "bad_step", index=0, offset=2)
    assert_nothing_emitted(hid, m)


def test_capslock_compensation(dev, hid):
    hid.set_leds(hidhost.LED_CAPS)
    support.wait_status(dev, lambda s: s["usb"]["leds"]["caps"] is True, timeout=2)
    text = "Hello World ABC xyz 123!? MiXeD"
    type_and_check(dev, hid, text, caps=True)


def test_numlock_scrolllock_do_not_change_text(dev, hid):
    hid.set_leds(hidhost.LED_NUM | hidhost.LED_SCROLL)
    support.wait_status(dev, lambda s: s["usb"]["leds"]["num"] and s["usb"]["leds"]["scroll"]
                        and not s["usb"]["leds"]["caps"], timeout=2)
    type_and_check(dev, hid, "Abc 123")


def test_capslock_off_again_restores_normal_shift(dev, hid):
    hid.set_leds(hidhost.LED_CAPS)
    support.wait_status(dev, lambda s: s["usb"]["leds"]["caps"] is True, timeout=2)
    hid.set_leds(0)
    support.wait_status(dev, lambda s: s["usb"]["leds"]["caps"] is False, timeout=2)
    type_and_check(dev, hid, "Abc")


def _random_text(n, seed):
    rnd = random.Random(seed)
    alphabet = hidhost.PRINTABLE_ASCII * 3 + "\n\t"
    return "".join(rnd.choice(alphabet) for _ in range(n))


def test_long_text_exact(dev, hid):
    type_and_check(dev, hid, _random_text(3000, 1234), delay_ms=0)


@pytest.mark.slow
def test_very_long_text_exact(dev, hid):
    type_and_check(dev, hid, _random_text(20000, 99), delay_ms=0)


@pytest.mark.slow
def test_text_body_near_limit(dev, hid, raw):
    """One request whose JSON body is just under the 65536-byte limit."""
    import json
    text = _random_text(70000, 5).replace("\\", "/").replace('"', "'")
    body = b""
    lo, hi = 0, len(text)
    while lo < hi:  # longest prefix whose body fits in 65536 - 16 bytes
        mid = (lo + hi + 1) // 2
        if len(json.dumps({"text": text[:mid], "delay_ms": 0}).encode()) <= 65536 - 16:
            lo = mid
        else:
            hi = mid - 1
    text = text[:lo]
    body = json.dumps({"text": text, "delay_ms": 0}).encode()
    exp = hidhost.ReferenceEncoder().text(text, 0)
    resp, reps = support.do(hid, lambda: raw.post("/api/type", data=body, headers={"Content-Type": "application/json"},
                                                  timeout=600), min_count=len(exp), timeout=600)
    assert resp.status_code == 200, resp.text
    hidhost.assert_reports_equal(exp, reps)


def test_empty_text(dev, hid):
    """API.md does not say whether an empty text is valid; it must not emit anything."""
    m = hid.mark()
    try:
        dev.type("")
    except Exception as e:
        assert getattr(e, "status", None) == 400, e
    assert_nothing_emitted(hid, m)


def test_text_with_held_shift_is_not_corrupted_by_release(dev, hid):
    """Typing while nothing is held must leave nothing held; final report all zero."""
    _, reps = type_and_check(dev, hid, "End.")
    assert kbd(reps)[-1].data == bytes(8)
