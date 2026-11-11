"""API.md section 2: token forms, unauthorized access, GET / without token, Origin and Host checks."""
from __future__ import annotations

import time

import pytest

import support
from support import assert_nothing_emitted, expect_http_error, resp_json


def fresh_token(prefix="tok"):
    return "%s-%d" % (prefix, int(time.time() * 1000) % 10_000_000)


def test_open_without_token(dev, raw):
    st = dev.status()
    assert st["auth_required"] is False
    assert dev.get_config()["token_set"] is False
    assert raw.get("/api/config", auth=False).status_code == 200


def test_token_forms(dev, hid, raw, set_token):
    tok = fresh_token()
    res = set_token(tok)
    assert res["reboot_required"] is False, "token must take effect immediately"
    # missing / wrong
    expect_http_error(raw.get("/api/status", auth=False), 401, "unauthorized")
    expect_http_error(raw.get("/api/status", auth=False, headers={"Authorization": "Bearer wrong"}), 401, "unauthorized")
    expect_http_error(raw.get("/api/status", auth=False, headers={"X-Token": "wrong"}), 401, "unauthorized")
    expect_http_error(raw.get("/api/status", auth=False, params={"token": "wrong"}), 401, "unauthorized")
    expect_http_error(raw.get("/api/status", auth=False, headers={"Authorization": tok}), 401, "unauthorized")
    # the three accepted forms
    for kw in ({"headers": {"Authorization": "Bearer " + tok}}, {"headers": {"X-Token": tok}},
               {"params": {"token": tok}}):
        r = raw.get("/api/status", auth=False, **kw)
        assert r.status_code == 200, (kw, r.text)
        assert resp_json(r)["auth_required"] is True
    assert dev.get_config()["token_set"] is True


def test_every_api_endpoint_requires_token(dev, hid, raw, set_token):
    set_token(fresh_token())
    for method, path in [("GET", "/api"), ("GET", "/api/status"), ("GET", "/api/keys"), ("GET", "/api/config"),
                         ("GET", "/api/wifi/scan"), ("POST", "/api/config"), ("POST", "/api/wake"),
                         ("POST", "/api/release"), ("POST", "/api/reboot")]:
        r = raw.request(method, path, auth=False, json_body={} if method == "POST" else None)
        expect_http_error(r, 401, "unauthorized")


@pytest.mark.parametrize("path,body", [("/api/key", {"key": "a"}), ("/api/type", {"text": "x"}),
                                       ("/api/mouse", {"click": "left"}), ("/api/seq", {"steps": [{"key": "b"}]})])
def test_actions_without_token_emit_nothing(dev, hid, raw, set_token, path, body):
    set_token(fresh_token())
    m = hid.mark()
    expect_http_error(raw.post(path, auth=False, json_body=body), 401, "unauthorized")
    expect_http_error(raw.post(path, auth=False, json_body=body, headers={"X-Token": "nope"}), 401, "unauthorized")
    assert_nothing_emitted(hid, m)


def test_root_page_needs_no_token(dev, raw, set_token):
    set_token(fresh_token())
    r = raw.get("/", auth=False)
    assert r.status_code == 200, "GET / returned %d with a token configured" % r.status_code
    assert "html" in r.headers.get("Content-Type", "").lower()
    assert b"<" in r.content[:200]


def test_token_action_works_with_token(dev, hid, set_token):
    set_token(fresh_token())
    _, reps = support.do(hid, lambda: dev.key("k"))
    assert len(reps) == 2


def test_token_max_length_and_charset(dev, raw, set_token):
    tok = "".join(chr(33 + (i % 94)) for i in range(64))  # 64 printable non-space characters
    set_token(tok)
    r = raw.get("/api/status", auth=False, headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    r = raw.get("/api/status", auth=False, headers={"X-Token": tok})
    assert r.status_code == 200, r.text
    r = raw.get("/api/status", auth=False, params={"token": tok})  # percent-encoded by requests
    assert r.status_code == 200, "query token with special characters (percent-encoded) rejected: %s" % r.text


@pytest.mark.parametrize("bad", ["x" * 65, "has space", "tab\tchar", "café", "nl\n"], ids=repr)
def test_invalid_tokens_rejected(dev, bad):
    support.expect_error(lambda: dev.set_config(token=bad), 400, "bad_request")
    assert dev.get_config()["token_set"] is False


def test_clearing_token_reopens(dev, raw, set_token):
    set_token(fresh_token())
    res = dev.set_config(token="")
    dev.token = ""
    assert res["ok"] is True
    assert raw.get("/api/status", auth=False).status_code == 200
    assert dev.status()["auth_required"] is False


def test_origin_mismatch_forbidden(dev, hid, raw):
    m = hid.mark()
    for origin in ("http://evil.example", "null", "http://evil.example:80", "https://10.0.0.1"):
        expect_http_error(raw.get("/api/status", headers={"Origin": origin}), 403, "forbidden")
        expect_http_error(raw.post("/api/key", json_body={"key": "a"}, headers={"Origin": origin}), 403, "forbidden")
    assert_nothing_emitted(hid, m)


def test_origin_matching_host_allowed(dev, hid, raw, device_host):
    origin = "http://" + support.host_only(device_host)
    r = raw.get("/api/status", headers={"Origin": origin})
    assert r.status_code == 200, r.text
    _, reps = support.do(hid, lambda: raw.post("/api/key", json_body={"key": "a"}, headers={"Origin": origin}))
    assert len(reps) == 2


def test_no_cors_headers(dev, raw, device_host):
    for origin in ("http://" + support.host_only(device_host), "http://evil.example"):
        for r in (raw.get("/api/status", headers={"Origin": origin}),
                  raw.request("OPTIONS", "/api/key", headers={"Origin": origin,
                                                              "Access-Control-Request-Method": "POST"})):
            cors = [h for h in r.headers if h.lower().startswith("access-control-")]
            assert not cors, "device returned CORS headers %s" % cors


@pytest.mark.parametrize("host_header", ["evil.example", "evil.example:80", "10.255.255.1", "espkb.local.evil.example"])
def test_host_check_without_token(dev, hid, raw, host_header):
    m = hid.mark()
    expect_http_error(raw.get("/api/status", headers={"Host": host_header}), 403, "forbidden")
    expect_http_error(raw.post("/api/key", json_body={"key": "a"}, headers={"Host": host_header}), 403, "forbidden")
    assert_nothing_emitted(hid, m)


def test_host_check_accepts_device_names(dev, raw, device_host):
    cfg = dev.get_config()
    st = dev.status()
    hn = cfg["hostname"]
    ip = st["wifi"]["ip"]
    allowed = [ip, ip + ":80", hn + ".local", hn + ".local:80", hn, hn + ":80", "192.168.4.1", "192.168.4.1:80"]
    for h in allowed:
        r = raw.get("/api/status", headers={"Host": h})
        assert r.status_code == 200, "Host %r rejected: %s" % (h, r.text)
    expect_http_error(raw.get("/api/status", headers={"Host": ip + ":8080"}), 403, "forbidden")


def test_host_check_skipped_when_token_set(dev, raw, set_token):
    tok = fresh_token()
    set_token(tok)
    r = raw.get("/api/status", auth=False, headers={"Host": "evil.example", "Authorization": "Bearer " + tok})
    assert r.status_code == 200, "with a token configured the Host check should not apply: %s" % r.text
