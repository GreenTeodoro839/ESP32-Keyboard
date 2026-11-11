#!/usr/bin/env python3
"""Browser checks for web/index.html against web/dev/mock_server.py (dev aid only).

Starts mock devices on 127.0.0.1 high ports, drives the page in headless system Chrome
through Playwright and checks the WebSocket/HTTP traffic recorded by the mock.

  .venv/bin/python web/dev/test_page.py [--headed] [--shots DIR] [--only substr]
      [--host espkb.local]          load the page from a non-secure origin (Chrome maps the name to 127.0.0.1)
      [--browser firefox|webkit]    Playwright's own builds (python -m playwright install firefox webkit)
      [--browser-exe PATH]          custom executable, e.g. a WebKit wrapper that adds missing shared libs

Uses system Chrome (channel="chrome") by default. Screenshots go to --shots (default web/dev/shots,
'' to skip). Exit status 1 if any check fails.
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time

import requests
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
MOCK = HERE / "mock_server.py"
PY = sys.executable

RESULTS = []
PAGE_HOST = "127.0.0.1"   # --host: name the page is loaded from (mapped to 127.0.0.1 inside Chrome)
TEMP_FILES = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"), flush=True)
    return cond


class Mock:
    def __init__(self, port, *extra):
        self.port, self.extra, self.proc = port, list(extra), None
        self.logfile = tempfile.NamedTemporaryFile(prefix="espkb-mock-", suffix=".log", delete=False).name
        TEMP_FILES.extend([self.logfile, self.logfile + ".err"])
        self.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def page(self):
        return f"http://{PAGE_HOST}:{self.port}"

    def start(self):
        self.err = open(self.logfile + ".err", "a")
        self.proc = subprocess.Popen([PY, str(MOCK), "--port", str(self.port), "--log", self.logfile,
                                      "--scan-secs", "0.5", "--reboot-secs", "2", *self.extra],
                                     stdout=subprocess.DEVNULL, stderr=self.err)
        for _ in range(100):
            try:
                requests.get(self.base + "/mock/log?since=999999", timeout=0.5)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("mock did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None

    def recs(self, since=0):
        return requests.get(self.base + "/mock/log", params={"since": since}, timeout=2).json()["records"]

    def mark(self):
        return requests.get(self.base + "/mock/log", params={"since": 10 ** 9}, timeout=2).json()["next"]

    def ws(self, since=0, pings=False):
        return [r for r in self.recs(since) if r["kind"] == "ws" and (pings or not (isinstance(r["msg"], dict) and "ping" in r["msg"]))]

    def msgs(self, since=0):
        return [r["msg"] for r in self.ws(since)]

    def http(self, since=0):
        return [r["msg"] for r in self.recs(since) if r["kind"] == "http"]

    def events(self, since=0):
        return [r["msg"] for r in self.recs(since) if r["kind"] == "event"]

    def state(self, **kw):
        return requests.post(self.base + "/mock/state", json=kw, timeout=2).json()


def wait_for(fn, timeout=5.0, step=0.05):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() > end:
            return v
        time.sleep(step)


FAKE_LOCK = """
window.__lock = (on) => {
  Object.defineProperty(document, 'pointerLockElement', {configurable: true, get: () => on ? document.getElementById('cap') : null});
  document.dispatchEvent(new Event('pointerlockchange'));
};
window.__key = (type, code, opts) => {
  const e = new KeyboardEvent(type, Object.assign({code: code, key: code, bubbles: true, cancelable: true}, opts || {}));
  document.getElementById('cap').dispatchEvent(e); return e.defaultPrevented;
};
"""


def open_page(browser, url, **ctx):
    c = browser.new_context(**ctx)
    pg = c.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.goto(url)
    pg.add_script_tag(content=FAKE_LOCK)
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0", timeout=8000)
    return c, pg


def key_seq(msgs):
    return [m for m in msgs if isinstance(m, dict) and "key" in m]


# ---------------------------------------------------------------------------
def t_status(b, m, a):
    c, pg = open_page(b, m.page)
    print("     (page origin", pg.evaluate("location.origin"), "isSecureContext =", pg.evaluate("isSecureContext"), ")")
    check("status: connected pill", pg.inner_text("#connTxt").startswith("已连接"), pg.inner_text("#connTxt"))
    wait_for(lambda: "ms" in pg.inner_text("#connTxt"), 3)
    check("status: latency shown", "ms" in pg.inner_text("#connTxt"), pg.inner_text("#connTxt"))
    check("status: USB mounted", "已连接" in pg.inner_text("#pUsb"), pg.inner_text("#pUsb"))
    check("status: Num LED on, Caps off", pg.get_attribute("#lNum", "class") == "on" and pg.get_attribute("#lCaps", "class") == "")
    check("status: WiFi rssi", "-48 dBm" in pg.inner_text("#pWifi"), pg.inner_text("#pWifi"))
    check("status: no-token warning visible", pg.is_visible("#bn-auth"))
    m.state(usb={"leds": {"caps": True, "num": False}})
    pg.wait_for_timeout(400)
    check("status: LED push updates", pg.get_attribute("#lCaps", "class") == "on" and pg.get_attribute("#lNum", "class") == "")
    m.state(usb={"leds": {"caps": False, "num": True}})
    check("status: no page errors", not pg.errors, pg.errors)
    c.close()


SAMPLE = [("KeyA", 4), ("KeyM", 16), ("KeyZ", 29), ("Digit1", 30), ("Digit5", 34), ("Digit0", 39), ("Minus", 45), ("Equal", 46),
          ("BracketLeft", 47), ("BracketRight", 48), ("Backslash", 49), ("Semicolon", 51), ("Quote", 52), ("Backquote", 53),
          ("Comma", 54), ("Period", 55), ("Slash", 56), ("Enter", 40), ("Escape", 41), ("Backspace", 42), ("Tab", 43),
          ("Space", 44), ("F1", 58), ("F5", 62), ("F12", 69), ("F13", 104), ("F24", 115), ("PrintScreen", 70),
          ("ScrollLock", 71), ("Pause", 72), ("Insert", 73), ("Home", 74), ("PageUp", 75), ("Delete", 76), ("End", 77),
          ("PageDown", 78), ("ArrowRight", 79), ("ArrowLeft", 80), ("ArrowDown", 81), ("ArrowUp", 82), ("NumLock", 83),
          ("NumpadDivide", 84), ("NumpadMultiply", 85), ("NumpadSubtract", 86), ("NumpadAdd", 87), ("NumpadEnter", 88),
          ("Numpad1", 89), ("Numpad9", 97), ("Numpad0", 98), ("NumpadDecimal", 99), ("IntlBackslash", 100),
          ("ContextMenu", 101), ("IntlRo", 135), ("IntlYen", 137), ("Lang1", 144), ("ControlLeft", 224),
          ("ShiftLeft", 225), ("AltLeft", 226), ("MetaLeft", 227), ("ControlRight", 228), ("ShiftRight", 229),
          ("AltRight", 230), ("MetaRight", 231)]


def t_keyboard(b, m, a):
    c, pg = open_page(b, m.page)
    pg.click("#bKbOnly")
    check("kbd: capture focused via 仅捕获键盘", pg.evaluate("document.activeElement.id") == "cap")
    check("kbd: capture state text", pg.inner_text("#capT") == "键盘捕获中", pg.inner_text("#capT"))
    check("kbd: title shows capture", "捕获中" in pg.title(), pg.title())
    bad, synthetic = [], []
    for code, u in SAMPLE:
        s = m.mark()
        try:
            pg.keyboard.down(code)
            pg.keyboard.up(code)
        except Exception:
            synthetic.append(code)
            mods = {"ctrlKey": code.startswith("Control"), "shiftKey": code.startswith("Shift"),
                    "altKey": code.startswith("Alt"), "metaKey": code.startswith("Meta")}
            pg.evaluate("([c,o]) => __key('keydown', c, o)", [code, mods])
            pg.evaluate("([c]) => __key('keyup', c, {})", [code])
        pg.wait_for_timeout(30)
        got = key_seq(m.msgs(s))
        want = [{"key": u, "action": "down"}, {"key": u, "action": "up"}]
        if got != want:
            bad.append((code, got))
    check(f"kbd: {len(SAMPLE)} codes map to correct usage with down/up pairs", not bad, bad)
    print("     (synthetic KeyboardEvent used for:", ", ".join(synthetic) + ")")
    check("kbd: still focused (Tab/F5 etc. prevented)", pg.evaluate("document.activeElement.id") == "cap")

    s = m.mark()
    pg.evaluate("() => __key('keydown', 'CapsLock', {})")
    pg.evaluate("() => __key('keyup', 'CapsLock', {})")
    pg.wait_for_timeout(50)
    check("kbd: CapsLock sent as one tap (non-mac)", key_seq(m.msgs(s)) == [{"key": 57}], m.msgs(s))

    s = m.mark()
    pg.keyboard.down("KeyB")
    pg.keyboard.down("KeyB")   # Playwright marks this one repeat=true
    pg.evaluate("() => __key('keydown', 'KeyB', {repeat: true})")
    pg.keyboard.up("KeyB")
    pg.wait_for_timeout(50)
    check("kbd: auto-repeat ignored", key_seq(m.msgs(s)) == [{"key": 5, "action": "down"}, {"key": 5, "action": "up"}], m.msgs(s))
    check("kbd: repeat keydown still preventDefault'ed", pg.evaluate("() => __key('keydown', 'KeyQ', {repeat: true})"))

    s = m.mark()
    pg.evaluate("() => __key('keyup', 'PrintScreen', {})")
    pg.wait_for_timeout(50)
    check("kbd: PrintScreen keyup-only -> tap", key_seq(m.msgs(s)) == [{"key": 70}], m.msgs(s))

    # both shifts held, one keyup missed (Windows quirk): the next event without shiftKey releases both
    s = m.mark()
    pg.evaluate("() => { __key('keydown','ShiftLeft',{shiftKey:true}); __key('keydown','ShiftRight',{shiftKey:true}); __key('keyup','ShiftLeft',{shiftKey:false}); }")
    pg.wait_for_timeout(50)
    got = key_seq(m.msgs(s))
    check("kbd: missed modifier keyup reconciled from event state",
          {"key": 225, "action": "up"} in got and {"key": 229, "action": "up"} in got and pg.inner_text("#mine") == "本页按住：无", got)

    s = m.mark()
    pg.evaluate("() => __key('keydown','KeyG',{}) ")
    pg.evaluate("() => __key('keydown','KeyH',{}) ")
    for k in ["KeyI", "KeyJ", "KeyK", "KeyL", "KeyN"]:
        pg.evaluate(f"() => __key('keydown','{k}',{{}})")
    pg.wait_for_timeout(100)
    downs = [x for x in key_seq(m.msgs(s)) if x.get("action") == "down"]
    check("kbd: at most 6 non-modifier keys held (7th not sent)", len(downs) == 6, downs)
    check("kbd: 6-key toast shown", "6" in pg.inner_text("#toasts"))
    pg.evaluate("() => document.getElementById('cap').blur()")
    pg.wait_for_timeout(100)
    ups = [x for x in key_seq(m.msgs(s)) if x.get("action") == "up"]
    check("kbd: capture blur releases all held keys", len(ups) == 6 and pg.inner_text("#mine") == "本页按住：无", ups)

    # release triggers
    for label, js in [("window blur", "window.dispatchEvent(new Event('blur'))"),
                      ("visibility hidden", "Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}); document.dispatchEvent(new Event('visibilitychange')); Object.defineProperty(document,'hidden',{configurable:true,get:()=>false})"),
                      ("pagehide", "window.dispatchEvent(new Event('pagehide'))"),
                      ("click outside", None)]:
        pg.click("#bKbOnly")
        s = m.mark()
        pg.keyboard.down("ShiftLeft")
        pg.keyboard.down("KeyD")
        pg.wait_for_timeout(50)
        if js:
            pg.evaluate("() => {" + js + "}")
        else:
            pg.click("h2 >> text=发送文本")
        pg.wait_for_timeout(100)
        got = key_seq(m.msgs(s))
        ok = got[:2] == [{"key": 225, "action": "down"}, {"key": 7, "action": "down"}] and \
            {"key": 7, "action": "up"} in got[2:] and {"key": 225, "action": "up"} in got[2:]
        check(f"release on {label}: keys released", ok, got)
        pg.keyboard.up("KeyD")
        pg.keyboard.up("ShiftLeft")
        pg.wait_for_timeout(50)
        check(f"release on {label}: no duplicate up afterwards", len(key_seq(m.msgs(s))) == 4, key_seq(m.msgs(s)))
    check("kbd: blur shows idle capture text", pg.inner_text("#capT") == "点击这里开始控制", pg.inner_text("#capT"))
    check("kbd: no page errors", not pg.errors, pg.errors)
    c.close()


def t_mac(b, m, a):
    c = b.new_context()
    c.add_init_script("Object.defineProperty(Navigator.prototype,'platform',{get(){return 'MacIntel'}})")
    pg = c.new_page()
    pg.goto(m.page)
    pg.add_script_tag(content=FAKE_LOCK)
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0")
    pg.click("#bKbOnly")
    s = m.mark()
    # Cmd+C on macOS: keyup for C never arrives while Cmd is held
    pg.evaluate("() => { __key('keydown','MetaLeft',{metaKey:true}); __key('keydown','KeyC',{metaKey:true}); }")
    pg.wait_for_timeout(50)
    pg.evaluate("() => __key('keyup','MetaLeft',{metaKey:false})")
    pg.wait_for_timeout(50)
    got = key_seq(m.msgs(s))
    check("mac: Cmd+key sent as tap, Meta released, nothing left held",
          got == [{"key": 227, "action": "down"}, {"key": 6}, {"key": 227, "action": "up"}] and pg.inner_text("#mine") == "本页按住：无", got)
    s = m.mark()
    pg.evaluate("() => __key('keydown','CapsLock',{})")
    pg.wait_for_timeout(700)
    pg.evaluate("() => __key('keyup','CapsLock',{})")
    pg.wait_for_timeout(50)
    check("mac: CapsLock on (keydown) and off (lone keyup) both tap", key_seq(m.msgs(s)) == [{"key": 57}, {"key": 57}], m.msgs(s))
    c.close()


def t_ping(b, m, a):
    c, pg = open_page(b, m.page)
    pg.wait_for_timeout(500)
    s = m.mark()
    pg.wait_for_timeout(3600)
    pings = [r for r in m.ws(s, pings=True) if "ping" in r["msg"]]
    gaps = [round(y["ms"] - x["ms"]) for x, y in zip(pings, pings[1:])]
    check("ping: idle about once per second", 3 <= len(pings) <= 5 and all(900 <= g <= 1200 for g in gaps), gaps)
    pg.click("#bKbOnly")
    pg.keyboard.down("KeyA")
    t0 = m.mark()
    pg.wait_for_timeout(2500)
    recs = m.ws(t0, pings=True)
    pg.keyboard.up("KeyA")
    ms = [r["ms"] for r in recs]
    gaps = [round(y - x) for x, y in zip(ms, ms[1:])]
    check("ping: while holding, gaps <= 250 ms", gaps and max(gaps) <= 250, gaps)
    check("ping: no keep-alive release by device while holding", not [e for e in m.events(s) if "keepalive_release" in e], m.events(s))
    pg.wait_for_timeout(300)
    s2 = m.mark()
    pg.wait_for_timeout(1500)
    n = len([r for r in m.ws(s2, pings=True) if "ping" in r["msg"]])
    check("ping: back to idle rate after release", n <= 2, n)
    c.close()


def t_mouse(b, m, a):
    c, pg = open_page(b, m.page)
    pg.click("#cap")
    pg.wait_for_timeout(300)
    real = pg.evaluate("document.pointerLockElement && document.pointerLockElement.id")
    print("     (real pointer lock in headless browser:", real, ")")
    if not real:
        pg.evaluate("__lock(true)")
    check("mouse: lock state shown", pg.inner_text("#capT") == "键盘 + 鼠标捕获中", pg.inner_text("#capT"))
    s = m.mark()
    pg.evaluate("""() => { for (let i = 0; i < 5; i++) document.dispatchEvent(new MouseEvent('mousemove', {movementX: 3, movementY: -2, bubbles: true})); }""")
    pg.wait_for_timeout(100)
    mv = [x for x in m.msgs(s) if "move" in x]
    check("mouse: 5 movements in one frame batched into one move", mv == [{"move": [15, -10]}], mv)
    pg.evaluate("() => { const s = document.getElementById('sMouse'); s.value = 2; s.dispatchEvent(new Event('input')); }")
    s = m.mark()
    pg.evaluate("""() => document.dispatchEvent(new MouseEvent('mousemove', {movementX: 4, movementY: 1, bubbles: true}))""")
    pg.wait_for_timeout(100)
    check("mouse: sensitivity 2x applied", [x for x in m.msgs(s) if "move" in x] == [{"move": [8, 2]}], m.msgs(s))
    pg.evaluate("() => { const s = document.getElementById('sMouse'); s.value = 1; s.dispatchEvent(new Event('input')); }")
    s = m.mark()
    pg.evaluate("""() => { const c = document.getElementById('cap'); window.__pd = [];
        for (let i = 0; i < 5; i++) {
          const d = new MouseEvent('mousedown', {button: i, bubbles: true, cancelable: true}); c.dispatchEvent(d); __pd.push(d.defaultPrevented);
          const u = new MouseEvent('mouseup', {button: i, bubbles: true, cancelable: true}); c.dispatchEvent(u); __pd.push(u.defaultPrevented);
        } }""")
    pg.wait_for_timeout(100)
    want = []
    for n in ["left", "middle", "right", "back", "forward"]:
        want += [{"button": n, "action": "down"}, {"button": n, "action": "up"}]
    check("mouse: buttons left/middle/right/back/forward down/up", [x for x in m.msgs(s) if "button" in x] == want, m.msgs(s))
    check("mouse: button events preventDefault'ed", all(pg.evaluate("__pd")))
    cm = pg.evaluate("() => { const e = new MouseEvent('contextmenu', {bubbles: true, cancelable: true}); document.getElementById('cap').dispatchEvent(e); return e.defaultPrevented; }")
    check("mouse: context menu suppressed in capture area", cm)

    def wheel(evts, wait=60):
        s = m.mark()
        for e in evts:
            pg.evaluate("(o) => document.getElementById('cap').dispatchEvent(new WheelEvent('wheel', Object.assign({bubbles: true, cancelable: true}, o)))", e)
            pg.wait_for_timeout(wait)
        pg.wait_for_timeout(80)
        sc = [x["scroll"] for x in m.msgs(s) if "scroll" in x]
        return [sum(v for v, h in sc), sum(h for v, h in sc)], sc
    tot, sc = wheel([{"deltaY": 100}])
    check("wheel: one 100px notch down -> v=-1", tot == [-1, 0], sc)
    tot, sc = wheel([{"deltaY": -300}])
    check("wheel: -300px -> v=+3 (up)", tot == [3, 0], sc)
    tot, sc = wheel([{"deltaY": 3, "deltaMode": 1}])
    check("wheel: 3 lines (deltaMode 1) -> v=-1", tot == [-1, 0], sc)
    tot, sc = wheel([{"deltaY": 1, "deltaMode": 2}])
    check("wheel: 1 page (deltaMode 2) -> v=-3", tot == [-3, 0], sc)
    tot, sc = wheel([{"deltaY": 7}] * 30, wait=10)
    check("wheel: 30 trackpad deltas of 7px accumulate to 2 notches", tot == [-2, 0], sc)
    tot, sc = wheel([{"deltaY": -10}] * 3, wait=10)
    check("wheel: direction change drops leftover fraction", tot == [0, 0], sc)
    tot, sc = wheel([{"deltaX": 200}])
    check("wheel: horizontal deltaX 200 -> h=+2", tot == [0, 2], sc)
    pd = pg.evaluate("() => { const e = new WheelEvent('wheel', {deltaY: 1, bubbles: true, cancelable: true}); document.getElementById('cap').dispatchEvent(e); return e.defaultPrevented; }")
    check("wheel: page scroll prevented while captured", pd)

    s = m.mark()
    pg.evaluate("() => __key('keydown','Escape',{})")
    pg.wait_for_timeout(50)
    check("mouse: Esc while pointer-locked is not forwarded", not key_seq(m.msgs(s)), m.msgs(s))

    s = m.mark()
    pg.evaluate("() => document.getElementById('cap').dispatchEvent(new MouseEvent('mousedown', {button: 0, bubbles: true, cancelable: true}))")
    pg.keyboard.down("ControlLeft")
    pg.wait_for_timeout(50)
    if real:
        pg.evaluate("document.exitPointerLock()")
    else:
        pg.evaluate("__lock(false)")
    pg.wait_for_timeout(100)
    got = m.msgs(s)
    check("mouse: pointer-lock exit releases held button and keys",
          {"button": "left", "action": "up"} in got and {"key": 224, "action": "up"} in got, got)
    check("mouse: after lock exit capture stays in keyboard mode", pg.inner_text("#capT") == "键盘捕获中", pg.inner_text("#capT"))
    pg.keyboard.up("ControlLeft")
    s = m.mark()
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(50)
    check("mouse: Esc forwarded in keyboard-only capture", key_seq(m.msgs(s)) == [{"key": 41, "action": "down"}, {"key": 41, "action": "up"}], m.msgs(s))
    s = m.mark()
    pg.evaluate("""() => document.dispatchEvent(new MouseEvent('mousemove', {movementX: 30, movementY: 30, bubbles: true}))""")
    pg.wait_for_timeout(100)
    check("mouse: no movement forwarded without pointer lock", not m.msgs(s), m.msgs(s))
    check("mouse: no page errors", not pg.errors, pg.errors)
    c.close()


def t_touch(b, m, a):
    c, pg = open_page(b, m.page, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    check("touch: phone defaults to touchpad tab", pg.is_visible("#pad") and not pg.is_visible("#cap"))
    cdp = c.new_cdp_session(pg)
    box = pg.locator("#pad").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def touch(kind, pts):
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(pts)]})

    def drag(starts, dx, dy, steps=12, dt=16):
        touch("touchStart", starts)
        for k in range(1, steps + 1):
            touch("touchMove", [(x + dx * k / steps, y + dy * k / steps) for x, y in starts])
            pg.wait_for_timeout(dt)
        touch("touchEnd", [])
        pg.wait_for_timeout(120)

    sy = pg.evaluate("window.scrollY")
    s = m.mark()
    drag([(cx - 40, cy)], 60, 40)
    mv = [x["move"] for x in m.msgs(s) if "move" in x]
    tot = [sum(v[0] for v in mv), sum(v[1] for v in mv)]
    check("touch: one-finger drag moves pointer right/down", tot[0] > 60 and tot[1] > 40 and not [x for x in m.msgs(s) if "click" in x], (tot, m.msgs(s)[:4]))
    check("touch: page did not scroll during drag", pg.evaluate("window.scrollY") == sy)
    s = m.mark()
    drag([(cx - 40, cy)], 300, 0, steps=4, dt=8)
    mv = [x["move"] for x in m.msgs(s) if "move" in x]
    fast = sum(v[0] for v in mv)
    check("touch: fast swipe accelerated (gain > base 1.5x)", fast > 300 * 1.5 * 1.2, fast)
    s = m.mark()
    touch("touchStart", [(cx, cy)]); pg.wait_for_timeout(60); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: tap -> left click", m.msgs(s) == [{"click": "left"}], m.msgs(s))
    s = m.mark()
    touch("touchStart", [(cx - 30, cy), (cx + 30, cy)]); pg.wait_for_timeout(60); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: two-finger tap -> right click", m.msgs(s) == [{"click": "right"}], m.msgs(s))
    s = m.mark()
    drag([(cx - 30, cy - 60), (cx + 30, cy - 60)], 0, 150)
    sc = [x["scroll"] for x in m.msgs(s) if "scroll" in x]
    v = sum(x[0] for x in sc)
    check("touch: two-finger drag down -> scroll up (natural), ~5 notches, no pointer move",
          4 <= v <= 6 and not [x for x in m.msgs(s) if "move" in x or "click" in x], (v, m.msgs(s)))
    s = m.mark()
    pg.tap("#bDrag")
    pg.wait_for_timeout(100)
    check("touch: drag latch presses left", m.msgs(s) == [{"button": "left", "action": "down"}], m.msgs(s))
    check("touch: drag indicator visible", "on" in pg.get_attribute("#bDrag", "class") and "左键已按住" in pg.inner_text("#padT"))
    t0 = m.mark()
    drag([(cx - 40, cy)], 50, 0)
    pg.wait_for_timeout(1300)
    recs = m.ws(t0, pings=True)
    gaps = [round(y["ms"] - x["ms"]) for x, y in zip(recs, recs[1:])]
    check("touch: keep-alive pings while drag latched", gaps and max(gaps) <= 250 and not [e for e in m.events(t0) if "keepalive_release" in e], gaps)
    s = m.mark()
    touch("touchStart", [(cx, cy)]); pg.wait_for_timeout(60); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: tap while dragging releases left", m.msgs(s) == [{"button": "left", "action": "up"}], m.msgs(s))
    # on-screen mouse button hold
    bb = pg.locator("[data-b=right]").bounding_box()
    s = m.mark()
    touch("touchStart", [(bb["x"] + 10, bb["y"] + 10)]); pg.wait_for_timeout(300); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: on-screen right button down/up", m.msgs(s) == [{"button": "right", "action": "down"}, {"button": "right", "action": "up"}], m.msgs(s))
    # on-screen keys: quick tap -> tap; long press -> hold
    esc = pg.locator("#kMain .k", has_text="Esc")
    esc.scroll_into_view_if_needed()
    eb = esc.bounding_box()
    s = m.mark()
    touch("touchStart", [(eb["x"] + 10, eb["y"] + 10)]); pg.wait_for_timeout(80); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: quick tap on Esc key -> one tap", key_seq(m.msgs(s)) == [{"key": 41}], m.msgs(s))
    s = m.mark()
    touch("touchStart", [(eb["x"] + 10, eb["y"] + 10)]); pg.wait_for_timeout(600); touch("touchEnd", []); pg.wait_for_timeout(120)
    check("touch: long press on Esc key -> down then up", key_seq(m.msgs(s)) == [{"key": 41, "action": "down"}, {"key": 41, "action": "up"}], m.msgs(s))
    s = m.mark()
    touch("touchStart", [(eb["x"] + 10, eb["y"] + 10)]); pg.wait_for_timeout(50)
    touch("touchMove", [(eb["x"] + 10, eb["y"] - 60)]); pg.wait_for_timeout(50)
    touch("touchCancel", []); pg.wait_for_timeout(120)
    check("touch: swipe starting on a key (scroll gesture) presses nothing", not key_seq(m.msgs(s)), m.msgs(s))
    check("touch: no page errors", not pg.errors, pg.errors)
    c.close()


def t_touch_basic(b, m, a):
    """Portable touch check (no CDP): single taps via Playwright's touchscreen."""
    c, pg = open_page(b, m.page, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=a.browser != "firefox")
    if not pg.is_visible("#pad"):
        pg.click("#tPad")
    s = m.mark()
    pg.tap("#pad")
    pg.wait_for_timeout(300)
    check("touch(basic): tap on pad -> left click", m.msgs(s) == [{"click": "left"}], m.msgs(s))
    s = m.mark()
    pg.tap("#bDrag")
    pg.wait_for_timeout(200)
    pg.tap("#pad")
    pg.wait_for_timeout(300)
    check("touch(basic): drag latch down, tap releases", m.msgs(s) == [{"button": "left", "action": "down"}, {"button": "left", "action": "up"}], m.msgs(s))
    s = m.mark()
    pg.tap("#kMain .k >> text=Tab")
    pg.wait_for_timeout(300)
    check("touch(basic): tap on-screen Tab -> tap", key_seq(m.msgs(s)) == [{"key": 43}], m.msgs(s))
    check("touch(basic): no page errors", not pg.errors, pg.errors)
    c.close()


