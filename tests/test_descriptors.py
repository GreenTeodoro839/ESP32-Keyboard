"""API.md section 9: USB descriptors, interfaces, endpoints, report formats, protocols."""
from __future__ import annotations

import os

import pytest

import hidhost
import support
from hidhost import KBD_INTF, MOUSE_INTF


@pytest.fixture(scope="module")
def descs(hid):
    hid.assert_isolated()
    return hid.descriptors()


def test_vid_pid(descs):
    d = descs["device"]
    assert (d["idVendor"], d["idProduct"]) == (0x303A, 0x4004)
    assert d["bNumConfigurations"] >= 1


def test_full_speed(hid, descs):
    assert descs["speed"] == 2, "libusb speed code %r, expected 2 (full speed)" % descs["speed"]
    speed = open(os.path.join(hidhost.SYSFS_USB, hid.devname, "speed")).read().strip()
    assert speed == "12", "sysfs speed %s Mbit/s, expected 12" % speed


def test_bus_powered_with_remote_wakeup(descs):
    cfg = descs["config"]
    assert not cfg["self_powered"], "bmAttributes 0x%02x says self-powered" % cfg["bmAttributes"]
    assert cfg["remote_wakeup"], "bmAttributes 0x%02x lacks remote wakeup" % cfg["bmAttributes"]
    assert cfg["bMaxPower"] > 0


def test_two_hid_boot_interfaces(descs):
    cfg = descs["config"]
    assert cfg["bNumInterfaces"] == 2
    by_num = {i["bInterfaceNumber"]: i for i in cfg["interfaces"] if i["bAlternateSetting"] == 0}
    assert (by_num[0]["bInterfaceClass"], by_num[0]["bInterfaceSubClass"], by_num[0]["bInterfaceProtocol"]) == (3, 1, 1)
    assert (by_num[1]["bInterfaceClass"], by_num[1]["bInterfaceSubClass"], by_num[1]["bInterfaceProtocol"]) == (3, 1, 2)
    for i in by_num.values():
        assert i["hid"] is not None, "interface %d has no HID descriptor" % i["bInterfaceNumber"]


@pytest.mark.parametrize("intf,ep,minpkt", [(0, 0x81, 8), (1, 0x82, 5)])
def test_interrupt_in_endpoints(descs, intf, ep, minpkt):
    i = [x for x in descs["config"]["interfaces"] if x["bInterfaceNumber"] == intf][0]
    eps = {e["bEndpointAddress"]: e for e in i["endpoints"]}
    assert ep in eps, "interface %d endpoints: %s" % (intf, [hex(a) for a in eps])
    e = eps[ep]
    assert e["bmAttributes"] & 3 == 3, "endpoint 0x%02x is not interrupt" % ep
    assert e["bInterval"] == 1
    assert e["wMaxPacketSize"] & 0x7FF >= minpkt


def test_all_section9_checks(descs):
    assert descs["problems"] == [], "\n".join(descs["problems"])


def test_keyboard_report_descriptor(descs):
    rd = hidhost.parse_report_descriptor(descs["report_descriptors"][KBD_INTF])
    assert hidhost.verify_keyboard_report_descriptor(rd) == []


def test_mouse_report_descriptor(descs):
    rd = hidhost.parse_report_descriptor(descs["report_descriptors"][MOUSE_INTF])
    assert hidhost.verify_mouse_report_descriptor(rd) == []


def test_hid_descriptor_lengths_match_report_descriptors(descs):
    for i in descs["config"]["interfaces"]:
        want = [d["wDescriptorLength"] for d in i["hid"]["descriptors"] if d["bDescriptorType"] == 0x22]
        assert want and want[0] == len(descs["report_descriptors"][i["bInterfaceNumber"]])


def test_observed_report_lengths(dev, hid):
    _, reps = support.do(hid, lambda: dev.seq([{"key": "a"}, {"move": [3, -3]}, {"click": "left"}]))
    assert {len(r.data) for r in support.kbd(reps)} == {8}
    assert {len(r.data) for r in support.mouse(reps)} == {5}
    assert len(support.kbd(reps)) == 2 and len(support.mouse(reps)) == 3


def test_default_protocol_is_report(hid, dev):
    assert hid.get_protocol(KBD_INTF) == hidhost.PROTOCOL_REPORT
    assert hid.get_protocol(MOUSE_INTF) == hidhost.PROTOCOL_REPORT
    st = dev.status()
    assert st["usb"]["kbd_protocol"] == "report" and st["usb"]["mouse_protocol"] == "report"


@pytest.mark.parametrize("intf,field", [(KBD_INTF, "kbd_protocol"), (MOUSE_INTF, "mouse_protocol")])
def test_set_get_protocol_roundtrip(hid, dev, intf, field):
    hid.set_protocol(intf, "boot")
    assert hid.get_protocol(intf) == hidhost.PROTOCOL_BOOT
    support.wait_status(dev, lambda s: s["usb"][field] == "boot", timeout=2)
    hid.set_protocol(intf, "report")
    assert hid.get_protocol(intf) == hidhost.PROTOCOL_REPORT
    support.wait_status(dev, lambda s: s["usb"][field] == "report", timeout=2)


def test_set_idle_accepted_and_no_idle_repeats(hid, dev):
    hid.set_idle(KBD_INTF, 0)
    hid.set_idle(MOUSE_INTF, 0)
    m = hid.mark()
    dev.key_down("shift", ttl_ms=3000)
    hid.wait_reports(1, m, "kbd", timeout=2)
    hid.wait_idle(800, timeout=3)  # with idle 0 a held key must not be re-sent
    assert len(hid.kbd_reports(m)) == 1
    dev.key_up("shift")
