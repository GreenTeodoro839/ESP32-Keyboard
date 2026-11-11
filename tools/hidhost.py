#!/usr/bin/env python3
"""hidhost.py - a fake USB host for the ESPKB keyboard/mouse (pyusb / libusb-1.0).

The ESPKB HID port is plugged into a development machine that runs a live
desktop session.  A udev rule unbinds the kernel ``usbhid`` driver from
303a:4004, so the device's reports reach nothing unless a program reads them
over libusb.  This module is that program: it claims both HID interfaces,
keeps the interrupt IN endpoints polled, and records every report with a
monotonic timestamp so tests can check, report by report, what the device
emitted.

Safety rule (central): before any HID output may be produced, both HID
interfaces must be bound to nothing or to ``usbfs`` (i.e. claimed by us),
never to ``usbhid``.  :class:`HIDHost` refuses to start otherwise, detaches a
kernel driver via libusb if one is somehow active, and NEVER re-attaches the
kernel driver.

Contents
  * key tables (API.md section 8) and the US-layout character map
  * report parsing / formatting helpers
  * decoders: keyboard reports -> key events -> typed text; mouse summaries
  * ReferenceEncoder: text / steps -> the exact expected report sequence
  * compare_reports / assert_reports_equal: readable report-by-report diff
  * USB / HID report descriptor parsers and API.md section 9 verification
  * ReportLog: thread-safe ordered log with wait primitives
  * sysfs isolation checks
  * HIDHost: the libusb host (async transfers, pause/resume, re-enumeration)
  * CLI: monitor | descriptors | leds | check

Run ``hidhost.py --help`` for the CLI.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import difflib
import glob
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Constants (API.md section 9)
# ---------------------------------------------------------------------------

DEFAULT_VID = 0x303A
DEFAULT_PID = 0x4004
KBD_INTF = 0
MOUSE_INTF = 1
KBD_EP = 0x81
MOUSE_EP = 0x82
EP_ADDR = {"kbd": KBD_EP, "mouse": MOUSE_EP}
EP_INTF = {"kbd": KBD_INTF, "mouse": MOUSE_INTF}
INTF_EP = {KBD_INTF: "kbd", MOUSE_INTF: "mouse"}
KBD_REPORT_LEN = 8
MOUSE_REPORT_LEN = 5
MOUSE_BOOT_REPORT_LEN = 3

LED_NUM = 0x01
LED_CAPS = 0x02
LED_SCROLL = 0x04
LED_BITS = {"num": LED_NUM, "caps": LED_CAPS, "scroll": LED_SCROLL}

BUTTON_BITS = {"left": 0x01, "right": 0x02, "middle": 0x04, "back": 0x08, "forward": 0x10}
BUTTON_NAMES = ["left", "right", "middle", "back", "forward"]

MOD_FIRST = 0xE0  # usage of left ctrl; bitmap bit n = usage 0xE0 + n
MOD_SHIFT_MASK = 0x22  # lshift | rshift
MOD_NONSHIFT_MASK = 0xDD  # ctrl/alt/gui, left and right
USAGE_ERROR_ROLLOVER = 0x01
USAGE_POWER = 0x66  # must never be emitted by randomized tests
USAGE_PRINTSCREEN = 0x46
USAGE_CAPSLOCK = 0x39

# HID class requests
HID_GET_REPORT = 0x01
HID_GET_IDLE = 0x02
HID_GET_PROTOCOL = 0x03
HID_SET_REPORT = 0x09
HID_SET_IDLE = 0x0A
HID_SET_PROTOCOL = 0x0B
REPORT_TYPE_INPUT = 1
REPORT_TYPE_OUTPUT = 2
REPORT_TYPE_FEATURE = 3
PROTOCOL_BOOT = 0
PROTOCOL_REPORT = 1

SYSFS_USB = "/sys/bus/usb/devices"


class HIDHostError(RuntimeError):
    """Generic harness error (device missing, transfer failure, ...)."""


class IsolationError(HIDHostError):
    """HID isolation from the local desktop could not be established."""


class DeviceNotFound(HIDHostError):
    """The USB device is not present on the bus."""


class HIDTimeout(HIDHostError):
    """A waiting primitive timed out."""


class DecodeError(ValueError):
    """Reports could not be decoded to plain text."""

    def __init__(self, message: str, index: int = -1):
        super().__init__(message)
        self.index = index


class EncodeError(ValueError):
    """A step is invalid per API.md (mirrors the device's validation)."""

    def __init__(self, message: str, index: int = 0, offset: Optional[int] = None,
                 code: str = "bad_step"):
        super().__init__(message)
        self.index = index
        self.offset = offset
        self.code = code


# ---------------------------------------------------------------------------
# Key tables (API.md section 8)
# ---------------------------------------------------------------------------

def _build_key_table() -> List[Tuple[str, Tuple[str, ...], int]]:
    """Return [(primary_name, aliases, usage)] exactly as listed in API.md section 8."""
    t: List[Tuple[str, Tuple[str, ...], int]] = []
    for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
        t.append((c, (), 4 + i))
    for i, c in enumerate("1234567890"):
        t.append((c, (), 30 + i))
    t += [
        ("enter", ("return",), 40), ("esc", ("escape",), 41), ("backspace", ("bksp",), 42),
        ("tab", (), 43), ("space", ("spacebar",), 44),
        ("minus", ("-",), 45), ("equal", ("=",), 46), ("lbracket", ("[",), 47),
        ("rbracket", ("]",), 48), ("backslash", ("\\",), 49),
        ("semicolon", (";",), 51), ("quote", ("'",), 52), ("grave", ("`",), 53),
        ("comma", (",",), 54), ("period", (".", "dot"), 55), ("slash", ("/",), 56),
        ("capslock", (), 57),
    ]
    for i in range(12):
        t.append(("f%d" % (i + 1), (), 58 + i))
    for i in range(12):
        t.append(("f%d" % (i + 13), (), 104 + i))
    t += [
        ("printscreen", ("prtsc",), 70), ("scrolllock", (), 71), ("pause", (), 72),
        ("insert", ("ins",), 73), ("home", (), 74), ("pageup", ("pgup",), 75),
        ("delete", ("del",), 76), ("end", (), 77), ("pagedown", ("pgdn",), 78),
        ("right", (), 79), ("left", (), 80), ("down", (), 81), ("up", (), 82),
        ("numlock", (), 83), ("kp_divide", (), 84), ("kp_multiply", (), 85),
        ("kp_minus", (), 86), ("kp_plus", (), 87), ("kp_enter", (), 88),
    ]
    for i in range(9):
        t.append(("kp_%d" % (i + 1), (), 89 + i))
    t += [
        ("kp_0", (), 98), ("kp_dot", (), 99),
        ("menu", ("app",), 101),
        ("ctrl", ("lctrl", "control"), 224), ("shift", ("lshift",), 225),
        ("alt", ("lalt", "option"), 226), ("gui", ("lgui", "win", "meta", "super", "cmd"), 227),
        ("rctrl", (), 228), ("rshift", (), 229), ("ralt", ("altgr",), 230), ("rgui", ("rwin",), 231),
    ]
    return t


KEY_TABLE = _build_key_table()
#: every documented name and alias (lower case) -> HID usage
KEY_NAMES: Dict[str, int] = {}
#: HID usage -> primary documented name
USAGE_NAMES: Dict[int, str] = {}
for _p, _aliases, _u in KEY_TABLE:
    KEY_NAMES[_p] = _u
    USAGE_NAMES.setdefault(_u, _p)
    for _a in _aliases:
        KEY_NAMES[_a] = _u
MODIFIER_NAMES = ["ctrl", "shift", "alt", "gui", "rctrl", "rshift", "ralt", "rgui"]


def is_modifier(usage: int) -> bool:
    return 0xE0 <= usage <= 0xE7


def usage_name(usage: int) -> str:
    return USAGE_NAMES.get(usage, "0x%02x" % usage)


def parse_key_spec(spec: Union[int, str]) -> List[int]:
    """Translate a ``key`` value (name, combo, integer usage, "0x.." string) to usages.

    Mirrors API.md section 3: names are case-insensitive, ``+`` separates combo
    members, integers 0-255 and "0x.." strings are raw usages.
    Raises EncodeError for anything invalid.
    """
    if isinstance(spec, bool):
        raise EncodeError("key must be a string or integer, not a boolean")
    if isinstance(spec, int):
        if not 0 <= spec <= 255:
            raise EncodeError("key usage %d out of range 0-255" % spec)
        return [spec]
    if not isinstance(spec, str):
        raise EncodeError("key must be a string or integer")
    s = spec.strip().lower()
    if s.startswith("0x") and len(s) > 2:
        try:
            v = int(s, 16)
        except ValueError:
            raise EncodeError("bad hex key %r" % spec)
        if not 0 <= v <= 255:
            raise EncodeError("key usage %s out of range 0-255" % spec)
        return [v]
    parts = s.split("+")
    out = []
    for p in parts:
        if p not in KEY_NAMES:
            raise EncodeError("unknown key name %r in %r" % (p, spec))
        out.append(KEY_NAMES[p])
    return out


# US layout: character -> (usage, shift)
_SHIFTED = {"1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*",
            "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|", ";": ":",
            "'": '"', "`": "~", ",": "<", ".": ">", "/": "?"}
CHAR_TO_KEY: Dict[str, Tuple[int, bool]] = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    CHAR_TO_KEY[_c] = (KEY_NAMES[_c], False)
    CHAR_TO_KEY[_c.upper()] = (KEY_NAMES[_c], True)
for _c, _s in _SHIFTED.items():
    CHAR_TO_KEY[_c] = (KEY_NAMES[_c], False)
    CHAR_TO_KEY[_s] = (KEY_NAMES[_c], True)
CHAR_TO_KEY[" "] = (44, False)
CHAR_TO_KEY["\n"] = (40, False)
CHAR_TO_KEY["\t"] = (43, False)
CHAR_TO_KEY["\b"] = (42, False)
CHAR_TO_KEY["\x1b"] = (41, False)
#: (usage, shifted) -> character, for decoding
KEY_TO_CHAR: Dict[Tuple[int, bool], str] = {v: k for k, v in CHAR_TO_KEY.items()}
PRINTABLE_ASCII = "".join(chr(c) for c in range(0x20, 0x7F))
CONTROL_CHARS = {40: "\n", 43: "\t", 42: "\b", 41: "\x1b"}


def normalize_text(text: str) -> str:
    """What a text step is expected to type: ``\\r\\n`` and lone ``\\r`` become ``\\n``."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def unsupported_chars(text: str) -> List[Tuple[int, int, str]]:
    """[(char_index, utf8_byte_offset, char)] for characters a text step must reject."""
    out = []
    off = 0
    for i, ch in enumerate(text):
        if ch not in CHAR_TO_KEY and ch != "\r":
            out.append((i, off, ch))
        off += len(ch.encode("utf-8"))
    return out


# ---------------------------------------------------------------------------
# Report parsing / formatting
# ---------------------------------------------------------------------------

def _int8(b: int) -> int:
    return b - 256 if b > 127 else b


def _u8(v: int) -> int:
    if not -128 <= v <= 255:
        raise ValueError("value %d does not fit a byte" % v)
    return v & 0xFF


@dataclass(frozen=True)
class KbdReport:
    mods: int
    keys: Tuple[int, ...]  # non-zero entries, slot order
    reserved: int
    raw: bytes

    @property
    def pressed(self) -> frozenset:
        """All pressed usages including modifiers (224-231)."""
        s = set(self.keys)
        for i in range(8):
            if self.mods & (1 << i):
                s.add(MOD_FIRST + i)
        return frozenset(s)

    @property
    def empty(self) -> bool:
        return self.mods == 0 and not self.keys


@dataclass(frozen=True)
class MouseReport:
    buttons: int
    dx: int
    dy: int
    wheel: int
    pan: int
    length: int
    raw: bytes


def parse_kbd(data: bytes) -> KbdReport:
    data = bytes(data)
    if len(data) != KBD_REPORT_LEN:
        raise ValueError("keyboard report must be 8 bytes, got %d: %s" % (len(data), data.hex(" ")))
    return KbdReport(data[0], tuple(b for b in data[2:8] if b), data[1], data)


def parse_mouse(data: bytes) -> MouseReport:
    data = bytes(data)
    if len(data) < 3:
        raise ValueError("mouse report too short: %s" % data.hex(" "))
    b = list(data) + [0, 0]
    return MouseReport(b[0], _int8(b[1]), _int8(b[2]), _int8(b[3]) if len(data) > 3 else 0,
                       _int8(b[4]) if len(data) > 4 else 0, len(data), data)


def kbd_bytes(mods: int = 0, keys: Sequence[int] = ()) -> bytes:
    keys = list(keys)
    if len(keys) > 6:
        raise ValueError("more than 6 keys")
    return bytes([mods & 0xFF, 0] + keys + [0] * (6 - len(keys)))


def mouse_bytes(buttons: int = 0, dx: int = 0, dy: int = 0, wheel: int = 0, pan: int = 0,
                boot: bool = False) -> bytes:
    b = bytes([buttons & 0xFF, _u8(dx), _u8(dy), _u8(wheel), _u8(pan)])
    return b[:3] if boot else b


def state_to_kbd(pressed: Iterable[int]) -> bytes:
    """Build a keyboard report from a set/sequence of usages (modifiers go to the bitmap)."""
    mods = 0
    keys = []
    for u in pressed:
        if is_modifier(u):
            mods |= 1 << (u - MOD_FIRST)
        else:
            keys.append(u)
    return kbd_bytes(mods, keys)


def mods_names(mods: int) -> List[str]:
    return [MODIFIER_NAMES[i] for i in range(8) if mods & (1 << i)]


def describe_kbd(data: bytes) -> str:
    try:
        r = parse_kbd(data)
    except ValueError as e:
        return "INVALID(%s)" % e
    parts = mods_names(r.mods) + [usage_name(k) for k in r.keys]
    s = "+".join(parts) if parts else "-"
    if r.reserved:
        s += " [reserved=0x%02x]" % r.reserved
    return s


def describe_mouse(data: bytes) -> str:
    try:
        r = parse_mouse(data)
    except ValueError as e:
        return "INVALID(%s)" % e
    btn = "+".join(n for n in BUTTON_NAMES if r.buttons & BUTTON_BITS[n]) or "-"
    extra = r.buttons & ~0x1F
    if extra:
        btn += "|0x%02x" % extra
    s = "btn=%s dx=%+d dy=%+d" % (btn, r.dx, r.dy)
    if r.length > 3:
        s += " wheel=%+d pan=%+d" % (r.wheel, r.pan)
    if r.length not in (3, 5):
        s += " len=%d" % r.length
    return s


def describe(ep: str, data: bytes) -> str:
    return describe_kbd(data) if ep == "kbd" else describe_mouse(data)


# ---------------------------------------------------------------------------
# Report log entries
# ---------------------------------------------------------------------------

class Report(NamedTuple):
    seq: int     # global order (shared by both endpoints and log events)
    t_ns: int    # time.monotonic_ns() when the transfer completed
    ep: str      # "kbd" or "mouse"
    data: bytes
    gen: int = 0  # connection generation (increments on every (re-)acquisition)


class LogEvent(NamedTuple):
    seq: int
    t_ns: int
    kind: str    # connect, disconnect, pause, resume, error, detach, ...
    info: dict


def _ep_data(r: Any, default_ep: Optional[str] = None) -> Tuple[str, bytes]:
    if isinstance(r, (bytes, bytearray)):
        if default_ep is None:
            raise ValueError("bare bytes need default_ep")
        return default_ep, bytes(r)
    if isinstance(r, Report):
        return r.ep, bytes(r.data)
    if isinstance(r, Expected):
        return r.ep, bytes(r.data)
    if isinstance(r, tuple) and len(r) == 2:
        return r[0], bytes(r[1])
    raise TypeError("cannot interpret %r as a report" % (r,))


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyEvent:
    kind: str          # "down", "up" or "rollover"
    usage: int
    index: int         # index of the report that produced the event
    mods: int          # modifier bitmap after applying the report
    t_ns: Optional[int] = None

    def __str__(self) -> str:
        return "%s %s" % (self.kind, usage_name(self.usage)) if self.kind != "rollover" else "rollover"


def kbd_events(reports: Iterable[Any], initial: bytes = bytes(8)) -> List[KeyEvent]:
    """Diff consecutive keyboard reports into key down/up events.

    Order inside one report follows the Linux HID input layer: modifier changes
    (in bit order) first, then array releases, then array presses.  An
    ErrorRollOver report (all key slots = 0x01) yields one "rollover" event
    and leaves the key state unchanged, as a real host does.
    """
    prev = parse_kbd(initial)
    prev_keys = list(prev.keys)
    prev_mods = prev.mods
    out: List[KeyEvent] = []
    for idx, r in enumerate(reports):
        ep, data = _ep_data(r, "kbd")
        t_ns = r.t_ns if isinstance(r, Report) else None
        cur = parse_kbd(data)
        if len(data) == 8 and all(b == USAGE_ERROR_ROLLOVER for b in data[2:8]):
            out.append(KeyEvent("rollover", USAGE_ERROR_ROLLOVER, idx, prev_mods, t_ns))
            continue
        for bit in range(8):
            m = 1 << bit
            if (prev_mods ^ cur.mods) & m:
                kind = "down" if cur.mods & m else "up"
                out.append(KeyEvent(kind, MOD_FIRST + bit, idx, cur.mods, t_ns))
        cur_keys = list(cur.keys)
        for k in prev_keys:
            if k not in cur_keys:
                out.append(KeyEvent("up", k, idx, cur.mods, t_ns))
        for k in cur_keys:
            if k not in prev_keys:
                out.append(KeyEvent("down", k, idx, cur.mods, t_ns))
        prev_keys, prev_mods = cur_keys, cur.mods
    return out


def key_to_text(usage: int, mods: int, caps: bool = False) -> Optional[str]:
    """The character a US-layout host types for a key press, or None if it is not text."""
    if mods & MOD_NONSHIFT_MASK:
        return None
    shift = bool(mods & MOD_SHIFT_MASK)
    if usage in CONTROL_CHARS:
        return None if shift else CONTROL_CHARS[usage]
    if 4 <= usage <= 29:
        c = chr(ord("a") + usage - 4)
        return c.upper() if (shift != caps) else c
    return KEY_TO_CHAR.get((usage, shift))


def kbd_text(reports: Iterable[Any], caps: bool = False, strict: bool = True,
             initial: bytes = bytes(8), track_capslock: bool = False) -> str:
    """The text that a US-layout host would type for these keyboard reports.

    caps: the host's CapsLock state (as set through the LED output report).
    strict: raise DecodeError on any key press that is not plain text (F-keys,
      arrows, Ctrl/Alt/GUI combinations, rollover).  With strict=False such
      presses are rendered as ``<name>`` tokens.
    track_capslock: toggle the CapsLock state when the CapsLock key goes down.
    """
    out = []
    for ev in kbd_events(reports, initial):
        if ev.kind == "rollover":
            if strict:
                raise DecodeError("ErrorRollOver report at index %d" % ev.index, ev.index)
            out.append("<rollover>")
            continue
        if ev.kind != "down" or is_modifier(ev.usage):
            continue
        if track_capslock and ev.usage == USAGE_CAPSLOCK:
            caps = not caps
            continue
        ch = key_to_text(ev.usage, ev.mods, caps)
        if ch is None:
            token = "<%s>" % "+".join(mods_names(ev.mods) + [usage_name(ev.usage)])
            if strict:
                raise DecodeError("non-text key press %s at report %d" % (token, ev.index), ev.index)
            out.append(token)
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class MouseSummary:
    dx: int = 0
    dy: int = 0
    wheel: int = 0
    pan: int = 0
    n_reports: int = 0
    button_events: List[Tuple[str, str, int]] = field(default_factory=list)  # (kind, name, index)
    final_buttons: int = 0
    lengths: List[int] = field(default_factory=list)

    @property
    def clicks(self) -> Dict[str, int]:
        c: Dict[str, int] = collections.Counter()
        for kind, name, _ in self.button_events:
            if kind == "down":
                c[name] += 1
        return dict(c)


def mouse_summary(reports: Iterable[Any], initial_buttons: int = 0) -> MouseSummary:
    s = MouseSummary(final_buttons=initial_buttons)
    prev = initial_buttons
    for idx, r in enumerate(reports):
        _, data = _ep_data(r, "mouse")
        m = parse_mouse(data)
        s.dx += m.dx
        s.dy += m.dy
        s.wheel += m.wheel
        s.pan += m.pan
        s.n_reports += 1
        s.lengths.append(m.length)
        for name in BUTTON_NAMES:
            bit = BUTTON_BITS[name]
            if (prev ^ m.buttons) & bit:
                s.button_events.append(("down" if m.buttons & bit else "up", name, idx))
        prev = m.buttons
    s.final_buttons = prev
    return s


def _round_div(a: int, b: int) -> int:
    """Round a/b half away from zero (b > 0), integer arithmetic only."""
    q = (2 * abs(a) + b) // (2 * b)
    return q if a >= 0 else -q


def split_move(dx: int, dy: int, step: int = 127) -> List[Tuple[int, int]]:
    """Canonical straight-line split of a relative move into per-report deltas.

    n = ceil(max(|dx|, |dy|) / step) reports; report i ends at round(i*d/n).
    The firmware may use a different (equally valid) split; tests should use
    :func:`check_move_split` for properties rather than comparing exactly.
    """
    if step < 1:
        raise ValueError("step must be >= 1")
    n = max(-(-abs(dx) // step), -(-abs(dy) // step))
    out = []
    px = py = 0
    for i in range(1, n + 1):
        x = _round_div(dx * i, n)
        y = _round_div(dy * i, n)
        out.append((x - px, y - py))
        px, py = x, y
    return out


def check_move_split(reports: Iterable[Any], dx: int, dy: int, step: int = 127,
                     line_tol: float = 2.0, max_extra_reports: int = 1) -> List[str]:
    """Check API.md move properties on the mouse reports of one move. Returns problems."""
    deltas = []
    for r in reports:
        _, data = _ep_data(r, "mouse")
        m = parse_mouse(data)
        deltas.append((m.dx, m.dy, m.wheel, m.pan))
    problems = []
    sx = sum(d[0] for d in deltas)
    sy = sum(d[1] for d in deltas)
    if (sx, sy) != (dx, dy):
        problems.append("sum of reports is (%d,%d), expected (%d,%d)" % (sx, sy, dx, dy))
    minimal = max(-(-abs(dx) // step), -(-abs(dy) // step))
    if len(deltas) > minimal + max_extra_reports:
        problems.append("%d reports for a move needing %d (step %d)" % (len(deltas), minimal, step))
    length = math.hypot(dx, dy)
    x = y = 0
    for i, (ddx, ddy, wh, pan) in enumerate(deltas):
        if abs(ddx) > step or abs(ddy) > step:
            problems.append("report %d delta (%d,%d) exceeds step %d" % (i, ddx, ddy, step))
        if (ddx and dx and (ddx > 0) != (dx > 0)) or (ddx and not dx):
            problems.append("report %d x delta %d goes against the move direction" % (i, ddx))
        if (ddy and dy and (ddy > 0) != (dy > 0)) or (ddy and not dy):
            problems.append("report %d y delta %d goes against the move direction" % (i, ddy))
        if wh or pan:
            problems.append("report %d has wheel/pan %d/%d during a move" % (i, wh, pan))
        x += ddx
        y += ddy
        if length:
            dist = abs(x * dy - y * dx) / length
            if dist > line_tol:
                problems.append("after report %d the pointer is %.1f units off the straight line"
                                % (i, dist))
    return problems[:20]


def check_scroll_split(reports: Iterable[Any], v: int, h: int = 0) -> List[str]:
    """Check scroll properties: exact sums, int8 bounds, consistent signs, no motion."""
    problems = []
    sw = sp = 0
    for i, r in enumerate(reports):
        _, data = _ep_data(r, "mouse")
        m = parse_mouse(data)
        if m.dx or m.dy:
            problems.append("report %d moves the pointer (%d,%d) during scroll" % (i, m.dx, m.dy))
        if m.wheel and (not v or (m.wheel > 0) != (v > 0)):
            problems.append("report %d wheel %d has the wrong sign" % (i, m.wheel))
        if m.pan and (not h or (m.pan > 0) != (h > 0)):
            problems.append("report %d pan %d has the wrong sign" % (i, m.pan))
        sw += m.wheel
        sp += m.pan
    if (sw, sp) != (v, h):
        problems.append("wheel/pan totals are (%d,%d), expected (%d,%d)" % (sw, sp, v, h))
    return problems


# ---------------------------------------------------------------------------
# Reference encoder
# ---------------------------------------------------------------------------

class Expected(NamedTuple):
    ep: str
    data: bytes
    gap_ms: float = 0.0   # nominal minimum time since the previous report (any endpoint)
    note: str = ""


VERBS = ("text", "key", "move", "click", "button", "scroll", "delay", "release")


def _is_int(v: Any) -> bool:
    return type(v) is int


class ReferenceEncoder:
    """Produce the exact report sequence API.md prescribes for text / steps.

    The encoder keeps the held-key / held-button state between calls, so a
    sequence of calls models consecutive jobs.  Validation mirrors API.md and
    raises :class:`EncodeError` with the step ``index`` (and ``offset`` for
    unsupported text characters) the device is expected to report.

    Interpretations where API.md is silent (see the test report):
      * Shift for text is left shift (bit 0x02); the release report restores the
        previously held state (normally all zero).
      * key tap: one report per pressed key in order, one per released key in
        reverse order; the gap before the first release is hold_ms if given,
        else delay_ms.  ``up`` of a combo releases in reverse order.
      * releasing a key compacts the key array; new keys are appended.
      * move/scroll use :func:`split_move`; a zero move/scroll emits nothing.
      * click: press, wait hold_ms, release, wait interval_ms, next press.
      * a "release" step always emits an all-released report on both
        endpoints, keyboard first (API.md section 10).
      * in boot mouse protocol, scroll emits nothing (no wheel in 3-byte reports).
    """

    def __init__(self, caps: bool = False, key_delay_ms: int = 5, boot_mouse: bool = False,
                 held_keys: Sequence[int] = (), held_buttons: int = 0):
        self.caps = caps
        self.key_delay_ms = key_delay_ms
        self.boot_mouse = boot_mouse
        self.mods = 0
        self.keys: List[int] = []
        for u in held_keys:
            self._press(u)
        self.buttons = held_buttons
        self._pending_gap = 0.0

    # -- state helpers -----------------------------------------------------
    def copy(self) -> "ReferenceEncoder":
        e = ReferenceEncoder(self.caps, self.key_delay_ms, self.boot_mouse)
        e.mods, e.keys, e.buttons, e._pending_gap = self.mods, list(self.keys), self.buttons, self._pending_gap
        return e

    def _press(self, u: int) -> bool:
        if is_modifier(u):
            bit = 1 << (u - MOD_FIRST)
            if self.mods & bit:
                return False
            self.mods |= bit
            return True
        if u in self.keys:
            return False
        if len(self.keys) >= 6:
            raise EncodeError("more than 6 non-modifier keys held")
        self.keys.append(u)
        return True

    def _release(self, u: int) -> bool:
        if is_modifier(u):
            bit = 1 << (u - MOD_FIRST)
            if not self.mods & bit:
                return False
            self.mods &= ~bit
            return True
        if u not in self.keys:
            return False
        self.keys.remove(u)
        return True

    def _kbd(self) -> bytes:
        return kbd_bytes(self.mods, self.keys)

    def _mouse(self, dx: int = 0, dy: int = 0, wheel: int = 0, pan: int = 0) -> bytes:
        return mouse_bytes(self.buttons, dx, dy, wheel, pan, boot=self.boot_mouse)

    def _emit(self, out: List[Expected], ep: str, data: bytes, gap: float, note: str = "") -> None:
        out.append(Expected(ep, data, gap + self._pending_gap, note))
        self._pending_gap = 0.0

    @property
    def held(self) -> List[int]:
        """Held usages (modifiers as 224-231), like /api/status hid.keys."""
        return sorted(parse_kbd(self._kbd()).pressed)

    @property
    def held_buttons(self) -> List[str]:
        return [n for n in BUTTON_NAMES if self.buttons & BUTTON_BITS[n]]

    # -- verbs -------------------------------------------------------------
    def text(self, text: str, delay_ms: Optional[int] = None) -> List[Expected]:
        bad = unsupported_chars(text)
        if bad:
            i, off, ch = bad[0]
            raise EncodeError("unsupported character %r at byte offset %d" % (ch, off), offset=off)
        d = self.key_delay_ms if delay_ms is None else delay_ms
        out: List[Expected] = []
        for ch in normalize_text(text):
            usage, shift = CHAR_TO_KEY[ch]
            if self.caps and ch.isalpha():
                shift = not shift
            mods = self.mods | (0x02 if shift else 0)
            keys = self.keys + ([usage] if usage not in self.keys else [])
            self._emit(out, "kbd", kbd_bytes(mods, keys), d if out else 0, repr(ch))
            self._emit(out, "kbd", self._kbd(), d)
        return out

    def key(self, spec: Union[int, str], action: str = "tap", repeat: int = 1,
            hold_ms: Optional[int] = None, delay_ms: Optional[int] = None) -> List[Expected]:
        usages = parse_key_spec(spec)
        d = self.key_delay_ms if delay_ms is None else delay_ms
        out: List[Expected] = []
        if action == "down":
            for u in usages:
                if self._press(u):
                    self._emit(out, "kbd", self._kbd(), d if out else 0)
            return out
        if action == "up":
            for u in reversed(usages):
                if self._release(u):
                    self._emit(out, "kbd", self._kbd(), d if out else 0)
            return out
        if action != "tap":
            raise EncodeError("bad key action %r" % action)
        for _ in range(repeat):
            pressed = []
            for u in usages:
                if self._press(u):
                    pressed.append(u)
                    self._emit(out, "kbd", self._kbd(), d if out else 0)
            first = True
            for u in reversed(pressed):
                self._release(u)
                gap = (hold_ms if hold_ms is not None else d) if first else d
                self._emit(out, "kbd", self._kbd(), gap)
                first = False
        return out

    def move(self, dx: int, dy: int, step: int = 127, delay_ms: int = 0) -> List[Expected]:
        out: List[Expected] = []
        for ddx, ddy in split_move(dx, dy, step):
            self._emit(out, "mouse", self._mouse(ddx, ddy), delay_ms if out else 0)
        return out

    def click(self, button: str = "left", count: int = 1, hold_ms: int = 20,
              interval_ms: int = 60) -> List[Expected]:
        bit = BUTTON_BITS[button]
        out: List[Expected] = []
        base = self.buttons
        for i in range(count):
            self.buttons = base | bit
            self._emit(out, "mouse", self._mouse(), interval_ms if i else 0, "%s down" % button)
            self.buttons = base & ~bit
            self._emit(out, "mouse", self._mouse(), hold_ms, "%s up" % button)
        self.buttons = base & ~bit
        return out

    def button(self, button: str, action: str) -> List[Expected]:
        bit = BUTTON_BITS[button]
        before = self.buttons
        self.buttons = (before | bit) if action == "down" else (before & ~bit)
        if self.buttons == before:
            return []
        out: List[Expected] = []
        self._emit(out, "mouse", self._mouse(), 0, "%s %s" % (button, action))
        return out

    def scroll(self, v: int, h: int = 0) -> List[Expected]:
        out: List[Expected] = []
        if self.boot_mouse:
            return out
        for dv, dh in split_move(v, h, 127):
            self._emit(out, "mouse", self._mouse(wheel=dv, pan=dh), 0)
        return out

    def delay(self, ms: int) -> List[Expected]:
        self._pending_gap += ms
        return []

    def release(self) -> List[Expected]:
        out: List[Expected] = []
        self.mods, self.keys, self.buttons = 0, [], 0
        self._emit(out, "kbd", self._kbd(), 0, "release")
        self._emit(out, "mouse", self._mouse(), 0, "release")
        return out

    # -- steps -------------------------------------------------------------
    @staticmethod
    def validate_step(step: Any, index: int = 0) -> str:
        """Validate one step's shape and ranges; return its verb.  Raises EncodeError."""
        def bad(msg: str, offset: Optional[int] = None) -> EncodeError:
            return EncodeError("step %d: %s" % (index, msg), index, offset)

        if not isinstance(step, dict):
            raise bad("step must be an object")
        verbs = [v for v in VERBS if v in step]
        if len(verbs) != 1:
            raise bad("step must contain exactly one verb, found %s" % (verbs or "none"))
        verb = verbs[0]
        val = step[verb]

        def opt_int(name: str, lo: int, hi: Optional[int]) -> None:
            if name in step:
                v = step[name]
                if not _is_int(v):
                    raise bad("%s must be an integer" % name)
                if v < lo or (hi is not None and v > hi):
                    raise bad("%s=%d out of range" % (name, v))

        if verb == "text":
            if not isinstance(val, str):
                raise bad("text must be a string")
            badc = unsupported_chars(val)
            if badc:
                raise bad("unsupported character %r" % badc[0][2], badc[0][1])
            opt_int("delay_ms", 0, None)
        elif verb == "key":
            if isinstance(val, bool) or not isinstance(val, (int, str)):
                raise bad("key must be a string or integer")
            try:
                parse_key_spec(val)
            except EncodeError as e:
                raise bad(str(e))
            action = step.get("action", "tap")
            if action not in ("tap", "down", "up"):
                raise bad("bad action %r" % (action,))
            opt_int("repeat", 1, 1000)
            opt_int("hold_ms", 0, None)
            opt_int("delay_ms", 0, None)
            opt_int("ttl_ms", 1, 600000)
        elif verb == "move":
            if not (isinstance(val, list) and len(val) == 2 and all(_is_int(v) for v in val)):
                raise bad("move must be [dx, dy] integers")
            if any(abs(v) > 32767 for v in val):
                raise bad("move value out of range")
            opt_int("step", 1, 127)
            opt_int("delay_ms", 0, None)
        elif verb == "click":
            if val not in BUTTON_BITS:
                raise bad("unknown button %r" % (val,))
            opt_int("count", 1, 100)
            opt_int("hold_ms", 0, None)
            opt_int("interval_ms", 0, None)
        elif verb == "button":
            if val not in BUTTON_BITS:
                raise bad("unknown button %r" % (val,))
            if step.get("action") not in ("down", "up"):
                raise bad("button needs action down/up")
            opt_int("ttl_ms", 1, 600000)
        elif verb == "scroll":
            if _is_int(val):
                vals = [val]
            elif isinstance(val, list) and len(val) == 2 and all(_is_int(v) for v in val):
                vals = val
            else:
                raise bad("scroll must be an integer or [v, h]")
            if any(abs(v) > 32767 for v in vals):
                raise bad("scroll value out of range")
        elif verb == "delay":
            if not _is_int(val):
                raise bad("delay must be an integer")
            if not 0 <= val <= 60000:
                raise bad("delay out of range")
        elif verb == "release":
            if val is not True:
                raise bad("release must be true")
        return verb

    def step(self, step: Dict[str, Any], index: int = 0) -> List[Expected]:
        verb = self.validate_step(step, index)
        try:
            if verb == "text":
                return self.text(step["text"], step.get("delay_ms"))
            if verb == "key":
                return self.key(step["key"], step.get("action", "tap"), step.get("repeat", 1),
                                step.get("hold_ms"), step.get("delay_ms"))
            if verb == "move":
                return self.move(step["move"][0], step["move"][1], step.get("step", 127),
                                 step.get("delay_ms", 0))
            if verb == "click":
                return self.click(step["click"], step.get("count", 1), step.get("hold_ms", 20),
                                  step.get("interval_ms", 60))
            if verb == "button":
                return self.button(step["button"], step["action"])
            if verb == "scroll":
                v = step["scroll"]
                return self.scroll(v, 0) if _is_int(v) else self.scroll(v[0], v[1])
            if verb == "delay":
                return self.delay(step["delay"])
            return self.release()
        except EncodeError as e:
            e.index = index
            raise

    def steps(self, steps: Sequence[Dict[str, Any]]) -> List[Expected]:
        """Encode a whole job atomically: validate everything (including the
        6-key limit) on a copy first, then encode for real."""
        trial = self.copy()
        for i, s in enumerate(steps):
            trial.step(s, i)
        out: List[Expected] = []
        for i, s in enumerate(steps):
            out.extend(self.step(s, i))
        return out


def expected_text_reports(text: str, caps: bool = False, delay_ms: int = 5) -> List[Expected]:
    return ReferenceEncoder(caps=caps, key_delay_ms=delay_ms).text(text)


def expected_step_reports(steps: Sequence[Dict[str, Any]], caps: bool = False,
                          key_delay_ms: int = 5, boot_mouse: bool = False) -> List[Expected]:
    return ReferenceEncoder(caps=caps, key_delay_ms=key_delay_ms, boot_mouse=boot_mouse).steps(steps)


# ---------------------------------------------------------------------------
# Comparison and diff
# ---------------------------------------------------------------------------

def _cmp_key(ep: str, data: bytes, exact: bool) -> Tuple:
    if ep == "kbd" and not exact and len(data) == 8:
        r = parse_kbd(data)
        return ("kbd", r.mods, r.reserved, tuple(sorted(r.keys)), tuple(sorted(b for b in data[2:] if not b)))
    return (ep, bytes(data))


def _fmt_line(tag: str, ei: Optional[int], ai: Optional[int], ep: str, data: bytes,
              t_ns: Optional[int] = None, t0: Optional[int] = None) -> str:
    e = "%5d" % ei if ei is not None else "    ."
    a = "%5d" % ai if ai is not None else "    ."
    ts = ""
    if t_ns is not None and t0 is not None:
        ts = " %9.3fms" % ((t_ns - t0) / 1e6)
    return "%s %s %s %-5s %-23s %s%s" % (tag, e, a, ep, bytes(data).hex(" "), describe(ep, data), ts)


def compare_reports(expected: Sequence[Any], actual: Sequence[Any], exact: bool = False,
                    default_ep: Optional[str] = None, context: int = 3, max_lines: int = 120,
                    caps: bool = False) -> str:
    """Compare expected vs actual report sequences.  Returns "" when they match,
    otherwise a readable diff (missing reports marked '-', extra '+').

    exact=False compares keyboard reports semantically (modifier bitmap,
    reserved byte and the *set* of keys), because API.md does not fix the slot
    order inside the 6-key array.  Mouse reports are always compared byte-exact
    (which includes the 3- vs 5-byte length).
    """
    exp = [_ep_data(r, default_ep) for r in expected]
    act_raw = list(actual)
    act = [_ep_data(r, default_ep) for r in act_raw]
    times = [r.t_ns if isinstance(r, Report) else None for r in act_raw]
    t0 = times[0] if times and times[0] is not None else None
    ek = [_cmp_key(ep, d, exact) for ep, d in exp]
    ak = [_cmp_key(ep, d, exact) for ep, d in act]
    if ek == ak:
        return ""
    lines = ["report sequence mismatch: expected %d reports, got %d (%s compare)"
             % (len(exp), len(act), "exact" if exact else "semantic")]
    for label, seq in (("expected", exp), ("actual", act)):
        kb = [d for ep, d in seq if ep == "kbd"]
        if kb:
            try:
                txt = kbd_text(kb, caps=caps, strict=False)
            except ValueError as e:
                txt = "<undecodable: %s>" % e
            lines.append("  %-8s keyboard text: %r" % (label, txt[:200] + ("..." if len(txt) > 200 else "")))
        ms = [d for ep, d in seq if ep == "mouse"]
        if ms:
            try:
                s = mouse_summary(ms)
                lines.append("  %-8s mouse: dx=%d dy=%d wheel=%d pan=%d clicks=%s reports=%d"
                             % (label, s.dx, s.dy, s.wheel, s.pan, s.clicks, s.n_reports))
            except ValueError as e:
                lines.append("  %-8s mouse: <undecodable: %s>" % (label, e))
    lines.append("  tag  exp#  act# ep    bytes                   decoded")
    sm = difflib.SequenceMatcher(None, ek, ak, autojunk=False)
    body: List[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            n = i2 - i1
            if n <= 2 * context:
                idx = list(range(n))
            else:
                idx = list(range(context)) + [-1] + list(range(n - context, n))
            for k in idx:
                if k == -1:
                    body.append("  ... %d matching reports ..." % (n - 2 * context))
                    continue
                body.append(_fmt_line(" ", i1 + k, j1 + k, act[j1 + k][0], act[j1 + k][1], times[j1 + k], t0))
            continue
        if op in ("replace", "delete"):
            for k in range(i1, i2):
                body.append(_fmt_line("-", k, None, exp[k][0], exp[k][1]))
        if op in ("replace", "insert"):
            for k in range(j1, j2):
                body.append(_fmt_line("+", None, k, act[k][0], act[k][1], times[k], t0))
    if len(body) > max_lines:
        body = body[:max_lines] + ["  ... (%d more lines)" % (len(body) - max_lines)]
    lines += body
    lines.append("  ('-' = expected but missing, '+' = emitted but not expected)")
    return "\n".join(lines)


def assert_reports_equal(expected: Sequence[Any], actual: Sequence[Any], exact: bool = False,
                         msg: str = "", **kw: Any) -> None:
    d = compare_reports(expected, actual, exact=exact, **kw)
    if d:
        raise AssertionError((msg + "\n" if msg else "") + d)


def timing_problems(expected: Sequence[Expected], actual: Sequence[Report], tol_ms: float = 3.0,
                    min_gap_ms: float = 2.0) -> List[str]:
    """Check that every actual inter-report gap is at least the expected nominal
    gap minus tol_ms (only for expected gaps >= min_gap_ms).  Sequences must
    already match in length."""
    problems = []
    for i in range(1, min(len(expected), len(actual))):
        want = expected[i].gap_ms
        if want < min_gap_ms:
            continue
        got = (actual[i].t_ns - actual[i - 1].t_ns) / 1e6
        if got < want - tol_ms:
            problems.append("gap before report %d is %.1f ms, expected >= %.1f ms" % (i, got, want))
    return problems


def gaps_ms(reports: Sequence[Report]) -> List[float]:
    return [(reports[i].t_ns - reports[i - 1].t_ns) / 1e6 for i in range(1, len(reports))]


# ---------------------------------------------------------------------------
# USB descriptor parsing and API.md section 9 verification
# ---------------------------------------------------------------------------

def parse_device_descriptor(b: bytes) -> Dict[str, int]:
    b = bytes(b)
    if len(b) < 18 or b[1] != 0x01:
        raise ValueError("not a device descriptor: %s" % b.hex(" "))
    w = lambda o: b[o] | (b[o + 1] << 8)  # noqa: E731
    return {"bLength": b[0], "bcdUSB": w(2), "bDeviceClass": b[4], "bDeviceSubClass": b[5],
            "bDeviceProtocol": b[6], "bMaxPacketSize0": b[7], "idVendor": w(8), "idProduct": w(10),
            "bcdDevice": w(12), "iManufacturer": b[14], "iProduct": b[15], "iSerialNumber": b[16],
            "bNumConfigurations": b[17]}


def parse_config_descriptor(b: bytes) -> Dict[str, Any]:
    """Parse a full configuration descriptor (with interfaces, HID and endpoint descriptors)."""
    b = bytes(b)
    if len(b) < 9 or b[1] != 0x02:
        raise ValueError("not a configuration descriptor")
    cfg: Dict[str, Any] = {"wTotalLength": b[2] | (b[3] << 8), "bNumInterfaces": b[4],
                           "bConfigurationValue": b[5], "iConfiguration": b[6],
                           "bmAttributes": b[7], "bMaxPower": b[8], "interfaces": [], "other": []}
    cfg["self_powered"] = bool(b[7] & 0x40)
    cfg["remote_wakeup"] = bool(b[7] & 0x20)
    pos = b[0]
    cur: Optional[Dict[str, Any]] = None
    while pos + 2 <= len(b):
        ln, typ = b[pos], b[pos + 1]
        if ln < 2:
            break
        d = b[pos:pos + ln]
        if typ == 0x04 and ln >= 9:
            cur = {"bInterfaceNumber": d[2], "bAlternateSetting": d[3], "bNumEndpoints": d[4],
                   "bInterfaceClass": d[5], "bInterfaceSubClass": d[6], "bInterfaceProtocol": d[7],
                   "iInterface": d[8], "endpoints": [], "hid": None}
            cfg["interfaces"].append(cur)
        elif typ == 0x05 and ln >= 7 and cur is not None:
            cur["endpoints"].append({"bEndpointAddress": d[2], "bmAttributes": d[3],
                                     "wMaxPacketSize": d[4] | (d[5] << 8), "bInterval": d[6]})
        elif typ == 0x21 and ln >= 9 and cur is not None:
            hid = {"bcdHID": d[2] | (d[3] << 8), "bCountryCode": d[4], "bNumDescriptors": d[5],
                   "descriptors": []}
            for k in range(d[5]):
                o = 6 + 3 * k
                if o + 3 <= ln:
                    hid["descriptors"].append({"bDescriptorType": d[o], "wDescriptorLength": d[o + 1] | (d[o + 2] << 8)})
            cur["hid"] = hid
        else:
            cfg["other"].append({"type": typ, "bytes": d.hex(" ")})
        pos += ln
    return cfg


@dataclass
class HidField:
    kind: str                 # Input / Output / Feature
    report_id: int
    bit_offset: int           # offset within the report (excluding the report-id byte)
    size: int
    count: int
    flags: int
    usage_page: int
    usages: List[int]         # explicit usages (extended: page<<16|usage)
    usage_min: Optional[int]
    usage_max: Optional[int]
    logical_min: int
    logical_max: int
    collection_usages: List[int]

    @property
    def constant(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def variable(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def relative(self) -> bool:
        return bool(self.flags & 0x04)

    def all_usages(self) -> List[int]:
        """Usages as (page << 16 | id)."""
        out = [u if u > 0xFFFF else (self.usage_page << 16) | u for u in self.usages]
        if self.usage_min is not None and self.usage_max is not None:
            lo = self.usage_min & 0xFFFF
            hi = self.usage_max & 0xFFFF
            out += [(self.usage_page << 16) | u for u in range(lo, hi + 1)]
        return out


@dataclass
class ReportDescriptor:
    raw: bytes
    fields: List[HidField]
    report_ids: List[int]
    collections: List[Tuple[int, int]]   # (collection type, extended usage)

    def bits(self, kind: str, report_id: int = 0) -> int:
        return sum(f.size * f.count for f in self.fields if f.kind == kind and f.report_id == report_id)

    def bytes_len(self, kind: str, report_id: int = 0) -> int:
        return (self.bits(kind, report_id) + 7) // 8

    def find(self, kind: str, usage: int) -> Optional[HidField]:
        for f in self.fields:
            if f.kind == kind and usage in f.all_usages():
                return f
        return None


def _signed(v: int, nbytes: int) -> int:
    bits = 8 * nbytes
    if nbytes and v & (1 << (bits - 1)):
        return v - (1 << bits)
    return v


def parse_report_descriptor(raw: bytes) -> ReportDescriptor:
    raw = bytes(raw)
    g = {"page": 0, "lmin": 0, "lmax": 0, "size": 0, "count": 0, "rid": 0}
    stack: List[dict] = []
    local: Dict[str, Any] = {"usages": [], "min": None, "max": None}
    offsets: Dict[Tuple[str, int], int] = collections.defaultdict(int)
    fields: List[HidField] = []
    rids: List[int] = []
    colls: List[Tuple[int, int]] = []
    coll_stack: List[int] = []
    pos = 0
    while pos < len(raw):
        prefix = raw[pos]
        if prefix == 0xFE:  # long item
            if pos + 2 >= len(raw):
                break
            pos += 3 + raw[pos + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        typ = (prefix >> 2) & 3
        tag = prefix >> 4
        data = raw[pos + 1:pos + 1 + size]
        val = int.from_bytes(data, "little") if data else 0
        sval = _signed(val, size)
        pos += 1 + size
        if typ == 0:  # main
            if tag in (8, 9, 0xB):
                kind = {8: "Input", 9: "Output", 0xB: "Feature"}[tag]
                key = (kind, g["rid"])
                f = HidField(kind, g["rid"], offsets[key], g["size"], g["count"], val, g["page"],
                             list(local["usages"]), local["min"], local["max"], g["lmin"], g["lmax"],
                             list(coll_stack))
                offsets[key] += g["size"] * g["count"]
                fields.append(f)
            elif tag == 0xA:
                u = local["usages"][0] if local["usages"] else 0
                eu = u if u > 0xFFFF else (g["page"] << 16) | u
                colls.append((val, eu))
                coll_stack.append(eu)
            elif tag == 0xC:
                if coll_stack:
                    coll_stack.pop()
            local = {"usages": [], "min": None, "max": None}
        elif typ == 1:  # global
            if tag == 0:
                g["page"] = val
            elif tag == 1:
                g["lmin"] = sval
            elif tag == 2:
                g["lmax"] = sval if g["lmin"] < 0 else val
            elif tag == 7:
                g["size"] = val
            elif tag == 8:
                g["rid"] = val
                rids.append(val)
            elif tag == 9:
                g["count"] = val
            elif tag == 0xA:
                stack.append(dict(g))
            elif tag == 0xB and stack:
                g = stack.pop()
        elif typ == 2:  # local
            if tag == 0:
                local["usages"].append(val)
            elif tag == 1:
                local["min"] = val
            elif tag == 2:
                local["max"] = val
    return ReportDescriptor(raw, fields, rids, colls)


def verify_keyboard_report_descriptor(rd: ReportDescriptor) -> List[str]:
    p = []
    if rd.report_ids:
        p.append("keyboard report descriptor uses report IDs %s (API.md: none)" % rd.report_ids)
    if (1, 0x00010006) not in rd.collections:
        p.append("no Application collection Generic Desktop / Keyboard")
    if rd.bits("Input") != 64:
        p.append("keyboard input report is %d bits, expected 64" % rd.bits("Input"))
    if rd.bits("Output") != 8:
        p.append("keyboard output report is %d bits, expected 8" % rd.bits("Output"))
    inputs = [f for f in rd.fields if f.kind == "Input"]
    mods = [f for f in inputs if f.usage_page == 7 and f.variable and f.size == 1]
    if not mods or mods[0].bit_offset != 0 or mods[0].count != 8 or \
            [u & 0xFFFF for u in mods[0].all_usages()] != list(range(0xE0, 0xE8)):
        p.append("byte 0 is not a modifier bitmap of usages 0xE0-0xE7")
    arr = [f for f in inputs if f.usage_page == 7 and not f.variable and not f.constant]
    if not arr or arr[0].bit_offset != 16 or arr[0].size != 8 or arr[0].count != 6:
        p.append("bytes 2-7 are not a 6-key array")
    else:
        a = arr[0]
        top = (a.usage_max if a.usage_max is not None else max([0] + a.usages)) & 0xFFFF
        if a.logical_max < 0x73 or top < 0x73:
            p.append("key array covers usages up to 0x%02x/logical max %d; F24 (0x73) needs more"
                     % (top, a.logical_max))
    reserved = [f for f in inputs if f.bit_offset == 8]
    if not reserved or not reserved[0].constant:
        p.append("byte 1 is not a constant reserved byte")
    leds = [f for f in rd.fields if f.kind == "Output" and f.usage_page == 8 and f.variable]
    if not leds or leds[0].bit_offset != 0:
        p.append("no LED output field at bit 0")
    else:
        u = [x & 0xFFFF for x in leds[0].all_usages()]
        if u[:3] != [1, 2, 3]:
            p.append("LED bits 0-2 are not NumLock/CapsLock/ScrollLock (usages %s)" % u[:3])
    return p


def verify_mouse_report_descriptor(rd: ReportDescriptor) -> List[str]:
    p = []
    if rd.report_ids:
        p.append("mouse report descriptor uses report IDs %s (API.md: none)" % rd.report_ids)
    if (1, 0x00010002) not in rd.collections:
        p.append("no Application collection Generic Desktop / Mouse")
    if rd.bits("Input") != 40:
        p.append("mouse input report is %d bits, expected 40" % rd.bits("Input"))
    b = rd.find("Input", 0x00090001)
    if b is None or b.bit_offset != 0 or b.size != 1 or b.count < 5 or \
            [u & 0xFFFF for u in b.all_usages()][:5] != [1, 2, 3, 4, 5]:
        p.append("buttons 1-5 are not bits 0-4")
    for usage, name, off in ((0x00010030, "X", 8), (0x00010031, "Y", 16), (0x00010038, "Wheel", 24),
                             (0x000C0238, "AC Pan", 32)):
        f = rd.find("Input", usage)
        if f is None:
            p.append("no %s field" % name)
            continue
        idx = f.all_usages().index(usage)
        bit = f.bit_offset + idx * f.size if f.variable else f.bit_offset
        if bit != off or f.size != 8:
            p.append("%s is at bit %d size %d, expected bit %d size 8" % (name, bit, f.size, off))
        if not f.relative:
            p.append("%s is not relative" % name)
        if f.logical_min > -127 or f.logical_max < 127:
            p.append("%s logical range %d..%d is narrower than -127..127" % (name, f.logical_min, f.logical_max))
    return p


def verify_usb_descriptors(dev: Dict[str, int], cfg: Dict[str, Any], speed: Optional[int] = None,
                           report_descs: Optional[Dict[int, bytes]] = None) -> List[str]:
    """All API.md section 9 checks on parsed descriptors. Returns a list of problems."""
    p = []
    if (dev["idVendor"], dev["idProduct"]) != (DEFAULT_VID, DEFAULT_PID):
        p.append("VID:PID is %04x:%04x" % (dev["idVendor"], dev["idProduct"]))
    if speed is not None and speed != 2:
        p.append("device speed code %s, expected full speed (2)" % speed)
    if cfg["self_powered"]:
        p.append("configuration says self-powered; API.md: bus powered")
    if not cfg["remote_wakeup"]:
        p.append("configuration does not advertise remote wakeup")
    if cfg["bNumInterfaces"] != 2:
        p.append("bNumInterfaces = %d, expected 2" % cfg["bNumInterfaces"])
    want = {0: (1, 1, KBD_EP, 8), 1: (1, 2, MOUSE_EP, 5)}
    for num, (sub, proto, ep, minpkt) in want.items():
        alts = [i for i in cfg["interfaces"] if i["bInterfaceNumber"] == num and i["bAlternateSetting"] == 0]
        if not alts:
            p.append("interface %d missing" % num)
            continue
        i = alts[0]
        if (i["bInterfaceClass"], i["bInterfaceSubClass"], i["bInterfaceProtocol"]) != (3, sub, proto):
            p.append("interface %d is class/sub/proto %d/%d/%d, expected 3/%d/%d"
                     % (num, i["bInterfaceClass"], i["bInterfaceSubClass"], i["bInterfaceProtocol"], sub, proto))
        eps = [e for e in i["endpoints"] if e["bEndpointAddress"] == ep]
        if not eps:
            p.append("interface %d has no endpoint 0x%02x" % (num, ep))
        else:
            e = eps[0]
            if e["bmAttributes"] & 3 != 3:
                p.append("endpoint 0x%02x is not interrupt" % ep)
            if e["bInterval"] != 1:
                p.append("endpoint 0x%02x bInterval %d, expected 1" % (ep, e["bInterval"]))
            if e["wMaxPacketSize"] & 0x7FF < minpkt:
                p.append("endpoint 0x%02x wMaxPacketSize %d < %d" % (ep, e["wMaxPacketSize"], minpkt))
        if i["hid"] is None:
            p.append("interface %d has no HID class descriptor" % num)
    if report_descs:
        if KBD_INTF in report_descs:
            p += verify_keyboard_report_descriptor(parse_report_descriptor(report_descs[KBD_INTF]))
        if MOUSE_INTF in report_descs:
            p += verify_mouse_report_descriptor(parse_report_descriptor(report_descs[MOUSE_INTF]))
    return p


# ---------------------------------------------------------------------------
# ReportLog
# ---------------------------------------------------------------------------

class ReportLog:
    """Thread-safe ordered log of reports (both endpoints) and harness events.

    Every entry gets a global sequence number from one counter, so the order
    of entries across endpoints is the order in which completions were
    observed.  :meth:`mark` returns the next sequence number; use it as the
    ``since`` argument of the query and wait methods.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._reports: List[Report] = []
        self._events: List[LogEvent] = []
        self._seq = 0
        self._last: Dict[str, Optional[Report]] = {"kbd": None, "mouse": None}
        self._last_t: Dict[Optional[str], int] = {None: 0, "kbd": 0, "mouse": 0}
        self.total: Dict[str, int] = {"kbd": 0, "mouse": 0}

    # -- producers ---------------------------------------------------------
    def append(self, ep: str, data: bytes, t_ns: Optional[int] = None, gen: int = 0) -> Report:
        with self._cond:
            r = Report(self._seq, t_ns if t_ns is not None else time.monotonic_ns(), ep, bytes(data), gen)
            self._seq += 1
            self._reports.append(r)
            self._last[ep] = r
            self._last_t[ep] = r.t_ns
            self._last_t[None] = r.t_ns
            self.total[ep] = self.total.get(ep, 0) + 1
            self._cond.notify_all()
            return r

    def event(self, kind: str, **info: Any) -> LogEvent:
        with self._cond:
            e = LogEvent(self._seq, time.monotonic_ns(), kind, info)
            self._seq += 1
            self._events.append(e)
            self._cond.notify_all()
            return e

    def reset_state(self) -> None:
        """Forget the host-side key/button state (device was disconnected)."""
        with self._cond:
            self._last = {"kbd": None, "mouse": None}

    # -- queries -----------------------------------------------------------
    def mark(self) -> int:
        with self._cond:
            return self._seq

    def reports(self, since: int = 0, ep: Optional[str] = None) -> List[Report]:
        with self._cond:
            return self._select(since, ep)

    def _select(self, since: int, ep: Optional[str]) -> List[Report]:
        # reports are sorted by seq: binary search the start
        lo, hi = 0, len(self._reports)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._reports[mid].seq < since:
                lo = mid + 1
            else:
                hi = mid
        sel = self._reports[lo:]
        return [r for r in sel if r.ep == ep] if ep else list(sel)

    def count(self, since: int = 0, ep: Optional[str] = None) -> int:
        return len(self.reports(since, ep))

    def events(self, since: int = 0, kind: Optional[str] = None) -> List[LogEvent]:
        with self._cond:
            return [e for e in self._events if e.seq >= since and (kind is None or e.kind == kind)]

    def last(self, ep: str) -> Optional[Report]:
        with self._cond:
            return self._last.get(ep)

    def kbd_state(self) -> KbdReport:
        r = self.last("kbd")
        return parse_kbd(r.data if r and len(r.data) == 8 else bytes(8))

    def mouse_buttons(self) -> int:
        r = self.last("mouse")
        return r.data[0] if r and r.data else 0

    def clear(self) -> None:
        """Drop stored reports and events (sequence numbers keep increasing)."""
        with self._cond:
            self._reports = []
            self._events = []

    # -- waiting -----------------------------------------------------------
    def wait_reports(self, n: int, since: int = 0, ep: Optional[str] = None,
                     timeout: float = 5.0) -> List[Report]:
        """Wait until at least n reports (of ep) exist since the mark; return them all.
        Raises HIDTimeout with the count received."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                sel = self._select(since, ep)
                if len(sel) >= n:
                    return sel
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HIDTimeout("waited %.1fs for %d %s reports, got %d"
                                     % (timeout, n, ep or "", len(sel)))
                self._cond.wait(min(left, 0.25))

    def wait_idle(self, idle_ms: float = 100, timeout: float = 10.0, ep: Optional[str] = None) -> bool:
        """Wait until no report (of ep) has arrived for idle_ms, measured from the
        later of the last report and the call itself.  Returns False on timeout."""
        start = time.monotonic_ns()
        deadline = time.monotonic() + timeout
        idle_ns = int(idle_ms * 1e6)
        with self._cond:
            while True:
                ref = max(self._last_t.get(ep, 0), start)
                now = time.monotonic_ns()
                if now - ref >= idle_ns:
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, (ref + idle_ns - now) / 1e9 + 0.0005))

    def wait_until(self, pred: Callable[[], bool], timeout: float = 5.0, poll: float = 0.01) -> bool:
        """Wait until pred() is true (re-evaluated on every log entry and every poll s)."""
        deadline = time.monotonic() + timeout
        while True:
            if pred():
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return bool(pred())
            with self._cond:
                self._cond.wait(min(left, poll))

    def collect(self, since: int, idle_ms: float = 100, timeout: float = 10.0,
                ep: Optional[str] = None, min_count: int = 0) -> List[Report]:
        """Wait for at least min_count reports since the mark and then for idle_ms
        without reports; return all reports since the mark (of ep)."""
        deadline = time.monotonic() + timeout
        if min_count:
            self.wait_reports(min_count, since, ep, timeout)
        self.wait_idle(idle_ms, max(0.0, deadline - time.monotonic()), None)
        return self.reports(since, ep)


# ---------------------------------------------------------------------------
# sysfs isolation checks
# ---------------------------------------------------------------------------

def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_sysfs_devices(vid: int = DEFAULT_VID, pid: int = DEFAULT_PID, root: str = SYSFS_USB) -> List[str]:
    """sysfs names (e.g. "3-2") of USB devices with this VID:PID."""
    out = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        name = os.path.basename(d)
        if ":" in name:
            continue
        v = _read(os.path.join(d, "idVendor"))
        p = _read(os.path.join(d, "idProduct"))
        if v and p and int(v, 16) == vid and int(p, 16) == pid:
            out.append(name)
    return out


def sysfs_name(bus: int, port_numbers: Optional[Sequence[int]]) -> Optional[str]:
    if not port_numbers:
        return None
    return "%d-%s" % (bus, ".".join(str(p) for p in port_numbers))


def interface_status(devname: str, root: str = SYSFS_USB) -> Dict[str, Dict[str, Any]]:
    """{interface_dir: {"number", "class", "driver", "hid_children"}} for one device."""
    out = {}
    for d in sorted(glob.glob(os.path.join(root, devname + ":*"))):
        name = os.path.basename(d)
        link = os.path.join(d, "driver")
        driver = os.path.basename(os.path.realpath(link)) if os.path.lexists(link) else None
        cls = _read(os.path.join(d, "bInterfaceClass"))
        num = _read(os.path.join(d, "bInterfaceNumber"))
        children = [os.path.basename(c) for c in glob.glob(os.path.join(d, "0003:*"))]
        out[name] = {"number": int(num, 16) if num else None, "class": int(cls, 16) if cls else None,
                     "driver": driver, "hid_children": children}
    return out


def isolation_problems(devname: str, root: str = SYSFS_USB, require_claimed: bool = False,
                       interfaces: Sequence[int] = (KBD_INTF, MOUSE_INTF)) -> List[str]:
    """Problems that break HID isolation for this device (empty list = isolated).

    A HID interface is isolated when no kernel driver is bound (driver absent)
    or when it is bound to "usbfs" (claimed through libusb).  With
    require_claimed=True only "usbfs" is accepted.
    """
    st = interface_status(devname, root)
    if not st:
        return ["device %s has no interfaces in sysfs (gone?)" % devname]
    problems = []
    seen = set()
    for name, s in st.items():
        if s["class"] != 3:
            continue
        seen.add(s["number"])
        drv = s["driver"]
        if drv == "usbfs":
            pass
        elif drv is None:
            if require_claimed:
                problems.append("%s is not claimed by the harness (no driver bound)" % name)
        else:
            problems.append("%s is bound to kernel driver %r" % (name, drv))
        if s["hid_children"]:
            problems.append("%s has HID children %s (usbhid was bound)" % (name, s["hid_children"]))
    for n in interfaces:
        if n not in seen:
            problems.append("HID interface %d of %s not found in sysfs" % (n, devname))
    return problems


# ---------------------------------------------------------------------------
# libusb async access (through pyusb's loaded libusb-1.0)
# ---------------------------------------------------------------------------

LIBUSB_TRANSFER_TYPE_INTERRUPT = 3
TRANSFER_STATUS = {0: "completed", 1: "error", 2: "timed_out", 3: "cancelled", 4: "stall",
                   5: "no_device", 6: "overflow"}


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _LibusbAsync:
    """ctypes access to libusb's async transfer API using pyusb's library and context."""

    def __init__(self) -> None:
        import usb.backend.libusb1 as l1
        backend = l1.get_backend()
        if backend is None:
            raise HIDHostError("libusb-1.0 backend not available to pyusb")
        self.l1 = l1
        self.backend = backend
        self.lib = backend.lib
        self.ctx = backend.ctx
        tp = ctypes.POINTER(l1._libusb_transfer)
        self.lib.libusb_alloc_transfer.argtypes = [ctypes.c_int]
        self.lib.libusb_alloc_transfer.restype = tp
        self.lib.libusb_free_transfer.argtypes = [tp]
        self.lib.libusb_free_transfer.restype = None
        self.lib.libusb_submit_transfer.argtypes = [tp]
        self.lib.libusb_submit_transfer.restype = ctypes.c_int
        self.lib.libusb_cancel_transfer.argtypes = [tp]
        self.lib.libusb_cancel_transfer.restype = ctypes.c_int
        self.lib.libusb_handle_events_timeout_completed.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_Timeval), ctypes.POINTER(ctypes.c_int)]
        self.lib.libusb_handle_events_timeout_completed.restype = ctypes.c_int
        self.cb_type = l1._libusb_transfer_cb_fn_p

    def handle_events(self, timeout_s: float = 0.1) -> int:
        tv = _Timeval(int(timeout_s), int((timeout_s % 1) * 1e6))
        return self.lib.libusb_handle_events_timeout_completed(self.ctx, ctypes.byref(tv), None)


class _Slot:
    """One libusb interrupt IN transfer."""

    def __init__(self, la: _LibusbAsync, ep: str, size: int = 64) -> None:
        self.la = la
        self.ep = ep
        self.addr = EP_ADDR[ep]
        self.size = size
        self.buf = (ctypes.c_ubyte * size)()
        self.ptr = la.lib.libusb_alloc_transfer(0)
        if not self.ptr:
            raise HIDHostError("libusb_alloc_transfer failed")
        self.key = ctypes.addressof(self.ptr.contents)
        self.in_flight = False

    def fill(self, handle_value: int, callback: Any, length: Optional[int] = None) -> None:
        """Prepare the transfer.  length must be the endpoint's wMaxPacketSize: an
        interrupt IN transfer only completes on a short packet or a full buffer, so a
        longer buffer would merge consecutive max-size reports into one transfer."""
        t = self.ptr.contents
        t.dev_handle = handle_value
        t.flags = 0
        t.endpoint = self.addr
        t.type = LIBUSB_TRANSFER_TYPE_INTERRUPT
        t.timeout = 0
        t.length = min(self.size, length) if length else self.size
        t.actual_length = 0
        t.buffer = ctypes.cast(self.buf, ctypes.c_void_p)
        t.num_iso_packets = 0
        t.callback = callback

    def free(self) -> None:
        if self.ptr:
            self.la.lib.libusb_free_transfer(self.ptr)
            self.ptr = None


# ---------------------------------------------------------------------------
# HIDHost
# ---------------------------------------------------------------------------

class HIDHost:
    """Fake USB host: claims both ESPKB HID interfaces and logs every report.

    Usage::

        with HIDHost() as hid:            # finds, isolates, claims, starts polling
            m = hid.mark()
            ...make the device type something...
            reps = hid.collect(m)          # wait until idle, get the reports
            assert kbd_text(r for r in reps if r.ep == "kbd") == "hello"

    mode="async" (default) keeps ``transfers`` libusb transfers queued per
    endpoint and processes completions on one event thread, so the log order
    across the two endpoints is the completion order seen by libusb.
    mode="sync" uses one pyusb read loop per endpoint (fallback; cross-endpoint
    order is then only as good as thread scheduling).  ESPKB_HID_MODE overrides.

    The host survives device re-enumeration (reboot): the supervisor thread
    re-finds the device, re-establishes isolation and resumes polling; each
    acquisition increments :attr:`generation` and logs a "connect" event.
    """

    def __init__(self, vid: int = DEFAULT_VID, pid: int = DEFAULT_PID, *, mode: Optional[str] = None,
                 transfers: int = 4, sysfs_root: str = SYSFS_USB, udev_grace_s: float = 3.0,
                 auto_reacquire: bool = True, log: Optional[ReportLog] = None,
                 verbose: bool = False) -> None:
        self.vid, self.pid = vid, pid
        self.mode = (mode or os.environ.get("ESPKB_HID_MODE") or "async").lower()
        if self.mode not in ("async", "sync"):
            raise ValueError("mode must be 'async' or 'sync'")
        self.transfers = max(1, transfers)
        self.sysfs_root = sysfs_root
        self.udev_grace_s = udev_grace_s
        self.auto_reacquire = auto_reacquire
        self.log = log or ReportLog()
        self.verbose = verbose
        self.generation = 0
        self.devname: Optional[str] = None
        self.leds = 0                       # LED mask this host last sent
        self.protocol = {KBD_INTF: PROTOCOL_REPORT, MOUSE_INTF: PROTOCOL_REPORT}
        self.isolation_error: Optional[str] = None
        self._dev: Any = None
        self._dev_lock = threading.RLock()
        self._state_cond = threading.Condition()
        self._connected = False
        self._running = False
        self._lost = threading.Event()
        self._lost_reason = ""
        self._paused = {"kbd": False, "mouse": False}
        self._la: Optional[_LibusbAsync] = None
        self._slots: Dict[str, List[_Slot]] = {"kbd": [], "mouse": []}
        self._slot_by_key: Dict[int, _Slot] = {}
        self._flight = threading.Condition()
        self._c_callback: Any = None
        self._threads: List[threading.Thread] = []
        self._sync_threads: Dict[str, threading.Thread] = {}
        self._sync_idle = {"kbd": threading.Event(), "mouse": threading.Event()}
        self._handle_value = 0
        self.max_packet = {"kbd": 8, "mouse": 8}  # per endpoint, from the descriptors at acquisition

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "HIDHost":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _say(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write("hidhost: %s\n" % msg)

    # -- lifecycle ---------------------------------------------------------
    def open(self, timeout: float = 10.0) -> "HIDHost":
        """Find the device (retrying up to timeout), isolate, claim and start polling."""
        if self._running:
            return self
        import usb.core  # noqa: F401  (fail early if pyusb is missing)
        del usb
        if self.mode == "async":
            self._la = _LibusbAsync()
            self._c_callback = self._la.cb_type(self._on_transfer)
            for ep in ("kbd", "mouse"):
                for _ in range(self.transfers):
                    s = _Slot(self._la, ep)
                    self._slots[ep].append(s)
                    self._slot_by_key[s.key] = s
        self._running = True
        if self.mode == "async":
            t = threading.Thread(target=self._event_loop, name="hidhost-events", daemon=True)
            t.start()
            self._threads.append(t)
        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        while True:
            try:
                self._acquire()
                break
            except IsolationError:
                self.close()
                raise
            except DeviceNotFound as e:
                last_err = e
            except Exception as e:  # transient enumeration errors
                last_err = e
            if time.monotonic() >= deadline:
                self.close()
                if isinstance(last_err, DeviceNotFound):
                    raise DeviceNotFound("USB device %04x:%04x not found within %.0fs"
                                         % (self.vid, self.pid, timeout))
                raise HIDHostError("could not acquire %04x:%04x: %s" % (self.vid, self.pid, last_err))
            time.sleep(0.2)
        t = threading.Thread(target=self._supervise, name="hidhost-supervisor", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def close(self) -> None:
        """Stop polling, release the interfaces and close the device.  Never re-attaches
        the kernel driver."""
        if not self._running and self._dev is None:
            return
        self._running = False
        self._teardown("close")
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2.0)
        self._threads = []
        for ep in ("kbd", "mouse"):
            for s in self._slots[ep]:
                if not s.in_flight:
                    s.free()
            self._slots[ep] = []
        self._slot_by_key = {}

    @property
    def connected(self) -> bool:
        return self._connected

    def wait_connected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._state_cond:
            while not self._connected:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._state_cond.wait(min(left, 0.2))
            return True

    def wait_disconnected(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._state_cond:
            while self._connected:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._state_cond.wait(min(left, 0.2))
            return True

    def wait_generation(self, gen: int, timeout: float = 30.0) -> bool:
        """Wait until the device has been (re-)acquired at least up to generation gen."""
        deadline = time.monotonic() + timeout
        with self._state_cond:
            while not (self._connected and self.generation >= gen):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._state_cond.wait(min(left, 0.2))
            return True

    def _set_connected(self, value: bool) -> None:
        with self._state_cond:
            self._connected = value
            self._state_cond.notify_all()

    # -- acquisition -------------------------------------------------------
    def _acquire(self) -> None:
        import usb.core
        import usb.util
        backend = self._la.backend if self._la else None
        dev = usb.core.find(idVendor=self.vid, idProduct=self.pid, backend=backend)
        if dev is None:
            raise DeviceNotFound("device %04x:%04x not present" % (self.vid, self.pid))
        devname = sysfs_name(dev.bus, getattr(dev, "port_numbers", None))
        if devname is None or not os.path.isdir(os.path.join(self.sysfs_root, devname)):
            found = find_sysfs_devices(self.vid, self.pid, self.sysfs_root)
            if len(found) != 1:
                raise IsolationError("cannot map USB device to sysfs (candidates: %s)" % found)
            devname = found[0]
        # Give the udev rule a moment to unbind usbhid after enumeration.
        deadline = time.monotonic() + self.udev_grace_s
        while isolation_problems(devname, self.sysfs_root) and time.monotonic() < deadline:
            time.sleep(0.05)
        with self._dev_lock:
            dev._ctx.managed_open()
            for intf in (KBD_INTF, MOUSE_INTF):
                try:
                    active = dev.is_kernel_driver_active(intf)
                except (NotImplementedError, usb.core.USBError):
                    active = False
                if active:
                    # Detach only.  NEVER call attach_kernel_driver().
                    dev.detach_kernel_driver(intf)
                    self.log.event("detach", interface=intf)
                    self._say("detached kernel driver from interface %d" % intf)
            try:
                for intf in (KBD_INTF, MOUSE_INTF):
                    usb.util.claim_interface(dev, intf)
            except usb.core.USBError as e:
                usb.util.dispose_resources(dev)
                if getattr(e, "errno", None) == 16:
                    raise IsolationError("HID interfaces of %s are already claimed by another program (another "
                                         "test run or 'hidhost.py monitor'?); refusing to share: %s" % (devname, e))
                raise IsolationError("cannot claim HID interfaces of %s: %s" % (devname, e))
            problems = isolation_problems(devname, self.sysfs_root, require_claimed=True)
            if problems:
                for intf in (KBD_INTF, MOUSE_INTF):
                    try:
                        usb.util.release_interface(dev, intf)
                    except usb.core.USBError:
                        pass
                usb.util.dispose_resources(dev)
                self.isolation_error = "; ".join(problems)
                raise IsolationError("HID isolation not established for %s: %s" % (devname, self.isolation_error))
            self.isolation_error = None
            self._dev = dev
            self.devname = devname
            self._handle_value = dev._ctx.handle.handle.value if self.mode == "async" else 0
            self.max_packet = self._endpoint_max_packet(dev)
            self.log.reset_state()
            self._lost.clear()
            self.protocol = {KBD_INTF: PROTOCOL_REPORT, MOUSE_INTF: PROTOCOL_REPORT}
            # Behave like a normal host after enumeration: idle 0, report protocol, LED sync.
            for intf in (KBD_INTF, MOUSE_INTF):
                for fn in (lambda: self._ctrl_out(HID_SET_IDLE, 0, intf),
                           lambda: self._ctrl_out(HID_SET_PROTOCOL, PROTOCOL_REPORT, intf)):
                    try:
                        fn()
                    except Exception as e:  # a STALL here is legal
                        self.log.event("error", where="setup", interface=intf, error=str(e))
            try:
                self._ctrl_out(HID_SET_REPORT, (REPORT_TYPE_OUTPUT << 8), KBD_INTF, bytes([self.leds]))
            except Exception as e:
                self.log.event("error", where="setup-leds", error=str(e))
            self.generation += 1
            # connected before polling starts: the completion callback only resubmits while connected
            self._set_connected(True)
            self._start_polling()
        self.log.event("connect", gen=self.generation, devname=devname)
        self._say("acquired %s (generation %d, mode %s)" % (devname, self.generation, self.mode))

    def _endpoint_max_packet(self, dev: Any) -> Dict[str, int]:
        """wMaxPacketSize of the two interrupt IN endpoints (from pyusb's cached descriptors)."""
        out = {}
        try:
            cfg = dev.get_active_configuration()
        except Exception:
            cfg = None
        for c in ([cfg] if cfg is not None else list(dev)):
            for intf in c:
                for e in intf:
                    for name, addr in EP_ADDR.items():
                        if e.bEndpointAddress == addr and name not in out:
                            out[name] = (e.wMaxPacketSize & 0x7FF) or 8
        for name in EP_ADDR:
            if name not in out:
                out[name] = 8
                self.log.event("error", where="setup", error="endpoint %s not found in descriptors" % name)
        self.log.event("endpoints", max_packet=dict(out))
        return out

    def _start_polling(self) -> None:
        if self.mode == "async":
            for ep in ("kbd", "mouse"):
                if not self._paused[ep]:
                    self._submit_ep(ep)
        else:
            for ep in ("kbd", "mouse"):
                t = threading.Thread(target=self._sync_reader, args=(ep, self.generation),
                                     name="hidhost-%s" % ep, daemon=True)
                self._sync_threads[ep] = t
                t.start()

    def _teardown(self, reason: str) -> None:
        was = self._dev is not None  # (_mark_lost may already have cleared _connected)
        self._set_connected(False)
        with self._dev_lock:
            if self.mode == "async":
                drained = self._cancel_and_drain(("kbd", "mouse"), 3.0)
            else:
                for t in self._sync_threads.values():
                    t.join(timeout=2.0)
                self._sync_threads = {}
                drained = True
            dev, self._dev = self._dev, None
            if dev is not None:
                import usb.core
                import usb.util
                if drained:
                    for intf in (KBD_INTF, MOUSE_INTF):
                        try:
                            usb.util.release_interface(dev, intf)
                        except (usb.core.USBError, ValueError):
                            pass
                    try:
                        usb.util.dispose_resources(dev)
                    except usb.core.USBError:
                        pass
                else:
                    self.log.event("error", where="teardown", error="transfers did not drain; handle leaked")
        self.log.reset_state()
        if was:
            self.log.event("disconnect", reason=reason, gen=self.generation)
            self._say("released device (%s)" % reason)

    def _mark_lost(self, reason: str) -> None:
        if not self._lost.is_set():
            self._lost_reason = reason
            self._lost.set()
            self._set_connected(False)

    def _supervise(self) -> None:
        while self._running:
            if not self._lost.wait(0.2):
                continue
            if not self._running:
                break
            self._teardown(self._lost_reason or "lost")
            if not self.auto_reacquire:
                self._lost.clear()
                break
            while self._running:
                try:
                    self._acquire()
                    break
                except DeviceNotFound:
                    time.sleep(0.2)
                except IsolationError as e:
                    self.isolation_error = str(e)
                    self.log.event("error", where="reacquire", error=str(e))
                    time.sleep(1.0)
                except Exception as e:
                    self.log.event("error", where="reacquire", error=str(e))
                    time.sleep(0.5)

    # -- async transfers ---------------------------------------------------
    def _event_loop(self) -> None:
        la = self._la
        while self._running or any(s.in_flight for ss in self._slots.values() for s in ss):
            try:
                la.handle_events(0.1)
            except Exception as e:  # pragma: no cover - defensive
                self.log.event("error", where="events", error=str(e))
                time.sleep(0.05)

    def _submit_slot(self, s: _Slot) -> bool:
        if s.in_flight or not s.ptr or not self._handle_value:
            return s.in_flight
        s.fill(self._handle_value, self._c_callback, self.max_packet.get(s.ep))
        s.in_flight = True
        rc = self._la.lib.libusb_submit_transfer(s.ptr)
        if rc != 0:
            s.in_flight = False
            self.log.event("error", where="submit", ep=s.ep, rc=rc)
            if rc == -4:  # LIBUSB_ERROR_NO_DEVICE
                self._mark_lost("no_device")
            return False
        return True

    def _submit_ep(self, ep: str) -> None:
        for s in self._slots[ep]:
            if not self._submit_slot(s):
                break

    def _on_transfer(self, tp: Any) -> None:
        try:
            t_ns = time.monotonic_ns()
            s = self._slot_by_key.get(ctypes.addressof(tp.contents))
            if s is None:
                return
            t = tp.contents
            status, n = t.status, t.actual_length
            if n > 0:
                self.log.append(s.ep, bytes(s.buf[:n]), t_ns, self.generation)
            with self._flight:
                s.in_flight = False
                self._flight.notify_all()
            if status == 5 or (status == 1 and not self._device_present()):
                self._mark_lost(TRANSFER_STATUS.get(status, str(status)))
                return
            if status not in (0, 2, 3):
                self.log.event("error", where="transfer", ep=s.ep, status=TRANSFER_STATUS.get(status, status))
            if status != 3 and self._running and self._connected and not self._paused[s.ep] \
                    and not self._lost.is_set():
                if status in (1, 4, 6):  # error / stall / overflow: back off before retrying
                    time.sleep(0.01)
                self._submit_slot(s)
        except Exception as e:  # never let an exception escape into libusb
            try:
                self.log.event("error", where="callback", error=repr(e))
            except Exception:
                pass

    def _device_present(self) -> bool:
        return bool(self.devname) and os.path.isdir(os.path.join(self.sysfs_root, self.devname))

    def _cancel_and_drain(self, eps: Sequence[str], timeout: float) -> bool:
        """Cancel in-flight transfers until none is left.  Repeats the cancel because a
        completion callback that already passed its "keep polling?" check may resubmit
        once more right after a cancel."""
        deadline = time.monotonic() + timeout
        while True:
            for ep in eps:
                for sl in self._slots[ep]:
                    if sl.in_flight and sl.ptr:
                        self._la.lib.libusb_cancel_transfer(sl.ptr)
            if all(self._wait_drained(ep, 0.1) for ep in eps):
                return True
            if time.monotonic() >= deadline:
                return False

    def _wait_drained(self, ep: Optional[str], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        eps = [ep] if ep else ["kbd", "mouse"]
        with self._flight:
            while any(s.in_flight for e in eps for s in self._slots[e]):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._flight.wait(min(left, 0.05))
        return True

    # -- sync fallback -----------------------------------------------------
    def _sync_reader(self, ep: str, gen: int) -> None:
        import usb.core
        idle = self._sync_idle[ep]
        while self._running and self.generation == gen and not self._lost.is_set():
            dev = self._dev
            if dev is None:
                break
            if self._paused[ep]:
                idle.set()
                time.sleep(0.005)
                continue
            idle.clear()
            try:
                data = dev.read(EP_ADDR[ep], self.max_packet.get(ep, 8), timeout=100)
                t_ns = time.monotonic_ns()
                if len(data):
                    self.log.append(ep, bytes(data), t_ns, gen)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                if e.errno == 19 or not self._device_present():
                    self._mark_lost("no_device")
                    break
                self.log.event("error", where="read", ep=ep, error=str(e))
                time.sleep(0.01)
        idle.set()

    # -- pause / resume ----------------------------------------------------
    def pause(self, ep: str, timeout: float = 2.0) -> None:
        """Stop polling an endpoint ("kbd" or "mouse"), like a host that stops taking reports.
        Reports already completed are still logged."""
        self._paused[ep] = True
        if self.mode == "async":
            with self._dev_lock:
                ok = self._cancel_and_drain((ep,), timeout)
            if not ok:
                raise HIDHostError("could not cancel transfers on %s" % ep)
        else:
            if self._connected and not self._sync_idle[ep].wait(timeout):
                raise HIDHostError("reader for %s did not pause" % ep)
        self.log.event("pause", ep=ep)

    def resume(self, ep: str) -> None:
        self._paused[ep] = False
        if self.mode == "async" and self._connected:
            with self._dev_lock:
                self._submit_ep(ep)
        self.log.event("resume", ep=ep)

    def is_paused(self, ep: str) -> bool:
        return self._paused[ep]

    def resume_all(self) -> None:
        for ep in ("kbd", "mouse"):
            if self._paused[ep]:
                self.resume(ep)

    # -- isolation ---------------------------------------------------------
    def isolation_status(self) -> Dict[str, Any]:
        names = [self.devname] if self.devname else find_sysfs_devices(self.vid, self.pid, self.sysfs_root)
        return {n: interface_status(n, self.sysfs_root) for n in names}

    def assert_isolated(self, wait_s: float = 10.0) -> None:
        """Raise IsolationError unless the device is acquired by this host and both
        HID interfaces are claimed through usbfs (never usbhid).  Waits up to
        wait_s for a re-acquisition in progress (e.g. after a device reboot)."""
        if not self._running:
            raise IsolationError("HID host is not running")
        if not self.wait_connected(wait_s):
            raise IsolationError("HID device not acquired by the harness%s"
                                 % (" (%s)" % self.isolation_error if self.isolation_error else ""))
        problems = isolation_problems(self.devname, self.sysfs_root, require_claimed=True)
        if problems:
            raise IsolationError("HID isolation broken: " + "; ".join(problems))

    # -- control transfers -------------------------------------------------
    def _ctrl_out(self, request: int, value: int, index: int, data: Optional[bytes] = None,
                  timeout: int = 1000) -> int:
        with self._dev_lock:
            if self._dev is None:
                raise HIDHostError("device not connected")
            return self._dev.ctrl_transfer(0x21, request, value, index, data, timeout)

    def _ctrl_in(self, request_type: int, request: int, value: int, index: int, length: int,
                 timeout: int = 1000) -> bytes:
        with self._dev_lock:
            if self._dev is None:
                raise HIDHostError("device not connected")
            return bytes(self._dev.ctrl_transfer(request_type, request, value, index, length, timeout))

    def set_leds(self, mask: int) -> None:
        """SET_REPORT(Output) to the keyboard interface: bit0 Num, bit1 Caps, bit2 Scroll."""
        self._ctrl_out(HID_SET_REPORT, REPORT_TYPE_OUTPUT << 8, KBD_INTF, bytes([mask & 0xFF]))
        self.leds = mask & 0xFF
        self.log.event("leds", mask=self.leds)

    def get_protocol(self, intf: int) -> int:
        """GET_PROTOCOL: 0 = boot, 1 = report."""
        return self._ctrl_in(0xA1, HID_GET_PROTOCOL, 0, intf, 1)[0]

    def set_protocol(self, intf: int, protocol: Union[int, str]) -> None:
        """SET_PROTOCOL on an interface (0/"boot" or 1/"report")."""
        p = {"boot": PROTOCOL_BOOT, "report": PROTOCOL_REPORT}.get(protocol, protocol)  # type: ignore[arg-type]
        self._ctrl_out(HID_SET_PROTOCOL, int(p), intf)
        self.protocol[intf] = int(p)
        self.log.event("protocol", interface=intf, protocol=int(p))

    def set_idle(self, intf: int, duration_4ms: int = 0, report_id: int = 0) -> None:
        self._ctrl_out(HID_SET_IDLE, ((duration_4ms & 0xFF) << 8) | (report_id & 0xFF), intf)

    def get_idle(self, intf: int, report_id: int = 0) -> int:
        return self._ctrl_in(0xA1, HID_GET_IDLE, report_id, intf, 1)[0]

    def get_report(self, intf: int, report_type: int = REPORT_TYPE_INPUT, report_id: int = 0,
                   length: int = 8) -> bytes:
        return self._ctrl_in(0xA1, HID_GET_REPORT, (report_type << 8) | report_id, intf, length)

    def device_descriptor_raw(self) -> bytes:
        return self._ctrl_in(0x80, 0x06, 0x0100, 0, 18)

    def config_descriptor_raw(self) -> bytes:
        head = self._ctrl_in(0x80, 0x06, 0x0200, 0, 9)
        total = head[2] | (head[3] << 8)
        return self._ctrl_in(0x80, 0x06, 0x0200, 0, total)

    def report_descriptor_raw(self, intf: int, length: Optional[int] = None) -> bytes:
        if length is None:
            cfg = parse_config_descriptor(self.config_descriptor_raw())
            length = 512
            for i in cfg["interfaces"]:
                if i["bInterfaceNumber"] == intf and i["hid"]:
                    for d in i["hid"]["descriptors"]:
                        if d["bDescriptorType"] == 0x22:
                            length = d["wDescriptorLength"]
        return self._ctrl_in(0x81, 0x06, 0x2200, intf, length)

    @property
    def speed(self) -> Optional[int]:
        with self._dev_lock:
            return getattr(self._dev, "speed", None) if self._dev is not None else None

    def descriptors(self) -> Dict[str, Any]:
        """Fetch and parse all descriptors; includes the API.md section 9 problem list."""
        dev = parse_device_descriptor(self.device_descriptor_raw())
        cfg_raw = self.config_descriptor_raw()
        cfg = parse_config_descriptor(cfg_raw)
        rds = {intf: self.report_descriptor_raw(intf) for intf in (KBD_INTF, MOUSE_INTF)}
        speed = self.speed
        return {"device": dev, "config": cfg, "config_raw": cfg_raw, "report_descriptors": rds,
                "speed": speed, "problems": verify_usb_descriptors(dev, cfg, speed, rds)}

    # -- log delegation ----------------------------------------------------
    def mark(self) -> int:
        return self.log.mark()

    def reports(self, since: int = 0, ep: Optional[str] = None) -> List[Report]:
        return self.log.reports(since, ep)

    def kbd_reports(self, since: int = 0) -> List[Report]:
        return self.log.reports(since, "kbd")

    def mouse_reports(self, since: int = 0) -> List[Report]:
        return self.log.reports(since, "mouse")

    def wait_reports(self, n: int, since: int = 0, ep: Optional[str] = None,
                     timeout: float = 5.0) -> List[Report]:
        return self.log.wait_reports(n, since, ep, timeout)

    def wait_idle(self, idle_ms: float = 100, timeout: float = 10.0, ep: Optional[str] = None) -> bool:
        return self.log.wait_idle(idle_ms, timeout, ep)

    def wait_until(self, pred: Callable[[], bool], timeout: float = 5.0) -> bool:
        return self.log.wait_until(pred, timeout)

    def collect(self, since: int, idle_ms: float = 100, timeout: float = 10.0,
                ep: Optional[str] = None, min_count: int = 0) -> List[Report]:
        return self.log.collect(since, idle_ms, timeout, ep, min_count)

    def clear(self) -> None:
        self.log.clear()

    def kbd_state(self) -> KbdReport:
        return self.log.kbd_state()

    def mouse_buttons(self) -> int:
        return self.log.mouse_buttons()

    def text_since(self, since: int, caps: bool = False, strict: bool = True) -> str:
        return kbd_text(self.kbd_reports(since), caps=caps, strict=strict)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_leds(s: str) -> int:
    s = s.strip().lower()
    try:
        return int(s, 0)
    except ValueError:
        pass
    mask = 0
    for part in s.replace("+", ",").split(","):
        part = part.strip()
        if not part or part in ("none", "off"):
            continue
        if part not in LED_BITS:
            raise argparse.ArgumentTypeError("unknown LED %r (num, caps, scroll)" % part)
        mask |= LED_BITS[part]
    return mask


def _cmd_check(args: argparse.Namespace) -> int:
    names = find_sysfs_devices(args.vid, args.pid, args.sysfs)
    if not names:
        print("device %04x:%04x not present on USB" % (args.vid, args.pid))
        return 2
    rc = 0
    for n in names:
        st = interface_status(n, args.sysfs)
        for intf, s in st.items():
            print("%-12s class=%s driver=%s%s" % (intf, s["class"], s["driver"] or "(none)",
                                                  " HID-children=%s" % s["hid_children"] if s["hid_children"] else ""))
        problems = isolation_problems(n, args.sysfs)
        if problems and args.fix:
            import usb.core
            dev = usb.core.find(idVendor=args.vid, idProduct=args.pid)
            if dev is not None:
                for intf in (KBD_INTF, MOUSE_INTF):
                    try:
                        if dev.is_kernel_driver_active(intf):
                            dev.detach_kernel_driver(intf)  # never re-attached
                            print("detached kernel driver from interface %d" % intf)
                    except Exception as e:
                        print("detach interface %d failed: %s" % (intf, e))
                import usb.util
                usb.util.dispose_resources(dev)
            problems = isolation_problems(n, args.sysfs)
        if problems:
            rc = 1
            print("%s: NOT ISOLATED: %s" % (n, "; ".join(problems)))
        else:
            print("%s: isolated (no HID interface bound to a kernel input driver)" % n)
    return rc


def _cmd_monitor(args: argparse.Namespace) -> int:
    with HIDHost(args.vid, args.pid, mode=args.mode, verbose=True) as hid:
        hid.assert_isolated()
        print("monitoring %s (generation %d); Ctrl-C to stop" % (hid.devname, hid.generation), flush=True)
        mark = hid.mark()
        t0 = time.monotonic_ns()
        prev_kbd = bytes(8)
        caps = args.caps
        end = time.monotonic() + args.duration if args.duration else None
        try:
            while end is None or time.monotonic() < end:
                time.sleep(0.02)
                new_mark = hid.mark()
                if new_mark == mark:
                    continue
                entries: List[Any] = hid.reports(mark) + hid.log.events(mark)
                entries.sort(key=lambda e: e.seq)
                mark = new_mark
                for e in entries:
                    rel = (e.t_ns - t0) / 1e9
                    if isinstance(e, LogEvent):
                        print("%+10.3fs  ** %s %s" % (rel, e.kind, e.info), flush=True)
                        if e.kind == "connect":
                            prev_kbd = bytes(8)
                        continue
                    line = "%+10.3fs g%d %-5s %-23s %s" % (rel, e.gen, e.ep, e.data.hex(" "), describe(e.ep, e.data))
                    if e.ep == "kbd" and len(e.data) == 8 and not args.raw:
                        evs = kbd_events([e.data], prev_kbd)
                        prev_kbd = e.data
                        txt = ""
                        for ev in evs:
                            if ev.kind == "down" and not is_modifier(ev.usage):
                                ch = key_to_text(ev.usage, ev.mods, caps)
                                txt += repr(ch) if ch is not None else ""
                        line += "   [%s]%s" % (", ".join(str(x) for x in evs), ("  -> " + txt) if txt else "")
                    print(line, flush=True)
        except KeyboardInterrupt:
            pass
    return 0


def _cmd_descriptors(args: argparse.Namespace) -> int:
    with HIDHost(args.vid, args.pid, mode=args.mode) as hid:
        hid.assert_isolated()
        d = hid.descriptors()
    dev = d["device"]
    print("device: %04x:%04x bcdUSB=%04x bMaxPacketSize0=%d bcdDevice=%04x configs=%d speed=%s"
          % (dev["idVendor"], dev["idProduct"], dev["bcdUSB"], dev["bMaxPacketSize0"], dev["bcdDevice"],
             dev["bNumConfigurations"], d["speed"]))
    cfg = d["config"]
    print("config: value=%d interfaces=%d attributes=0x%02x (self_powered=%s remote_wakeup=%s) maxpower=%dmA"
          % (cfg["bConfigurationValue"], cfg["bNumInterfaces"], cfg["bmAttributes"], cfg["self_powered"],
             cfg["remote_wakeup"], cfg["bMaxPower"] * 2))
    for i in cfg["interfaces"]:
        print("  interface %d alt %d class/sub/proto %d/%d/%d hid=%s" % (
            i["bInterfaceNumber"], i["bAlternateSetting"], i["bInterfaceClass"], i["bInterfaceSubClass"],
            i["bInterfaceProtocol"], i["hid"]))
        for e in i["endpoints"]:
            print("    endpoint 0x%02x attr=0x%02x maxpacket=%d interval=%d" % (
                e["bEndpointAddress"], e["bmAttributes"], e["wMaxPacketSize"], e["bInterval"]))
    for intf, raw in d["report_descriptors"].items():
        rd = parse_report_descriptor(raw)
        print("report descriptor interface %d (%d bytes): %s" % (intf, len(raw), raw.hex(" ")))
        print("  input %d bits, output %d bits, report ids %s" % (rd.bits("Input"), rd.bits("Output"), rd.report_ids))
        for f in rd.fields:
            print("    %-7s bit %3d size %d x %d flags=0x%02x page=0x%02x usages=%s min=%s max=%s logical=%d..%d" % (
                f.kind, f.bit_offset, f.size, f.count, f.flags, f.usage_page, [hex(u) for u in f.usages],
                hex(f.usage_min) if f.usage_min is not None else None,
                hex(f.usage_max) if f.usage_max is not None else None, f.logical_min, f.logical_max))
    if d["problems"]:
        print("API.md section 9 check: FAIL")
        for p in d["problems"]:
            print("  - " + p)
        return 1
    print("API.md section 9 check: PASS")
    return 0


def _cmd_leds(args: argparse.Namespace) -> int:
    with HIDHost(args.vid, args.pid, mode=args.mode) as hid:
        hid.assert_isolated()
        hid.set_leds(args.mask)
        print("LED output report sent: 0x%02x" % args.mask)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hidhost.py",
        description="Fake USB host for the ESPKB HID device (303a:4004). Claims both HID "
                    "interfaces over libusb, after verifying that the kernel usbhid driver is not "
                    "bound, so the device's keystrokes never reach the local desktop.",
        epilog="Only one program can claim the device at a time: stop the test suite before "
               "running monitor/descriptors/leds.  ESPKB_HID_MODE=sync selects the fallback reader.")
    ap.add_argument("--vid", type=lambda s: int(s, 16), default=DEFAULT_VID, help="USB vendor id (hex)")
    ap.add_argument("--pid", type=lambda s: int(s, 16), default=DEFAULT_PID, help="USB product id (hex)")
    ap.add_argument("--mode", choices=("async", "sync"), default=None, help="reader implementation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("monitor", help="print every report live, decoded (key events, typed text, mouse deltas)")
    p.add_argument("--caps", action="store_true", help="decode text as if CapsLock were on")
    p.add_argument("--raw", action="store_true", help="do not decode key events")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (default: until Ctrl-C)")
    p.set_defaults(fn=_cmd_monitor)
    p = sub.add_parser("descriptors", help="fetch device/configuration/report descriptors and check them against API.md section 9")
    p.set_defaults(fn=_cmd_descriptors)
    p = sub.add_parser("leds", help="send the keyboard LED output report (SET_REPORT)")
    p.add_argument("mask", type=_parse_leds, help="number (0-7, 1=num 2=caps 4=scroll) or names like caps,num")
    p.set_defaults(fn=_cmd_leds)
    p = sub.add_parser("check", help="isolation check only (sysfs): exit 0 isolated, 1 NOT isolated, 2 device absent")
    p.add_argument("--fix", action="store_true", help="detach an active kernel driver via libusb (never re-attaches)")
    p.add_argument("--sysfs", default=SYSFS_USB, help=argparse.SUPPRESS)
    p.set_defaults(fn=_cmd_check)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except IsolationError as e:
        sys.stderr.write("hidhost: REFUSING: %s\n" % e)
        return 1
    except HIDHostError as e:
        sys.stderr.write("hidhost: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