def t_text(b, m, a):
    c, pg = open_page(b, m.page)
    s = m.mark()
    pg.fill("#txt", "abc\nhé “x” ，ok 中")
    pg.click("#bSend")
    msg = pg.inner_text("#txtMsg")
    check("text: unsupported chars listed precisely, nothing sent",
          "U+00E9" in msg and "第 2 行第 2 列" in msg and "U+201C" in msg and "U+4E2D" in msg and "不可见字符" in msg
          and not [x for x in m.msgs(s) if "text" in x], msg)
    pg.click("#txtMsg button >> text=替换为相近")
    check("text: replace fixes quotes/full-width/nbsp, keeps others flagged",
          pg.input_value("#txt") == "abc\nhé \"x\" ,ok 中" and "U+00E9" in pg.inner_text("#txtMsg"), (pg.input_value("#txt"), pg.inner_text("#txtMsg")))
    pg.click("#txtMsg button >> text=删除所有")
    check("text: delete removes remaining unsupported chars", pg.input_value("#txt") == "abc\nh \"x\" ,ok ", pg.input_value("#txt"))
    pg.fill("#txt", "hello world")
    pg.check("#cEnter")
    pg.fill("#iDelay", "3")
    s = m.mark()
    pg.click("#bSend")
    pg.wait_for_function("document.getElementById('txtMsg').textContent.indexOf('已发送') === 0", timeout=5000)
    tm = [x for x in m.msgs(s) if "text" in x]
    check("text: sent as text job with id, Enter appended, delay_ms", len(tm) == 1 and tm[0]["text"] == "hello world\n" and tm[0]["delay_ms"] == 3 and "id" in tm[0], tm)
    check("text: success shown and box cleared", "12 个字符" in pg.inner_text("#txtMsg") and pg.input_value("#txt") == "", pg.inner_text("#txtMsg"))
    pg.uncheck("#cEnter")
    pg.fill("#iDelay", "")
    long = "".join(chr(97 + i % 26) for i in range(400))
    pg.fill("#txt", long)
    s = m.mark()
    pg.click("#bSend")
    pg.wait_for_timeout(300)
    check("text: progress and abort visible during job", pg.is_visible("#prog") and pg.is_visible("#bAbort") and pg.is_disabled("#bSend"))
    pg.wait_for_timeout(1200)
    pg.click("#bAbort")
    pg.wait_for_function("document.getElementById('txtMsg').textContent.indexOf('已中止') === 0", timeout=4000)
    tm = [x for x in m.msgs(s) if "text" in x]
    sent = "".join(x["text"] for x in tm)
    check("text: chunked, one chunk in flight, abort stops further chunks",
          2 <= len(tm) <= 3 and long.startswith(sent) and all(len(x["text"]) <= 80 for x in tm), [len(x["text"]) for x in tm])
    check("text: abort result reports progress", f"{len(sent)} / 400" in pg.inner_text("#txtMsg"), pg.inner_text("#txtMsg"))
    check("text: no delay_ms field when default", all("delay_ms" not in x for x in tm))
    # failed ack: USB unplugged mid-way -> error ack
    m.state(usb={"mounted": False})
    pg.wait_for_timeout(300)
    pg.fill("#txt", "abc")
    pg.click("#bSend")
    pg.wait_for_function("document.getElementById('txtMsg').textContent.indexOf('发送失败') === 0", timeout=4000)
    check("text: failed ack shown in Chinese", "没有识别到 USB" in pg.inner_text("#txtMsg"), pg.inner_text("#txtMsg"))
    m.state(usb={"mounted": True})
    pg.wait_for_timeout(300)
    # Ctrl+Enter in the textarea sends
    pg.fill("#txt", "x")
    s = m.mark()
    pg.focus("#txt")
    pg.keyboard.press("Control+Enter")
    pg.wait_for_timeout(300)
    check("text: Ctrl+Enter sends", [x["text"] for x in m.msgs(s) if "text" in x] == ["x"], m.msgs(s))
    check("text: no page errors", not pg.errors, pg.errors)
    c.close()


