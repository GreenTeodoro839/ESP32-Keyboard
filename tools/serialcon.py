#!/usr/bin/env python3
"""serialcon.py - ESPKB serial console helper (debug UART, 115200 baud).

Library and small CLI to
  * send a console command and collect the response (API.md section 7:
    help, status, wifi <ssid> [password], token [<value>|-], hostname <name>,
    keydelay <ms>, reboot, factory-reset),
  * capture the boot log,
  * reset the chip through the EN line WITHOUT entering download mode,
  * provision WiFi with the bench credentials and report the device IP.

Robustness rules (the console's output format is not specified and on this
bench the port can be silent, e.g. while the chip sits in a ROM boot loop):
  * every read has an explicit timeout; nothing waits forever;
  * a response is collected until the line has been quiet for ``quiet_s`` or
    ``max_s`` elapsed, and the result says whether ANY byte arrived
    (``got_data``); helpers raise :class:`NoDataError` with a clear message
    instead of mis-parsing an empty answer;
  * how DTR/RTS are handled on open is configurable (``lines``):
      "preset" (default) - set DTR/RTS to the given levels before opening, so
                pyserial applies them right after the OS open (the bench's
                verified way to avoid an unwanted reset);
      "after"  - open, then drop RTS first and DTR second (never passes the
                RTS=1/DTR=0 "EN low" state);
      "none"   - do not touch the modem lines at all.

The standard ESP32 auto-reset circuit maps RTS=1,DTR=0 to EN low (reset) and
DTR=1,RTS=0 to IO0 low (download mode when EN rises).  :meth:`reset` holds
DTR=0 throughout, so the chip always boots the application.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TESTBED = os.path.join(HERE, "..", "tests", "testbed.env")
DEFAULT_BAUD = 115200
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
IPV4_RE = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\d.])")
EOLS = {"lf": "\n", "cr": "\r", "crlf": "\r\n"}


class SerialConsoleError(RuntimeError):
    pass


class NoDataError(SerialConsoleError):
    """Nothing at all was received from the serial port within the timeout."""


def load_env(path: str) -> Dict[str, str]:
    """Parse a KEY=VALUE file (comments and blank lines ignored)."""
    out: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def default_port(testbed: str = DEFAULT_TESTBED) -> Optional[str]:
    return os.environ.get("ESPKB_SERIAL_PORT") or load_env(testbed).get("ESPKB_SERIAL_PORT")


def clean_text(raw: bytes) -> str:
    """Decode console bytes: UTF-8 with replacement, ANSI colour codes removed, CRLF -> LF."""
    t = raw.decode("utf-8", "replace")
    t = ANSI_RE.sub("", t)
    return t.replace("\r\n", "\n").replace("\r", "\n")


def find_ips(text: str, prefix: Optional[str] = None,
             exclude: tuple = ("0.0.0.0", "192.168.4.1", "255.255.255.0", "255.255.255.255")) -> List[str]:
    """IPv4 addresses in text (in order, unique), optionally only those starting with prefix."""
    seen: List[str] = []
    for m in IPV4_RE.finditer(text):
        ip = m.group(1)
        if ip in exclude or ip.startswith("255.") or ip in seen:
            continue
        if prefix and not ip.startswith(prefix):
            continue
        seen.append(ip)
    return seen


IP_KEYWORD_RE = re.compile(r"(?i)\b(?:sta[_ ]?)?ip(?:v4)?(?:[ _]?addr(?:ess)?)?\b[^0-9\n]{0,12}"
                           r"((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")


def best_ip(text: str, prefix: Optional[str] = None, exclude: tuple = ()) -> Optional[str]:
    """The device's IP from console text: the last address that follows an "ip"
    keyword (e.g. "got ip:", "IP: ", "ip=") and is not excluded; otherwise the
    last plain address (prefix-filtered); None if nothing matches."""
    def ok(ip: str) -> bool:
        return ip not in exclude and (not prefix or ip.startswith(prefix)) and bool(find_ips(ip))
    kw = [m.group(1) for m in IP_KEYWORD_RE.finditer(text) if ok(m.group(1))]
    if kw:
        return kw[-1]
    plain = [ip for ip in find_ips(text, prefix) if ip not in exclude]
    return plain[-1] if plain else None


@dataclass
class ConsoleResult:
    command: Optional[str]
    raw: bytes
    text: str          # cleaned text, echo of the command removed
    got_data: bool     # False = not a single byte arrived
    elapsed_s: float
    timed_out: bool    # max_s reached while data was still arriving

    @property
    def lines(self) -> List[str]:
        return [ln for ln in self.text.split("\n") if ln.strip()]

    def require_data(self, what: str = "response") -> "ConsoleResult":
        if not self.got_data:
            raise NoDataError("no data received on the serial port for %s within %.1f s "
                              "(firmware not running, console on another UART, wrong port/baud, "
                              "or the port is held by another program)" % (what, self.elapsed_s))
        return self

    def __str__(self) -> str:
        return self.text if self.got_data else "<no data received>"


class SerialConsole:
    """The ESPKB debug-UART console.

    port:  serial device (default $ESPKB_SERIAL_PORT or tests/testbed.env).
    dtr/rts: line levels to hold while open (default both False).
    lines: "preset" | "after" | "none" (see the module docstring).
    eol:   line ending sent after each command: "lf" (default), "cr" or "crlf".
    """

    def __init__(self, port: Optional[str] = None, baud: int = DEFAULT_BAUD, dtr: bool = False,
                 rts: bool = False, lines: str = "preset", eol: str = "lf", log=None) -> None:
        self.port = port or default_port()
        if not self.port:
            raise SerialConsoleError("no serial port given (set ESPKB_SERIAL_PORT or tests/testbed.env)")
        if lines not in ("preset", "after", "none"):
            raise ValueError("lines must be preset, after or none")
        self.baud = baud
        self.dtr = dtr
        self.rts = rts
        self.lines_mode = lines
        self.eol = EOLS.get(eol, eol)
        self.log = log  # optional file-like object receiving everything read
        self.ser = None

    # -- open / close ------------------------------------------------------
    def open(self) -> "SerialConsole":
        import serial
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baud
        s.timeout = 0.05
        s.write_timeout = 2.0
        if self.lines_mode == "preset":
            s.dtr = self.dtr
            s.rts = self.rts
        try:
            s.open()
        except (serial.SerialException, OSError) as e:
            raise SerialConsoleError("cannot open %s: %s" % (self.port, e))
        if self.lines_mode == "after":
            s.rts = self.rts
            s.dtr = self.dtr
        self.ser = s
        return self

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def __enter__(self) -> "SerialConsole":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _need(self):
        if self.ser is None:
            raise SerialConsoleError("serial console is not open")
        return self.ser

    # -- reading -----------------------------------------------------------
    def _read_chunk(self) -> bytes:
        s = self._need()
        n = s.in_waiting
        b = s.read(n if n else 1)
        if b and self.log is not None:
            try:
                self.log.write(clean_text(b))
                self.log.flush()
            except Exception:
                pass
        return b

    def drain(self, quiet_s: float = 0.1, max_s: float = 1.0) -> bytes:
        """Discard pending input (until quiet_s of silence or max_s)."""
        return self.read(quiet_s=quiet_s, max_s=max_s, first_byte_timeout=quiet_s).raw

    def read(self, quiet_s: float = 0.3, max_s: float = 5.0, first_byte_timeout: Optional[float] = None,
             until: Optional[str] = None, command: Optional[str] = None) -> ConsoleResult:
        """Collect input until quiet_s of silence after data, max_s overall, or
        the cleaned text contains ``until``.  If nothing arrives within
        first_byte_timeout (default max_s) the result has got_data=False."""
        if first_byte_timeout is None:
            first_byte_timeout = max_s
        start = time.monotonic()
        buf = bytearray()
        last = None
        timed_out = False
        while True:
            now = time.monotonic()
            if now - start >= max_s:
                timed_out = bool(buf)
                break
            if not buf and now - start >= first_byte_timeout:
                break
            if buf and last is not None and now - last >= quiet_s:
                break
            b = self._read_chunk()
            if b:
                buf += b
                last = time.monotonic()
                if until is not None and until in clean_text(bytes(buf)):
                    break
        text = clean_text(bytes(buf))
        if command is not None:
            text = self._strip_echo(text, command)
        return ConsoleResult(command, bytes(buf), text, bool(buf), time.monotonic() - start, timed_out)

    @staticmethod
    def _strip_echo(text: str, command: str) -> str:
        lines = text.split("\n")
        for i, ln in enumerate(lines[:3]):
            if command.strip() and ln.strip().endswith(command.strip()) \
                    and len(ln.strip()) <= len(command.strip()) + 24:
                return "\n".join(lines[i + 1:]).strip("\n")
        return text.strip("\n")

    # -- commands ----------------------------------------------------------
    def send_line(self, line: str) -> None:
        s = self._need()
        s.write((line + self.eol).encode("utf-8"))
        s.flush()

    def command(self, cmd: str, quiet_s: float = 0.4, max_s: float = 5.0,
                first_byte_timeout: float = 2.0, drain_first: bool = True) -> ConsoleResult:
        """Send one console command and collect its response (echo removed).
        Check ``result.got_data`` or call ``result.require_data()``."""
        if drain_first:
            self.drain()
        self.send_line(cmd)
        return self.read(quiet_s=quiet_s, max_s=max_s, first_byte_timeout=first_byte_timeout, command=cmd)

    def help(self) -> ConsoleResult:
        return self.command("help")

    def status(self) -> ConsoleResult:
        return self.command("status")

    def set_token(self, token: Optional[str]) -> ConsoleResult:
        """token <value>, or token - to clear (token=None or "")."""
        return self.command("token %s" % (token if token else "-"))

    def set_keydelay(self, ms: int) -> ConsoleResult:
        return self.command("keydelay %d" % ms)

    def set_hostname(self, name: str) -> ConsoleResult:
        return self.command("hostname %s" % name)

    # -- reset / boot ------------------------------------------------------
    def reset(self, hold_s: float = 0.2) -> None:
        """Pulse EN low via RTS while DTR stays low (IO0 high): normal application boot."""
        s = self._need()
        s.dtr = False
        s.rts = True
        time.sleep(hold_s)
        s.rts = False
        s.dtr = False

    def capture_boot_log(self, seconds: float = 8.0, reset: bool = True, quiet_s: float = 1.5,
                         first_byte_timeout: float = 3.0) -> ConsoleResult:
        """Reset through EN (unless reset=False) and capture output until quiet_s of
        silence or ``seconds`` overall.  got_data=False means the port stayed silent."""
        self.drain(max_s=0.5)
        if reset:
            self.reset()
        return self.read(quiet_s=quiet_s, max_s=seconds, first_byte_timeout=first_byte_timeout)

    # -- provisioning ------------------------------------------------------
    def wait_for_ip(self, prefix: Optional[str] = None, timeout: float = 60.0,
                    poll_status_s: float = 5.0, seen_text: str = "", exclude: tuple = ()) -> str:
        """Watch the console (and send ``status`` every poll_status_s) until an IPv4
        address (starting with prefix, if given) appears.  Raises NoDataError if
        the port stays completely silent, SerialConsoleError on timeout."""
        deadline = time.monotonic() + timeout
        text = seen_text
        got_any = bool(seen_text)
        next_status = time.monotonic() + poll_status_s
        while time.monotonic() < deadline:
            ip = best_ip(text, prefix, exclude)
            if ip:
                return ip
            r = self.read(quiet_s=0.5, max_s=min(2.0, max(0.1, deadline - time.monotonic())),
                          first_byte_timeout=1.0)
            if r.got_data:
                got_any = True
                text += r.text + "\n"
            if time.monotonic() >= next_status:
                next_status = time.monotonic() + poll_status_s
                st = self.command("status", drain_first=False, max_s=3.0)
                if st.got_data:
                    got_any = True
                    text += st.text + "\n"
        if not got_any:
            raise NoDataError("no data received on %s for %.0f s while waiting for an IP address"
                              % (self.port, timeout))
        raise SerialConsoleError("no IP address%s seen on the console within %.0f s; last output:\n%s"
                                 % (" starting with %s" % prefix if prefix else "", timeout, text[-1500:]))

    def provision_wifi(self, ssid: str, password: Optional[str] = None, timeout: float = 60.0,
                       ip_prefix: Optional[str] = None, exclude: tuple = ()) -> str:
        """Send ``wifi <ssid> [password]`` and return the IP the device reports."""
        if not ssid or any(c.isspace() for c in ssid) or (password and any(c.isspace() for c in password)):
            raise ValueError("the console syntax cannot carry SSIDs/passwords containing whitespace")
        cmd = "wifi %s" % ssid + (" %s" % password if password else "")
        r = self.command(cmd, max_s=5.0, first_byte_timeout=3.0)
        return self.wait_for_ip(ip_prefix, timeout=timeout, seen_text=r.text if r.got_data else "",
                                exclude=exclude)


def hotspot_prefix(env: Dict[str, str]) -> Optional[str]:
    gw = env.get("ESPKB_HOTSPOT_GW")
    if gw and gw.count(".") == 3:
        return gw.rsplit(".", 1)[0] + "."
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="serialcon.py",
        description="ESPKB serial console helper (debug UART). Opening the port does not reset the "
                    "board by default (DTR/RTS are preset low before opening).",
        epilog="exit codes: 0 ok, 1 no data received / no answer / error, 2 usage error.\n"
               "examples:\n  serialcon.py cmd help\n  serialcon.py cmd 'keydelay 10'\n"
               "  serialcon.py boot-log\n  serialcon.py provision            (bench SSID/PSK from tests/testbed.env)\n"
               "  serialcon.py listen --seconds 30",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=None, help="serial port (default $ESPKB_SERIAL_PORT or tests/testbed.env)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--dtr", type=int, choices=(0, 1), default=0, help="DTR level while open (default 0)")
    ap.add_argument("--rts", type=int, choices=(0, 1), default=0, help="RTS level while open (default 0)")
    ap.add_argument("--lines", choices=("preset", "after", "none"), default="preset",
                    help="how DTR/RTS are applied on open (default preset = before opening)")
    ap.add_argument("--eol", choices=sorted(EOLS), default="lf", help="line ending sent after commands")
    ap.add_argument("--timeout", type=float, default=None, help="overall seconds to wait for output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("cmd", help="send one console command and print the response")
    p.add_argument("command", nargs="+")
    p = sub.add_parser("boot-log", help="reset via EN (no download mode) and print the boot log")
    p.add_argument("--no-reset", action="store_true", help="just listen, do not reset")
    p.add_argument("--seconds", type=float, default=8.0)
    sub.add_parser("reset", help="reset via EN without entering download mode")
    p = sub.add_parser("listen", help="print whatever arrives for a while")
    p.add_argument("--seconds", type=float, default=10.0)
    p = sub.add_parser("provision", help="send 'wifi <ssid> <psk>' and report the IP the device gets")
    p.add_argument("--ssid", default=None, help="default: ESPKB_TEST_SSID from tests/testbed.env")
    p.add_argument("--psk", default=None, help="default: ESPKB_TEST_PSK from tests/testbed.env")
    p.add_argument("--write-env", default=None, metavar="PATH", help="also write ESPKB_HOST=<ip> to PATH")
    args = ap.parse_args(argv)
    env = load_env(DEFAULT_TESTBED)
    try:
        con = SerialConsole(args.port, args.baud, bool(args.dtr), bool(args.rts), args.lines, args.eol,
                            log=sys.stdout if args.cmd == "listen" else None)
        with con:
            if args.cmd == "cmd":
                r = con.command(" ".join(args.command), max_s=args.timeout or 5.0)
                if not r.got_data:
                    sys.stderr.write("serialcon: no data received within %.1f s\n" % r.elapsed_s)
                    return 1
                print(r.text)
            elif args.cmd == "boot-log":
                r = con.capture_boot_log(seconds=args.timeout or args.seconds, reset=not args.no_reset)
                if not r.got_data:
                    sys.stderr.write("serialcon: no data received within %.1f s\n" % r.elapsed_s)
                    return 1
                print(r.text)
            elif args.cmd == "reset":
                con.reset()
                print("reset pulse sent (EN via RTS, DTR low)")
            elif args.cmd == "listen":
                r = con.read(quiet_s=1e9, max_s=args.timeout or args.seconds)
                if not r.got_data:
                    sys.stderr.write("serialcon: no data received within %.1f s\n" % r.elapsed_s)
                    return 1
            elif args.cmd == "provision":
                ssid = args.ssid or env.get("ESPKB_TEST_SSID")
                psk = args.psk if args.psk is not None else env.get("ESPKB_TEST_PSK")
                if not ssid:
                    sys.stderr.write("serialcon: no SSID (use --ssid or tests/testbed.env)\n")
                    return 2
                gw = env.get("ESPKB_HOTSPOT_GW")
                ip = con.provision_wifi(ssid, psk, timeout=args.timeout or 60.0, ip_prefix=hotspot_prefix(env),
                                        exclude=(gw,) if gw else ())
                print(ip)
                if args.write_env:
                    with open(args.write_env, "w") as f:
                        f.write("ESPKB_HOST=%s\n" % ip)
    except NoDataError as e:
        sys.stderr.write("serialcon: %s\n" % e)
        return 1
    except (SerialConsoleError, ValueError) as e:
        sys.stderr.write("serialcon: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
