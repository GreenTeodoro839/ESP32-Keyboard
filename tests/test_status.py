"""API.md section 4 / 4.1: /api/status fields, LEDs, held keys, protocols, counters; /api, /api/keys, wake, scan."""
from __future__ import annotations

import time

import pytest

import hidhost
import support

SCHEMA = {
    "device": {"name": str, "api": int, "version": str, "uptime_ms": int, "heap_free": int, "heap_min": int,
               "reset_reason": str},
    "usb": {"mounted": bool, "suspended": bool, "remote_wakeup": bool, "leds": dict, "kbd_protocol": str,
            "mouse_protocol": str},
    "hid": {"busy": bool, "queued": int, "keys": list, "buttons": list, "reports_sent": int, "jobs_done": int,
            "jobs_failed": int},
    "wifi": {"sta_connected": bool, "ssid": str, "ip": str, "rssi": int, "ap_active": bool, "ap_ssid": str,
             "ap_ip": str, "hostname": str},
}


def test_status_schema(dev):
    st = dev.status()
    assert st["ok"] is True
    for section, fields in SCHEMA.items():
        assert isinstance(st[section], dict), section
        for k, t in fields.items():
            assert k in st[section], "%s.%s missing" % (section, k)
            assert type(st[section][k]) is t, "%s.%s = %r" % (section, k, st[section][k])
    assert set(st["usb"]["leds"]) == {"num", "caps", "scroll"}
    assert all(type(v) is bool for v in st["usb"]["leds"].values())
    assert st["usb"]["kbd_protocol"] in ("report", "boot") and st["usb"]["mouse_protocol"] in ("report", "boot")
    assert type(st["ws_clients"]) is int and type(st["auth_required"]) is bool


def test_status_identity_and_uptime(dev):
    a = dev.status()
    time.sleep(0.3)
    b = dev.status()
    assert a["device"]["name"] == "espkb" and a["device"]["api"] == 1 and a["device"]["version"]
    assert b["device"]["uptime_ms"] - a["device"]["uptime_ms"] >= 250
    assert 0 < a["device"]["heap_min"] <= a["device"]["heap_free"]
    assert a["device"]["reset_reason"]


def test_status_usb_and_wifi(dev, bench, device_host):
    st = dev.status()
    assert st["usb"]["mounted"] is True and st["usb"]["suspended"] is False
    w = st["wifi"]
    assert w["sta_connected"] is True
    assert w["ssid"] == bench.get("ESPKB_TEST_SSID", w["ssid"])
    if support.is_ip(device_host):
        assert w["ip"] == support.host_only(device_host).split(":")[0]
    assert w["ap_ip"] == "192.168.4.1"
    assert w["hostname"] == dev.get_config()["hostname"]
    assert w["rssi"] < 0
    assert w["ap_ssid"] == dev.get_config()["ap_ssid"]


@pytest.mark.parametrize("mask", [1, 2, 4, 3, 7, 0])
def test_leds_reflect_host_set_report(dev, hid, mask):
    hid.set_leds(mask)
    want = {"num": bool(mask & 1), "caps": bool(mask & 2), "scroll": bool(mask & 4)}
    support.wait_status(dev, lambda s: s["usb"]["leds"] == want, timeout=2)


def test_held_keys_and_buttons_listed(dev, hid):
    dev.key_down("shift", ttl_ms=10000)
    dev.key_down("a", ttl_ms=10000)
    dev.button_down("left", ttl_ms=10000)
    dev.button_down("forward", ttl_ms=10000)
    st = dev.status()["hid"]
    assert sorted(st["keys"]) == [4, 225]
    assert sorted(st["buttons"]) == ["forward", "left"]
    dev.release()
    st = dev.status()["hid"]
    assert st["keys"] == [] and st["buttons"] == []


def test_right_modifiers_listed_as_usages(dev):
    dev.key_down("rctrl+ralt", ttl_ms=10000)
    assert sorted(dev.status()["hid"]["keys"]) == [228, 230]
    dev.key_up("rctrl+ralt")


def test_protocol_fields(dev, hid):
    hid.set_protocol(hidhost.KBD_INTF, "boot")
    hid.set_protocol(hidhost.MOUSE_INTF, "boot")
    support.wait_status(dev, lambda s: s["usb"]["kbd_protocol"] == "boot" and s["usb"]["mouse_protocol"] == "boot", 2)
    hid.set_protocol(hidhost.KBD_INTF, "report")
    support.wait_status(dev, lambda s: s["usb"]["kbd_protocol"] == "report" and s["usb"]["mouse_protocol"] == "boot", 2)


def test_counters(dev, hid):
    a = dev.status()["hid"]
    m = hid.mark()
    dev.type("count")
    dev.click("left")
    support.expect_error(lambda: dev.key("nosuch"), 400, "bad_step")
    reps = hid.collect(m, idle_ms=150)
    b = dev.status()["hid"]
    assert b["jobs_done"] == a["jobs_done"] + 2, (a, b)
    assert b["jobs_failed"] == a["jobs_failed"], "a validation error is not a failed job"
    assert b["reports_sent"] - a["reports_sent"] == len(reps), \
        "reports_sent grew by %d, the host received %d" % (b["reports_sent"] - a["reports_sent"], len(reps))


def test_auth_required_false_by_default(dev):
    assert dev.status()["auth_required"] is False


def test_api_self_description(dev):
    d = dev.info()
    assert d["ok"] is True and d["name"] == "espkb" and d["api"] == 1 and isinstance(d["version"], str)
    eps = {(e["method"], e["path"]) for e in d["endpoints"]}
    for e in d["endpoints"]:
        assert isinstance(e.get("desc"), str) and e["desc"]
    want = {("POST", "/api/type"), ("POST", "/api/key"), ("POST", "/api/mouse"), ("POST", "/api/seq"),
            ("POST", "/api/release"), ("POST", "/api/wake"), ("GET", "/api/status"), ("GET", "/api/keys"),
            ("GET", "/api"), ("GET", "/api/config"), ("POST", "/api/config"), ("GET", "/api/wifi/scan"),
            ("POST", "/api/reboot")}
    assert want <= eps, "missing endpoints: %s" % sorted(want - eps)
    assert isinstance(d["steps"], list) and len(d["steps"]) >= 9
    assert all(isinstance(s, str) and s for s in d["steps"])


def test_version_consistent(dev):
    assert dev.info()["version"] == dev.status()["device"]["version"]


def test_wake_when_not_suspended(dev, hid):
    m = hid.mark()
    assert dev.wake()["ok"] is True
    support.assert_nothing_emitted(hid, m)


def test_wifi_scan(dev, bench):
    t0 = time.monotonic()
    res = dev.wifi_scan(timeout=20)
    assert time.monotonic() - t0 < 15
    assert isinstance(res["aps"], list) and res["aps"]
    for ap in res["aps"]:
        assert type(ap["ssid"]) is str and type(ap["rssi"]) is int and type(ap["channel"]) is int
        assert type(ap["secure"]) is bool
    ssid = bench.get("ESPKB_TEST_SSID")
    if ssid:
        mine = [ap for ap in res["aps"] if ap["ssid"] == ssid]
        assert mine and mine[0]["secure"] is True, "bench hotspot %s not in scan" % ssid
    assert dev.status()["wifi"]["sta_connected"] is True