def t_keys(b, m, a):
    c, pg = open_page(b, m.page)
    pg.evaluate("localStorage.removeItem('espkb.combos')")
    s = m.mark()
    pg.click("#kMods [data-l='224']")
    pg.wait_for_timeout(1500)
    check("latch: Ctrl latched -> down, indicator on", key_seq(m.msgs(s)) == [{"key": 224, "action": "down"}] and "on" in pg.get_attribute("#kMods [data-l='224']", "class"))
    check("latch: text box warns that text will combine with Ctrl", pg.is_visible("#txtLatch") and "Ctrl" in pg.inner_text("#txtLatch"), pg.inner_text("#txtLatch"))
    check("latch: kept alive (no device keep-alive release)", not [e for e in m.events(s) if "keepalive_release" in e])
    pg.click("#kMain .k >> text=Esc")
    pg.wait_for_timeout(100)
    got = key_seq(m.msgs(s))
    check("latch: next non-modifier key releases latch afterwards",
          got == [{"key": 224, "action": "down"}, {"key": 41, "action": "down"}, {"key": 41, "action": "up"}, {"key": 224, "action": "up"}]
          and "on" not in pg.get_attribute("#kMods [data-l='224']", "class"), got)
    s = m.mark()
    pg.click("#kMods [data-l='225']")
    pg.click("#kMods [data-l='226']")
    pg.click("#kMods [data-l='226']")
    pg.click(".combo button >> text=Alt+Tab")
    pg.wait_for_timeout(100)
    got = key_seq(m.msgs(s))
    check("latch: toggle off + combo with latched Shift",
          got == [{"key": 225, "action": "down"}, {"key": 226, "action": "down"}, {"key": 226, "action": "up"}, {"key": "alt+tab"}, {"key": 225, "action": "up"}], got)
    s = m.mark()
    pg.click("#kMods [data-l='227']")
    pg.click("#kMain .k >> text=Win")
    pg.wait_for_timeout(100)
    got = key_seq(m.msgs(s))
    check("latch: pressing a modifier key does not release the latch (already held: nothing extra sent)",
          got == [{"key": 227, "action": "down"}] and "on" in pg.get_attribute("#kMods [data-l='227']", "class"), got)
    pg.click("#kMods [data-l='227']")
    pg.wait_for_timeout(100)
    check("latch: toggling off sends up", key_seq(m.msgs(s))[-1] == {"key": 227, "action": "up"}, m.msgs(s))
    s = m.mark()
    pg.click("#kMods [data-l='224']")
    pg.fill("#txt", "c")
    pg.click("#bSend")
    pg.wait_for_timeout(600)
    got = [x for x in m.msgs(s) if "key" in x or "text" in x]
    check("latch: text job combines with latch then releases (phone Ctrl+C)",
          [{k: v for k, v in x.items() if k != "id"} for x in got] == [{"key": 224, "action": "down"}, {"text": "c"}, {"key": 224, "action": "up"}], got)
    # default combos
    s = m.mark()
    for lbl in ["Ctrl+Alt+Del", "Alt+F4", "Win+R", "Win+D", "Win+L", "Ctrl+Shift+Esc", "Ctrl+Alt+T"]:
        pg.click(f".combo button >> text='{lbl}'")
    pg.wait_for_timeout(100)
    check("combo: defaults send device key strings", key_seq(m.msgs(s)) == [{"key": k} for k in ["ctrl+alt+del", "alt+f4", "gui+r", "gui+d", "gui+l", "ctrl+shift+esc", "ctrl+alt+t"]], m.msgs(s))
    for bad in ["ctrl+foo", "ctrl++", ""]:
        pg.fill("#iCombo", bad)
        pg.click("#fCombo button")
        check(f"combo: invalid {bad!r} rejected with message", pg.inner_text("#comboErr") != "" and pg.locator(".combo").count() == 8, pg.inner_text("#comboErr"))
    pg.fill("#iCombo", " Ctrl + Alt + F2 ")
    pg.click("#fCombo button")
    check("combo: custom added and normalised", pg.locator(".combo button >> text='Ctrl+Alt+F2'").count() == 1)
    pg.reload()
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0")
    s = m.mark()
    pg.click(".combo button >> text='Ctrl+Alt+F2'")
    pg.wait_for_timeout(100)
    check("combo: custom persisted across reload and sends", key_seq(m.msgs(s)) == [{"key": "ctrl+alt+f2"}], m.msgs(s))
    pg.click(".combo .x")
    pg.reload()
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0")
    check("combo: custom removed persists", pg.locator(".combo").count() == 8)
    pg.click("#kMods [data-l='226']")
    pg.wait_for_timeout(1700)
    requests.post(m.base + "/api/release", timeout=2)   # another client's emergency stop
    pg.wait_for_function("!document.querySelector(\"#kMods [data-l='226']\").classList.contains('on')", timeout=4000)
    check("latch: device-side release (other client's 急停) syncs latch indicator off", pg.inner_text("#mine") == "本页按住：无", pg.inner_text("#mine"))
    s = m.mark()
    pg.click("#bStop")
    pg.wait_for_timeout(100)
    check("急停: sends {release:true}", m.msgs(s) == [{"release": True}], m.msgs(s))
    # function keys F1..F12 via mouse press-and-hold
    s = m.mark()
    for i in (1, 12):
        pg.click(f"#kF .k >> text='F{i}'")
    pg.wait_for_timeout(100)
    check("keys: F1/F12 on-screen", key_seq(m.msgs(s)) == [{"key": 58, "action": "down"}, {"key": 58, "action": "up"}, {"key": 69, "action": "down"}, {"key": 69, "action": "up"}], m.msgs(s))
    check("keys: no page errors", not pg.errors, pg.errors)
    c.close()


