"""The hardware suite must import and collect cleanly, and skip (not error or hang) without a device.

ESPKB_HOST points at a closed local port, so the session probe fails at once and
no test ever reaches the USB or serial fixtures.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

TESTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def run_pytest(*args, timeout=300):
    env = dict(os.environ)
    env["ESPKB_HOST"] = "127.0.0.1:9"
    env.pop("ESPKB_TOKEN", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
                          cwd=TESTS, capture_output=True, text=True, env=env, timeout=timeout)


def test_hardware_suite_collects_without_errors():
    r = run_pytest("--collect-only", "-q", "-m", "", "--ignore=offline", ".")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "error" not in r.stdout.lower().split("collected")[-1], r.stdout[-2000:]
    m = re.search(r"(\d+) tests? collected", r.stdout)
    assert m and int(m.group(1)) > 300, r.stdout[-500:]


def test_hardware_suite_skips_cleanly_without_device():
    t0 = time.monotonic()
    r = run_pytest("-q", "-m", "", "--ignore=offline", "-rs", ".")
    dt = time.monotonic() - t0
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert " passed" not in out and " failed" not in out and " error" not in out, out[-3000:]
    assert re.search(r"\d+ skipped", out), out[-2000:]
    assert "ESPKB device not reachable at 127.0.0.1:9" in out, out[-2000:]
    assert dt < 120, "skipping took %.0fs" % dt
