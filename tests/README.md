# ESPKB functional tests

Run everything from the project root with the project venv (`.venv/bin/python`).

## Running

```bash
.venv/bin/python -m pytest tests/offline        # no hardware: client, decoder/encoder, diff, isolation logic
.venv/bin/python -m pytest tests                # default hardware run (a few minutes) + offline tests
.venv/bin/python -m pytest tests -m slow        # only the long tests
.venv/bin/python -m pytest tests -m "wifi"      # hotspot down/up tests
.venv/bin/python -m pytest tests -m "reboot or serial"
.venv/bin/python -m pytest tests -m ""          # everything
.venv/bin/python -m pytest tests/test_typing.py -k capslock -v
```

The device address comes from `ESPKB_HOST`, falling back to `tests/device.env` (`ESPKB_HOST=<ip>`).
Bench parameters (serial port, USB ids, hotspot) come from `tests/testbed.env`. If the device is
unreachable, every hardware test is skipped with the reason; nothing hangs. If the device has a
token configured, set `ESPKB_TOKEN`: it is cleared for the run and restored afterwards.

| marker | meaning | default run |
|---|---|---|
| `slow` | a single test takes more than ~15 s | excluded |
| `wifi` | takes the test hotspot down and up (`nmcli connection down/up espkb-test`) | excluded |
| `reboot` | reboots or resets the device | excluded |
| `serial` | uses the debug serial console | excluded |
| `hw` | needs the device (added automatically outside `tests/offline`) | included |

Other knobs: `ESPKB_TIMING_TOL_MS` (timing tolerance, default 4), `ESPKB_HID_MODE=sync` (fallback
USB reader), `ESPKB_HID_FIND_TIMEOUT` (seconds, default 15), `ESPKB_ALLOW_FACTORY_RESET=1`
(enables the factory-reset test).

## Safety model

* **HID isolation.** The device's HID port is plugged into this machine's live desktop. A udev rule
  unbinds `usbhid` from `303a:4004`; the harness (`tools/hidhost.py`) then claims both HID interfaces
  over libusb and acts as the USB host. Before anything can emit HID output it verifies in sysfs that
  both interfaces are bound to `usbfs` (claimed by the harness), never `usbhid`. It detaches a kernel
  driver via libusb if one is somehow active and never re-attaches one. If isolation cannot be
  established, the whole run stops (`REFUSING TO RUN ...`). Every POST and WebSocket send goes through
  a guard that re-checks isolation. Requests that could press Power (0x66) or Alt+PrintScreen are
  refused.
* **One USB host at a time.** Only one program can claim the device. Stop `hidhost.py monitor`
  before running the suite, and the other way round.
* **Network.** The only network operation is in `wifi` tests: `nmcli connection down/up espkb-test`.
  The hotspot is always brought back up (try/finally). Nothing else (interfaces, routes, firewall,
  NetworkManager state) is touched, and nothing needs sudo.
* **Clean state.** Before and after every hardware test, an autouse fixture resumes paused
  endpoints, calls `/api/release`, and checks that nothing is held on either side. It drains the
  report log and restores the token, config (hostname, key_delay_ms, WiFi), LEDs and protocols. If
  a test leaves keys pressed on the host side, that test errors.

## Debugging with hidhost.py

```bash
.venv/bin/python tools/hidhost.py check            # isolation only (sysfs): 0 isolated, 1 NOT, 2 absent
.venv/bin/python tools/hidhost.py monitor          # live decoded reports; Ctrl-C to stop
.venv/bin/python tools/hidhost.py monitor --caps   # decode text as if CapsLock were on
.venv/bin/python tools/hidhost.py descriptors      # dump descriptors, check them against API.md section 9
.venv/bin/python tools/hidhost.py leds caps,num    # send the LED output report
```

`monitor` prints one line per report: time, connection generation, endpoint, raw bytes and the
decoded content. Keyboard lines also show key down/up events and the typed character; mouse lines
show buttons, dx/dy, wheel and pan. Re-enumeration (device reboot) shows up as `** disconnect` /
`** connect`. Drive the device from another terminal with `tools/espkb.py`
(for example `espkb.py type hello`).

## Writing tests (fixtures in conftest.py)

`dev` (guarded `espkb.ESPKB` client), `hid` (session `HIDHost`), `raw` (requests client for
header-level tests), `ws_factory`/`ws` (WebSocket clients with a reader thread), `serial_console`,
`set_token`, `wifi_restore`, `baseline_config`. Typical pattern:

```python
def test_example(dev, hid):
    expected = hidhost.ReferenceEncoder().text("Hi!\n")
    _, reports = support.do(hid, lambda: dev.type("Hi!\n"))
    hidhost.assert_reports_equal(expected, reports)        # prints a report-by-report diff on mismatch
```