def t_usb_errors(b, m, a):
    c, pg = open_page(b, m.page)
    m.state(usb={"suspended": True})
    pg.wait_for_timeout(400)
    check("usb: suspended shown with wake button", "挂起" in pg.inner_text("#pUsb") and pg.is_visible("#bWake"))
    s = m.mark()
    pg.click("#bWake")
    pg.wait_for_timeout(800)
    check("usb: wake calls POST /api/wake and state clears",
          any(h["path"] == "/api/wake" and h["method"] == "POST" for h in m.http(s)) and "已连接" in pg.inner_text("#pUsb") and not pg.is_visible("#bWake"))
    m.state(usb={"mounted": False})
    pg.wait_for_timeout(400)
    check("usb: unmounted shown", "未连接" in pg.inner_text("#pUsb"))
    pg.click("#bKbOnly")
    check("usb: capture area warns USB not connected", pg.is_visible("#capU"))
    for _ in range(3):
        pg.keyboard.press("KeyA")
        pg.wait_for_timeout(1100)
    toasts = pg.locator(".toast").all_inner_texts()
    check("errors: usb_not_ready toast in Chinese, deduplicated", len([t for t in toasts if "USB" in t]) == 1 and "×" in "".join(toasts), toasts)
    m.state(usb={"mounted": True})
    m.state(push_error={"error": "busy", "message": "queue full"})
    pg.wait_for_timeout(300)
    check("errors: busy explained", any("队列已满" in t for t in pg.locator(".toast").all_inner_texts()))
    m.state(push_error={"error": "bad_step", "message": "unknown key", "index": 2})
    pg.wait_for_timeout(300)
    check("errors: bad_step with index", any("第 3 步" in t for t in pg.locator(".toast").all_inner_texts()), pg.locator(".toast").all_inner_texts())
    pg.click("summary")
    check("errors: log lists events", pg.locator("#log li").count() >= 3)
    c.close()


