"""API.md section 7: serial console (debug UART).  All tests are marked `serial`.

The console's output format is not specified; these tests only rely on the
effects of commands (checked over HTTP) and on the presence of command names
in the help text.
"""
from __future__ import annotations

import os
import time

import pytest

import support

pytestmark = pytest.mark.serial

COMMANDS = ["help", "status", "wifi", "token", "hostname", "keydelay", "reboot", "factory-reset"]


def test_help_lists_all_commands(serial_console):
    r = serial_console.command("help").require_data("help")
    missing = [c for c in COMMANDS if c not in r.text]
    assert not missing, "help output lacks %s:\n%s" % (missing, r.text)


def test_status_mentions_ip(serial_console, dev):
    r = serial_console.command("status").require_data("status")
    ip = dev.status()["wifi"]["ip"]
    assert ip in r.text, "console status does not show the IP %s:\n%s" % (ip, r.text)


def test_keydelay_command(serial_console, dev):
    serial_console.command("keydelay 17").require_data("keydelay")
    support.wait_status(dev, lambda s: True, timeout=1)
    assert dev.get_config()["key_delay_ms"] == 17
    serial_console.command("keydelay 5")
    assert dev.get_config()["key_delay_ms"] == 5


def test_token_set_and_clear(serial_console, dev, raw):
    serial_console.command("token serialTok123").require_data("token")
    dev.known_tokens = list(getattr(dev, "known_tokens", [])) + ["serialTok123"]
    assert raw.get("/api/status", auth=False).status_code == 401
    assert raw.get("/api/status", auth=False, headers={"X-Token": "serialTok123"}).status_code == 200
    serial_console.command("token -")
    assert raw.get("/api/status", auth=False).status_code == 200


def test_hostname_command_saves(serial_console, dev, baseline_config):
    serial_console.command("hostname espkb-serial").require_data("hostname")
    assert dev.get_config()["hostname"] in ("espkb-serial", baseline_config["hostname"])
    serial_console.command("hostname %s" % baseline_config["hostname"])


def test_console_has_no_hid_commands(serial_console, hid):
    r = serial_console.command("help").require_data("help")
    for word in ("type ", "key ", "mouse", "click", "move "):
        assert word not in r.text.lower(), "help mentions %r" % word
    m = hid.mark()
    for cmd in ("type hello", "key a", "click", "move 10 10", "text abc", "press enter"):
        serial_console.command(cmd, max_s=1.5)
    support.assert_nothing_emitted(hid, m, wait_ms=500)


@pytest.mark.reboot
def test_boot_log_via_en_reset(serial_console, dev, hid):
    gen = hid.generation
    r = serial_console.capture_boot_log(seconds=10)
    assert r.got_data, "no boot log received after an EN reset"
    assert hid.wait_generation(gen + 1, 40), "USB did not re-enumerate after the EN reset"
    st = dev.wait_ready(timeout=60)
    assert st["device"]["uptime_ms"] < 60000


@pytest.mark.reboot
def test_reboot_command(serial_console, dev, hid):
    gen = hid.generation
    serial_console.send_line("reboot")
    assert hid.wait_disconnected(10)
    assert hid.wait_generation(gen + 1, 40)
    dev.wait_ready(timeout=60)


@pytest.mark.wifi
@pytest.mark.reboot
def test_provision_wifi_reports_ip(serial_console, dev, hid, bench, device_host):
    import serialcon
    gw = bench.get("ESPKB_HOTSPOT_GW")
    ip = serial_console.provision_wifi(bench["ESPKB_TEST_SSID"], bench.get("ESPKB_TEST_PSK"), timeout=90,
                                       ip_prefix=serialcon.hotspot_prefix(bench), exclude=(gw,) if gw else ())
    if support.is_ip(device_host):
        assert ip == support.host_only(device_host).split(":")[0]
    hid.wait_connected(40)
    dev.wait_ready(timeout=90)


@pytest.mark.reboot
@pytest.mark.slow
def test_factory_reset_and_reprovision(serial_console, dev, hid, bench):
    if os.environ.get("ESPKB_ALLOW_FACTORY_RESET") != "1":
        pytest.skip("set ESPKB_ALLOW_FACTORY_RESET=1 to run the factory-reset test")
    import serialcon
    serial_console.send_line("factory-reset")
    time.sleep(3)
    hid.wait_connected(40)
    gw = bench.get("ESPKB_HOTSPOT_GW")
    serial_console.provision_wifi(bench["ESPKB_TEST_SSID"], bench.get("ESPKB_TEST_PSK"), timeout=90,
                                  ip_prefix=serialcon.hotspot_prefix(bench), exclude=(gw,) if gw else ())
    dev.wait_ready(timeout=90)
    cfg = dev.get_config()
    assert cfg["hostname"] == "espkb" and cfg["key_delay_ms"] == 5 and cfg["token_set"] is False
