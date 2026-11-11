"""Offline tests of tools/espkb.py (library and CLI) against a stdlib mock device."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

import pytest

import espkb
from mock_device import MockDevice, STATUS

TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools")
ESPKB_PY = os.path.join(TOOLS, "espkb.py")


@pytest.fixture
def mock():
    m = MockDevice().start()
    yield m
    m.stop()


def cli(mock, *args, stdin=None, env=None, host=True, timeout=30):
    e = dict(os.environ)
    for k in ("ESPKB_HOST", "ESPKB_TOKEN"):
        e.pop(k, None)
    e.update(env or {})
    cmd = [sys.executable, ESPKB_PY] + (["--host", mock.host] if host and mock else []) + list(args)
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=e, timeout=timeout)


def only(mock):
    assert len(mock.requests) == 1, mock.requests
    return mock.requests[0]


# ---------------------------------------------------------------------------
# request shape for every command
# ---------------------------------------------------------------------------

POST_CASES = [
    (["type", "hello", "world"], "/api/type", {"text": "hello world"}),
    (["type", "echo hi", "--enter"], "/api/type", {"text": "echo hi\n"}),
    (["type", "x", "--delay-ms", "30"], "/api/type", {"text": "x", "delay_ms": 30}),
    (["type", "-e", r"a\nb\tc\bd\ee\\f\x41"], "/api/type", {"text": "a\nb\tc\bd\x1be\\fA"}),
    (["key", "ctrl+alt+del"], "/api/key", {"key": "ctrl+alt+del"}),
    (["key", "a", "--repeat", "3", "--hold-ms", "50", "--delay-ms", "7"], "/api/key",
     {"key": "a", "repeat": 3, "hold_ms": 50, "delay_ms": 7}),
    (["key", "40"], "/api/key", {"key": 40}),
    (["key", "4"], "/api/key", {"key": "4"}),
    (["key", "0x28"], "/api/key", {"key": "0x28"}),
    (["key", "-"], "/api/key", {"key": "-"}),
    (["down", "shift"], "/api/key", {"key": "shift", "action": "down"}),
    (["down", "ctrl+c", "--ttl-ms", "9000"], "/api/key", {"key": "ctrl+c", "action": "down", "ttl_ms": 9000}),
    (["up", "shift"], "/api/key", {"key": "shift", "action": "up"}),
    (["move", "-10", "20"], "/api/mouse", {"move": [-10, 20]}),
    (["move", "300", "-5", "--step", "10", "--delay-ms", "2"], "/api/mouse", {"move": [300, -5], "step": 10, "delay_ms": 2}),
    (["click"], "/api/mouse", {"click": "left"}),
    (["click", "right", "--count", "2", "--hold-ms", "30", "--interval-ms", "100"], "/api/mouse",
     {"click": "right", "count": 2, "hold_ms": 30, "interval_ms": 100}),
    (["mdown"], "/api/mouse", {"button": "left", "action": "down"}),
    (["mdown", "middle", "--ttl-ms", "1500"], "/api/mouse", {"button": "middle", "action": "down", "ttl_ms": 1500}),
    (["mup", "back"], "/api/mouse", {"button": "back", "action": "up"}),
    (["scroll", "-3"], "/api/mouse", {"scroll": -3}),
    (["scroll", "2", "-1"], "/api/mouse", {"scroll": [2, -1]}),
    (["seq", '[{"key":"gui+r"},{"delay":400},{"text":"notepad\\n"}]'], "/api/seq",
     {"steps": [{"key": "gui+r"}, {"delay": 400}, {"text": "notepad\n"}]}),
    (["seq", '{"steps":[{"key":"a"}]}'], "/api/seq", {"steps": [{"key": "a"}]}),
    (["seq", '{"key":"a"}'], "/api/seq", {"steps": [{"key": "a"}]}),
    (["config", "set", "key_delay_ms=10", "hostname=espkb2", "token="], "/api/config",
     {"key_delay_ms": 10, "hostname": "espkb2", "token": ""}),
]


@pytest.mark.parametrize("args,path,body", POST_CASES, ids=[" ".join(c[0]) for c in POST_CASES])
def test_post_commands(mock, args, path, body):
    r = cli(mock, *args)
    assert r.returncode == 0, r.stderr
    rec = only(mock)
    assert rec.method == "POST" and rec.path == path
    assert rec.json == body
    assert rec.headers["content-type"].startswith("application/json")
    assert rec.query == {}
    assert rec.headers["host"] == mock.host
    assert "authorization" not in rec.headers and "x-token" not in rec.headers
    if path != "/api/config":
        assert r.stdout.startswith("ok"), r.stdout


@pytest.mark.parametrize("args,path", [(["release"], "/api/release"), (["wake"], "/api/wake"),
                                       (["reboot"], "/api/reboot")])
def test_post_without_body(mock, args, path):
    r = cli(mock, *args)
    assert r.returncode == 0, r.stderr
    rec = only(mock)
    assert (rec.method, rec.path, rec.body) == ("POST", path, b"")
    assert rec.headers.get("content-length") == "0"


@pytest.mark.parametrize("args,path", [(["status"], "/api/status"), (["info"], "/api"), (["keys"], "/api/keys"),
                                       (["config", "get"], "/api/config"), (["wifi-scan"], "/api/wifi/scan"),
                                       (["wait-ready"], "/api/status")])
def test_get_commands(mock, args, path):
    r = cli(mock, *args)
    assert r.returncode == 0, r.stderr
    rec = only(mock)
    assert (rec.method, rec.path, rec.body) == ("GET", path, b"")
    assert r.stdout.strip()


def test_status_human_and_json(mock):
    r = cli(mock, "status")
    assert r.returncode == 0
    assert "mounted" in r.stdout and "192.168.1.50" in r.stdout and "[225, 4]" in r.stdout
    r = cli(mock, "status", "--json")  # global option after the command
    assert r.returncode == 0
    assert json.loads(r.stdout) == STATUS


def test_type_from_stdin_and_chomp(mock):
    r = cli(mock, "type", stdin="line1\nline2\n")
    assert r.returncode == 0, r.stderr
    assert mock.last().json == {"text": "line1\nline2\n"}
    r = cli(mock, "type", "--chomp", "-", stdin="abc\r\n")
    assert r.returncode == 0, r.stderr
    assert mock.last().json == {"text": "abc"}


def test_type_plain(mock):
    r = cli(mock, "type", "--plain", "hi there")
    assert r.returncode == 0, r.stderr
    rec = mock.last()
    assert rec.headers["content-type"].startswith("text/plain")
    assert rec.body == b"hi there"


def test_seq_from_file_and_stdin(mock, tmp_path):
    f = tmp_path / "steps.json"
    f.write_text('[{"click":"left"},{"scroll":[1,2]}]')
    assert cli(mock, "seq", str(f)).returncode == 0
    assert mock.last().json == {"steps": [{"click": "left"}, {"scroll": [1, 2]}]}
    assert cli(mock, "seq", stdin='[{"release":true}]').returncode == 0
    assert mock.last().json == {"steps": [{"release": True}]}


def test_seq_invalid_json_is_usage_error(mock):
    r = cli(mock, "seq", "[not json")
    assert r.returncode == 2
    assert "not valid JSON" in r.stderr
    assert mock.requests == []


def test_async_flag(mock):
    r = cli(mock, "--async", "type", "long text")
    assert r.returncode == 0, r.stderr
    assert mock.last().query == {"async": "1"}
    assert r.stdout.startswith("queued")
    r = cli(mock, "key", "a", "--async", "--json")
    assert mock.last().query == {"async": "1"}
    assert json.loads(r.stdout)["queued"] is True


@pytest.mark.parametrize("mode,where", [(None, "authorization"), ("bearer", "authorization"),
                                        ("header", "x-token"), ("query", "query")])
def test_token_forms(mock, mode, where):
    args = ["--token", "s3cr3t"] + (["--token-mode", mode] if mode else []) + ["status"]
    assert cli(mock, *args).returncode == 0
    rec = mock.last()
    if where == "authorization":
        assert rec.headers["authorization"] == "Bearer s3cr3t"
    elif where == "x-token":
        assert rec.headers["x-token"] == "s3cr3t" and "authorization" not in rec.headers
    else:
        assert rec.query == {"token": "s3cr3t"} and "authorization" not in rec.headers


def test_env_host_and_token(mock):
    r = cli(mock, "status", host=False, env={"ESPKB_HOST": mock.host, "ESPKB_TOKEN": "envtok"})
    assert r.returncode == 0, r.stderr
    assert mock.last().headers["authorization"] == "Bearer envtok"


def test_proxy_environment_is_ignored(mock):
    r = cli(mock, "status", env={"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                                 "no_proxy": ""})
    assert r.returncode == 0, r.stderr


def test_config_get_human(mock):
    r = cli(mock, "config", "get")
    assert "key_delay_ms = 5" in r.stdout and "hostname = \"espkb\"" in r.stdout


def test_config_set_reboot_required_message(mock):
    r = cli(mock, "config", "set", "hostname=x")
    assert r.returncode == 0 and "reboot required" in r.stdout


@pytest.mark.parametrize("args", [["config", "set"], ["config", "set", "novalue"],
                                  ["config", "set", "key_delay_ms=abc"], ["config", "get", "x=1"]])
def test_config_usage_errors(mock, args):
    r = cli(mock, *args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert mock.requests == []


# ---------------------------------------------------------------------------
# errors and exit codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,code", [(400, "bad_step"), (400, "bad_request"), (401, "unauthorized"),
                                         (403, "forbidden"), (404, "not_found"), (409, "aborted"),
                                         (413, "too_large"), (503, "busy"), (503, "usb_not_ready"),
                                         (504, "timeout")])
def test_device_errors_exit_1(mock, status, code):
    mock.error("POST", "/api/key", status, code, "explanation here", index=2)
    r = cli(mock, "key", "a")
    assert r.returncode == 1
    assert "%s (HTTP %d): explanation here" % (code, status) in r.stderr
    assert "index=2" in r.stderr
    assert r.stdout == ""
    r = cli(mock, "--json", "key", "a")
    assert r.returncode == 1
    assert json.loads(r.stdout) == {"ok": False, "error": code, "message": "explanation here", "index": 2}


def test_type_bad_step_offset_names_the_character(mock):
    mock.error("POST", "/api/type", 400, "bad_step", "unsupported character", index=0, offset=3)
    r = cli(mock, "type", "abcé")
    assert r.returncode == 1
    assert "offset=3" in r.stderr and "'é'" in r.stderr


def test_ok_false_with_200_is_an_error(mock):
    mock.respond("GET", "/api/status", 200, {"ok": False, "error": "weird", "message": "odd"})
    r = cli(mock, "status")
    assert r.returncode == 1 and "weird" in r.stderr


def test_non_json_answer(mock):
    mock.respond("GET", "/api/status", 500, raw=b"<html>oops</html>")
    r = cli(mock, "status")
    assert r.returncode == 1 and "bad_response" in r.stderr


def test_unreachable_exit_3():
    r = cli(None, "--host", "127.0.0.1:9", "status")
    assert r.returncode == 3
    assert "unreachable" in r.stderr


def test_client_timeout_exit_3(mock):
    mock.respond("GET", "/api/status", 200, dict(STATUS), delay=2.0)
    r = cli(mock, "--timeout", "0.5", "status")
    assert r.returncode == 3
    assert "client_timeout" in r.stderr


def test_usage_errors_exit_2(mock):
    assert cli(mock).returncode == 2
    assert cli(mock, "nosuchcommand").returncode == 2
    assert cli(mock, "move", "1").returncode == 2
    assert cli(mock, "click", "sideways").returncode == 2
    assert mock.requests == []


def test_wait_ready_timeout_exit_4(mock):
    st = json.loads(json.dumps(STATUS))
    st["usb"]["mounted"] = False
    mock.respond("GET", "/api/status", 200, st)
    r = cli(mock, "--timeout", "1.5", "wait-ready")
    assert r.returncode == 4
    assert "not mounted" in r.stderr
    assert len(mock.requests) >= 2
    r = cli(mock, "--timeout", "1", "wait-ready", "--no-usb")
    assert r.returncode == 0


def test_wait_ready_unreachable_exit_4():
    r = cli(None, "--host", "127.0.0.1:9", "--timeout", "1", "wait-ready")
    assert r.returncode == 4


def test_help_texts():
    r = subprocess.run([sys.executable, ESPKB_PY, "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    for word in ("ESPKB_HOST", "ESPKB_TOKEN", "exit codes", "release", "Step reference", "--json", "--async"):
        assert word in r.stdout
    for sub in ("type", "key", "seq", "config", "wait-ready"):
        r = subprocess.run([sys.executable, ESPKB_PY, sub, "--help"], capture_output=True, text=True)
        assert r.returncode == 0 and "--host" in r.stdout


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

def test_library_calls(mock):
    kb = espkb.ESPKB(mock.host, token="")
    assert kb.status()["device"]["name"] == "espkb"
    kb.type("hi", delay_ms=3)
    assert mock.last().json == {"text": "hi", "delay_ms": 3}
    kb.key("a", repeat=2)
    kb.key_down("shift", ttl_ms=100)
    kb.key_up("shift")
    kb.move(1, -2, step=5)
    kb.click("right", count=2)
    kb.button_down("left")
    kb.button_up("left")
    kb.scroll(3)
    kb.scroll(-1, 2)
    kb.seq([{"key": "a"}], wait=False)
    assert mock.last().query == {"async": "1"}
    assert kb.release() == {"ok": True, "aborted": 0}
    kb.wake()
    kb.set_config(key_delay_ms=7)
    assert kb.get_config()["hostname"] == "espkb"
    assert kb.keys()["buttons"][0] == "left"
    assert kb.info()["api"] == 1
    assert kb.wifi_scan()["aps"]
    bodies = [(r.method, r.path, r.json) for r in mock.requests]
    assert ("POST", "/api/mouse", {"scroll": [-1, 2]}) in bodies
    assert ("POST", "/api/key", {"key": "shift", "action": "down", "ttl_ms": 100}) in bodies
    assert ("POST", "/api/config", {"key_delay_ms": 7}) in bodies


def test_library_error_attributes(mock):
    mock.error("POST", "/api/seq", 400, "bad_step", "unknown key", index=1)
    kb = espkb.ESPKB(mock.host, token="")
    with pytest.raises(espkb.ESPKBError) as ei:
        kb.seq([{"key": "a"}, {"key": "nope"}])
    e = ei.value
    assert (e.status, e.code, e.message, e.index, e.offset) == (400, "bad_step", "unknown key", 1, None)
    assert "bad_step (HTTP 400): unknown key [index=1]" == str(e)
    with pytest.raises(espkb.ESPKBConnectionError) as ei:
        espkb.ESPKB("127.0.0.1:9", timeout=1).status()
    assert ei.value.status is None and ei.value.code == "unreachable"


def test_long_text_is_split_into_jobs(mock):
    kb = espkb.ESPKB(mock.host, token="")
    text = ("abc\r\n" * 30000)  # 150000 characters
    res = kb.type(text)
    bodies = [r.json["text"] for r in mock.requests]
    assert len(bodies) >= 3
    assert "".join(bodies) == text
    assert all(len(r.body) < espkb.MAX_BODY for r in mock.requests)
    assert all(not b.startswith("\n") for b in bodies[1:]), "a \\r\\n pair was split"
    assert res["jobs"] == len(bodies)


def test_split_text_keeps_crlf():
    chunks = espkb.split_text("x" * 9 + "\r\n" + "y" * 10, max_body=22)
    assert "".join(chunks) == "x" * 9 + "\r\n" + "y" * 10
    for c in chunks:
        assert not c.startswith("\n")


def test_estimate_ms_and_timeouts():
    assert espkb.estimate_ms([{"delay": 60000}]) == 60000
    assert espkb.estimate_ms([{"text": "a" * 100, "delay_ms": 10}]) >= 2000
    assert espkb.estimate_ms([{"key": "a", "repeat": 1000}]) > 10000
    assert espkb.estimate_ms([{"move": [32767, 0], "step": 1}]) > 32767
    assert espkb.estimate_ms([{"bogus": 1}, "x", {"move": "bad"}]) == 0


def test_sync_timeout_grows_with_job(mock):
    mock.respond("POST", "/api/seq", 200, {"ok": True, "job": 1, "elapsed_ms": 1500}, delay=1.5)
    kb = espkb.ESPKB(mock.host, token="", timeout=0.5)
    # base timeout 0.5 s, but a 1.2 s delay step makes the client wait long enough
    assert kb.seq([{"delay": 1200}])["ok"]


def test_host_normalisation():
    assert espkb.ESPKB("10.0.0.5").base_url == "http://10.0.0.5"
    assert espkb.ESPKB("http://10.0.0.5:8080/whatever").base_url == "http://10.0.0.5:8080"
    assert espkb.ESPKB("espkb.local:80").base_url == "http://espkb.local:80"


def test_stdlib_only_and_py38_syntax():
    src = open(ESPKB_PY).read()
    tree = ast.parse(src, feature_version=(3, 8))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", None)
    assert stdlib is not None
    assert mods <= set(stdlib) | {"__future__"}, mods - set(stdlib)
    # no PEP 585/604 runtime constructs that break 3.8 (annotations are strings via __future__)
    assert "from __future__ import annotations" in src
    for bad in (".removeprefix(", ".removesuffix(", "BooleanOptionalAction", "match "):
        assert bad not in src, bad