def t_settings(b, m, a):
    c, pg = open_page(b, m.page)
    pg.click("#bSet")
    pg.wait_for_function("document.getElementById('cHost').value === 'espkb'")
    check("settings: config loaded", pg.input_value("#cSsid") == "MyWiFi" and pg.input_value("#cDelay") == "5" and "已设置" in pg.get_attribute("#cWpw", "placeholder"))
    check("settings: device info filled", "1.0.0-mock" in pg.inner_text("#info"))
    pg.click("#bScan")
    pg.wait_for_selector("#scanList li")
    items = pg.locator("#scanList li span").all_inner_texts()
    check("settings: scan list deduped, hidden SSIDs dropped, sorted by signal", items == ["MyWiFi", "Office-5G", "Guest Open", "邻居家的网络"], items)
    pg.click("#scanList li >> text=Guest Open")
    check("settings: picking open AP fills SSID and ticks open", pg.input_value("#cSsid") == "Guest Open" and pg.is_checked("#cOpen"))
    pg.fill("#cHost", "-bad")
    s = m.mark()
    pg.click("#bSave")
    pg.wait_for_timeout(200)
    check("settings: invalid hostname rejected locally", "主机名" in pg.inner_text("#cfgMsg") and not [h for h in m.http(s) if h["method"] == "POST"], pg.inner_text("#cfgMsg"))
    pg.fill("#cHost", "espkb-2")
    pg.click("#bSave")
    pg.wait_for_function("document.getElementById('cfgMsg').textContent.indexOf('已保存') === 0")
    posts = [h for h in m.http(s) if h["method"] == "POST" and h["path"] == "/api/config"]
    check("settings: only changed fields posted", posts and posts[-1]["body"] == {"wifi_ssid": "Guest Open", "wifi_password": "", "hostname": "espkb-2"}, posts)
    check("settings: reboot_required banner", pg.is_visible("#bn-reboot"))
    # changing to a secured network without a password is refused
    pg.fill("#cSsid", "Office-5G")
    pg.click("#bSave")
    pg.wait_for_timeout(200)
    check("settings: new secured SSID without password refused", "密码" in pg.inner_text("#cfgMsg"), pg.inner_text("#cfgMsg"))
    pg.fill("#cSsid", "Guest Open")
    # set a token from settings
    pg.fill("#cTok", "abc12345")
    s = m.mark()
    pg.click("#bSave")
    pg.wait_for_function("document.getElementById('cfgMsg').textContent.indexOf('已保存') === 0")
    stored = pg.evaluate("localStorage.getItem('espkb.token')")
    hs = m.http(s)
    check("settings: token saved, stored locally, later requests use it",
          stored == '"abc12345"' and hs[0]["body"] == {"token": "abc12345"} and hs[-1]["authorization"] == "Bearer abc12345", (stored, hs))
    pg.wait_for_timeout(2200)
    check("settings: no-token warning gone after token set", not pg.is_visible("#bn-auth"))
    pg.check("#cTokClr")
    pg.click("#bSave")
    pg.wait_for_function("document.getElementById('cfgMsg').textContent.indexOf('已保存') === 0")
    check("settings: token cleared", pg.evaluate("localStorage.getItem('espkb.token')") is None)
    if a.shots:
        pg.set_viewport_size({"width": 1280, "height": 900})
        pg.screenshot(path=f"{a.shots}/settings-desktop.png")
    # reboot flow
    pg.once("dialog", lambda d: d.accept())
    s = m.mark()
    check("reboot: restart button offered inside settings", pg.is_visible("#bRb2"))
    pg.click("#bRb2")
    pg.wait_for_timeout(300)
    check("reboot: POST /api/reboot and restarting banner", any(h["path"] == "/api/reboot" for h in m.http(s)) and pg.is_visible("#bn-reboot2"))
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已断开') === 0", timeout=5000)
    check("reboot: page shows disconnected", True)
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0", timeout=10000)
    check("reboot: reconnected automatically, banner cleared", not pg.is_visible("#bn-reboot2"))
    check("settings: no page errors", not [e for e in pg.errors if not any(s in e for s in ("ERR_CONNECTION", "WebSocket", "establish a connection", "Connection refused", "Connection terminated"))], pg.errors)
    c.close()


