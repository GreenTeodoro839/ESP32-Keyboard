"""API.md section 7: configuration get/set, validation, secrets, reboot_required, persistence."""
from __future__ import annotations

import json
import time

import pytest

import support
from support import expect_error

FIELDS = {"hostname": str, "wifi_ssid": str, "wifi_password_set": bool, "ap_ssid": str, "ap_password_set": bool,
          "token_set": bool, "key_delay_ms": int}


def test_get_config_fields_and_types(dev):
    cfg = dev.get_config()
    assert cfg["ok"] is True
    for k, t in FIELDS.items():
        assert k in cfg, "missing %s" % k
        assert type(cfg[k]) is t, "%s is %r" % (k, cfg[k])
    for secret in ("wifi_password", "ap_password", "token"):
        assert secret not in cfg, "secret field %s returned" % secret
    assert cfg["ap_ssid"].startswith("ESPKB-") and len(cfg["ap_ssid"]) == 10


@pytest.mark.parametrize("value", [0, 1, 1000, 5])
def test_key_delay_ms_valid(dev, value):
    res = dev.set_config(key_delay_ms=value)
    assert res["ok"] is True and res["reboot_required"] is False
    assert dev.get_config()["key_delay_ms"] == value


@pytest.mark.parametrize("value", [-1, 1001, "5", 5.5, None, True, [5]], ids=repr)
def test_key_delay_ms_invalid(dev, value):
    before = dev.get_config()["key_delay_ms"]
    expect_error(lambda: dev.set_config(key_delay_ms=value), 400, "bad_request")
    assert dev.get_config()["key_delay_ms"] == before


@pytest.mark.parametrize("name", ["espkb-test-1", "a", "a" * 32, "0abc", "x1-2-3"])
def test_hostname_valid(dev, baseline_config, name):
    res = dev.set_config(hostname=name)
    assert res["reboot_required"] is (name != baseline_config["hostname"])
    assert dev.get_config()["hostname"] in (name, baseline_config["hostname"])
    assert dev.status()["wifi"]["hostname"] == baseline_config["hostname"], "hostname must apply after reboot"


@pytest.mark.parametrize("name", ["", "-abc", "abc-", "ABC", "a" * 33, "a_b", "a.b", "a b", "café", 5, None],
                         ids=repr)
def test_hostname_invalid(dev, baseline_config, name):
    expect_error(lambda: dev.set_config(hostname=name), 400, "bad_request")
    assert dev.get_config()["hostname"] == baseline_config["hostname"]


def test_wifi_ssid_too_long(dev, baseline_config):
    expect_error(lambda: dev.set_config(wifi_ssid="s" * 33), 400, "bad_request")
    assert dev.get_config()["wifi_ssid"] == baseline_config["wifi_ssid"]


def test_wifi_ssid_and_password_boundaries(dev, bench, wifi_restore):
    """Valid boundary values are accepted (the bench credentials are rewritten at teardown)."""
    assert dev.set_config(wifi_ssid="s" * 32, wifi_password="p" * 63)["reboot_required"] is True
    assert dev.get_config()["wifi_ssid"] in ("s" * 32, bench.get("ESPKB_TEST_SSID"))
    assert dev.set_config(wifi_password="p" * 8)["ok"] is True
    assert dev.set_config(wifi_password="")["ok"] is True
    dev.set_config(wifi_ssid=bench["ESPKB_TEST_SSID"], wifi_password=bench["ESPKB_TEST_PSK"])


@pytest.mark.parametrize("pw", ["p" * 7, "p" * 64, "1"], ids=repr)
def test_wifi_password_invalid(dev, pw):
    before = dev.get_config()
    expect_error(lambda: dev.set_config(wifi_password=pw), 400, "bad_request")
    after = dev.get_config()
    assert after["wifi_password_set"] == before["wifi_password_set"]


@pytest.mark.parametrize("pw", ["p" * 7, "p" * 64, ""], ids=repr)
def test_ap_password_invalid(dev, pw):
    before = dev.get_config()
    expect_error(lambda: dev.set_config(ap_password=pw), 400, "bad_request")
    assert dev.get_config()["ap_password_set"] == before["ap_password_set"]