def t_token(b, a):
    m = Mock(a.port + 1, "--token", "s3cret")
    try:
        c = b.new_context()
        pg = c.new_page()
        pg.goto(m.page)
        pg.wait_for_selector("#ovTok", state="visible", timeout=6000)
        check("token: dialog shown when device requires token", pg.inner_text("#connTxt") == "需要令牌", pg.inner_text("#connTxt"))
        if a.shots:
            pg.screenshot(path=f"{a.shots}/token-dialog.png")
        pg.fill("#tokIn", "wrong")
        pg.click("#fTok button.pri")
        pg.wait_for_function("document.getElementById('tokErr').textContent === '令牌不正确'")
        check("token: wrong token rejected", pg.is_visible("#ovTok"))
        s = m.mark()
        pg.fill("#tokIn", "s3cret")
        pg.click("#fTok button.pri")
        pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0", timeout=6000)
        ev = [e for e in m.events(s) if "ws_open" in e]
        hs = m.http(s)
        check("token: accepted, WS uses ?token=, HTTP uses Bearer",
              not pg.is_visible("#ovTok") and ev and ev[-1]["query"].get("token") == "s3cret" and hs and all(h["authorization"] == "Bearer s3cret" for h in hs), (ev, hs))
        check("token: no insecure warning when auth required", not pg.is_visible("#bn-auth"))
        pg.reload()
        pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0", timeout=6000)
        check("token: stored token reused after reload", not pg.is_visible("#ovTok"))
        m.stop()
        m.extra = ["--token", "changed"]
        m.start()
        pg.wait_for_selector("#ovTok", state="visible", timeout=15000)
        check("token: 401 after token change -> dialog says stored token wrong", "不正确" in pg.inner_text("#tokErr"), pg.inner_text("#tokErr"))
        pg.click("#fTok [data-close]")
        check("token: cancel leaves banner to enter token", pg.is_visible("#bn-auth401"))
        c.close()
    finally:
        m.stop()


def t_reconnect(b, m, a):
    c, pg = open_page(b, m.page)
    m.stop()
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已断开') === 0", timeout=8000)
    check("reconnect: disconnect shown with countdown", "秒后重连" in pg.inner_text("#connTxt"), pg.inner_text("#connTxt"))
    check("reconnect: capture shows not connected", pg.inner_text("#capT") == "未连接设备")
    check("reconnect: send disabled while offline", pg.is_disabled("#bSend"))
    time.sleep(3)
    m.start()
    pg.wait_for_function("document.getElementById('connTxt').textContent.indexOf('已连接') === 0", timeout=12000)
    check("reconnect: reconnected after mock restart", True)
    c.close()


def t_ap(b, a):
    m = Mock(a.port + 2, "--ap-mode")
    try:
        c = b.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
        pg = c.new_page()
        pg.goto(m.page)
        pg.wait_for_selector("#ovSet", state="visible", timeout=6000)
        pg.wait_for_function("document.getElementById('cHost').value === 'espkb'")
        check("ap: first-time setup opens settings with WiFi highlighted",
              pg.is_visible("#setSetup") and pg.get_attribute("#fsWifi", "class") == "hl" and pg.evaluate("document.activeElement.id") == "cSsid")
        if a.shots:
            pg.screenshot(path=f"{a.shots}/ap-setup-phone.png")
        pg.click("#ovSet [data-close]")
        check("ap: setup banner stays visible", pg.is_visible("#bn-setup") and "热点模式" in pg.inner_text("#pWifi"))
        pg.reload()
        pg.wait_for_timeout(1500)
        check("ap: auto-open only once per browser", not pg.is_visible("#ovSet"))
        c.close()
    finally:
        m.stop()