def test_ap_password_set_and_back_to_default(dev):
    """API.md §10: ap_password_set is true for a custom password, false while the default is in use."""
    try:
        res = dev.set_config(ap_password="custom-ap-pass")
        assert res["ok"] is True and res["reboot_required"] is True
        assert dev.get_config()["ap_password_set"] is True
    finally:
        res = dev.set_config(ap_password="espkb1234")
    assert res["ok"] is True
    assert dev.get_config()["ap_password_set"] is False


def test_validation_failure_saves_nothing(dev, baseline_config):
    expect_error(lambda: dev.set_config(key_delay_ms=7, hostname="BAD_NAME"), 400, "bad_request")
    cfg = dev.get_config()
    assert cfg["key_delay_ms"] == baseline_config["key_delay_ms"]
    assert cfg["hostname"] == baseline_config["hostname"]
    expect_error(lambda: dev.set_config(token="good-token", key_delay_ms=5000), 400, "bad_request")
    assert dev.get_config()["token_set"] is False
    assert dev.status()["auth_required"] is False


@pytest.mark.parametrize("field,value", [("hostname", 123), ("wifi_ssid", 5), ("token", 7), ("ap_password", None),
                                         ("wifi_password", ["x" * 8])], ids=str)
def test_wrong_types_rejected(dev, field, value):
    expect_error(lambda: dev.set_config(**{field: value}), 400, "bad_request")


def test_secrets_never_echoed(dev, raw, bench, set_token, wifi_restore):
    psk = bench["ESPKB_TEST_PSK"]
    tok = "SeCrEtToKeN-%d" % (int(time.time()) % 100000)
    r1 = dev.set_config(wifi_ssid=bench["ESPKB_TEST_SSID"], wifi_password=psk, ap_password="espkb1234")
    set_token(tok)
    texts = [json.dumps(r1), json.dumps(dev.get_config()), json.dumps(dev.status()), json.dumps(dev.info()),
             json.dumps(dev.keys())]
    texts.append(raw.post("/api/config", json_body={"token": tok}).text)
    for t in texts:
        for secret in (psk, tok, "espkb1234"):
            assert secret not in t, "secret %r echoed in %s" % (secret, t[:300])


def test_reboot_required_semantics(dev, baseline_config, set_token):
    assert dev.set_config(key_delay_ms=6)["reboot_required"] is False
    assert set_token("t-%d" % (int(time.time()) % 1000))["reboot_required"] is False
    assert dev.set_config(hostname=baseline_config["hostname"] + "-x")["reboot_required"] is True
    try:
        assert dev.set_config(ap_password="custom-ap-pass")["reboot_required"] is True
    finally:
        dev.set_config(ap_password="espkb1234")
    r = dev.set_config(key_delay_ms=5, hostname=baseline_config["hostname"])
    assert isinstance(r["reboot_required"], bool)


def test_empty_config_object(dev):
    res = dev.set_config()
    assert res["ok"] is True and res["reboot_required"] is False


def test_key_delay_takes_effect_immediately(dev, hid):
    dev.set_config(key_delay_ms=40)
    _, reps = support.do(hid, lambda: dev.type("ab"))
    import hidhost
    assert min(hidhost.gaps_ms(reps)) >= 40 - support.TIMING_TOL_MS


@pytest.mark.reboot
def test_config_persists_across_reboot(dev, hid, baseline_config):
    new_name = "espkb-persist"
    dev.set_config(key_delay_ms=7, hostname=new_name)
    gen = hid.generation
    dev.reboot()
    assert hid.wait_disconnected(10), "USB did not re-enumerate after reboot"
    assert hid.wait_generation(gen + 1, 40), "harness did not re-acquire the device"
    st = dev.wait_ready(timeout=60)
    cfg = dev.get_config()
    assert cfg["key_delay_ms"] == 7 and cfg["hostname"] == new_name
    assert st["wifi"]["hostname"] == new_name
    # restore and reboot again so the effective hostname is the original one
    dev.set_config(key_delay_ms=baseline_config["key_delay_ms"], hostname=baseline_config["hostname"])
    gen = hid.generation
    dev.reboot()
    hid.wait_disconnected(10)
    assert hid.wait_generation(gen + 1, 40)
    assert dev.wait_ready(timeout=60)["wifi"]["hostname"] == baseline_config["hostname"]