def t_shots(b, m, a):
    if not a.shots:
        return
    for name, vp, scheme, touch in [("desktop-light", (1280, 900), "light", False), ("desktop-dark", (1280, 900), "dark", False),
                                    ("wide-light", (1920, 1080), "light", False),
                                    ("phone-light", (390, 844), "light", True), ("phone-dark", (390, 844), "dark", True)]:
        c, pg = open_page(b, m.page, viewport={"width": vp[0], "height": vp[1]}, color_scheme=scheme, has_touch=touch, is_mobile=touch)
        pg.evaluate("localStorage.removeItem('espkb.mode')")
        pg.click("#kMods [data-l='224']")
        pg.fill("#txt", "echo “hi”")
        pg.click("#bSend")
        if not touch:
            pg.click("#bKbOnly")
        pg.wait_for_timeout(300)
        pg.evaluate("window.scrollTo(0, 0)")
        sw = pg.evaluate("document.documentElement.scrollWidth")
        check(f"layout {name}: no horizontal scroll", sw <= vp[0], sw)
        pg.screenshot(path=f"{a.shots}/{name}.png", full_page=True)
        if touch:
            pg.click("#bSet")
            pg.wait_for_function("document.getElementById('cHost').value !== ''")
            pg.screenshot(path=f"{a.shots}/{name}-settings.png")
        c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", default=str(HERE / "shots"))
    ap.add_argument("--port", type=int, default=18800)
    ap.add_argument("--only", default="")
    ap.add_argument("--host", default="127.0.0.1",
                    help="load the page as http://HOST:port (chrome only, e.g. espkb.local) to test a non-secure origin")
    ap.add_argument("--browser-exe", default=None, help="custom browser executable (e.g. a WebKit wrapper script)")
    ap.add_argument("--browser", choices=["chrome", "firefox", "webkit"], default="chrome",
                    help="firefox/webkit need: .venv/bin/python -m playwright install firefox webkit")
    a = ap.parse_args()
    if a.shots:
        pathlib.Path(a.shots).mkdir(parents=True, exist_ok=True)
    m = Mock(a.port)
    tests = [t_status, t_keyboard, t_mac, t_ping, t_mouse, t_touch if a.browser == "chrome" else t_touch_basic,
             t_text, t_keys, t_usb_errors, t_settings, t_reconnect, t_shots]
    try:
        with sync_playwright() as p:
            global PAGE_HOST
            PAGE_HOST = a.host
            if a.browser == "chrome":
                args = [f"--host-resolver-rules=MAP {a.host} 127.0.0.1"] if a.host != "127.0.0.1" else []
                b = p.chromium.launch(channel="chrome", headless=not a.headed, args=args)
            else:
                b = getattr(p, a.browser).launch(headless=not a.headed, executable_path=a.browser_exe)
            if a.shots and a.browser != "chrome":
                a.shots = str(pathlib.Path(a.shots) / a.browser)
                pathlib.Path(a.shots).mkdir(parents=True, exist_ok=True)
            for t in tests:
                if a.only and a.only not in t.__name__:
                    continue
                print(f"== {t.__name__}", flush=True)
                try:
                    t(b, m, a)
                except Exception as e:
                    check(f"{t.__name__} raised", False, repr(e)[:400])
                if m.proc is None:
                    m.start()
            for t in (t_token, t_ap):
                if a.only and a.only not in t.__name__:
                    continue
                print(f"== {t.__name__}", flush=True)
                try:
                    t(b, a)
                except Exception as e:
                    check(f"{t.__name__} raised", False, repr(e)[:400])
            b.close()
    finally:
        m.stop()
        for f in TEMP_FILES:
            pathlib.Path(f).unlink(missing_ok=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
