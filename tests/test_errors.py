"""API.md section 1/4: malformed bodies, content types, methods, unknown paths, empty bodies, JSON errors."""
from __future__ import annotations

import pytest

import support
from support import assert_nothing_emitted, expect_http_error, resp_json

JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize("path", ["/api/key", "/api/seq", "/api/mouse", "/api/type", "/api/config"])
@pytest.mark.parametrize("body", [b"{not json", b'{"key": "a"', b"\xff\xfe", b'{"key": "a"} trailing', b"[1, 2"],
                         ids=repr)
def test_malformed_json(hid, raw, path, body):
    m = hid.mark()
    expect_http_error(raw.post(path, data=body, headers=JSON), 400, "bad_request")
    assert_nothing_emitted(hid, m, wait_ms=100)


@pytest.mark.parametrize("path", ["/api/key", "/api/mouse", "/api/type"])
@pytest.mark.parametrize("body", [b"[1, 2]", b'"a string"', b"42", b"null", b"true"], ids=repr)
def test_json_not_an_object(hid, raw, path, body):
    m = hid.mark()
    expect_http_error(raw.post(path, data=body, headers=JSON), 400, ("bad_request", "bad_step"))
    assert_nothing_emitted(hid, m, wait_ms=100)


@pytest.mark.parametrize("path", ["/api/key", "/api/mouse", "/api/seq", "/api/config"])
def test_empty_body(hid, raw, path):
    m = hid.mark()
    expect_http_error(raw.post(path, data=b"", headers=JSON), 400, "bad_request")
    assert_nothing_emitted(hid, m, wait_ms=100)


def test_empty_json_body_on_type(hid, raw):
    m = hid.mark()
    expect_http_error(raw.post("/api/type", data=b"", headers=JSON), 400, "bad_request")
    expect_http_error(raw.post("/api/type", data=b"{}", headers=JSON), 400, ("bad_request", "bad_step"))
    assert_nothing_emitted(hid, m, wait_ms=100)


def test_form_encoded_body_rejected(hid, raw):
    m = hid.mark()
    r = raw.post("/api/key", data=b"key=a", headers={"Content-Type": "application/x-www-form-urlencoded"})
    expect_http_error(r, 400, "bad_request")
    assert_nothing_emitted(hid, m)


def test_json_with_charset_parameter_accepted(hid, raw):
    _, reps = support.do(hid, lambda: raw.post("/api/key", data=b'{"key":"a"}',
                                               headers={"Content-Type": "application/json; charset=utf-8"}))
    assert len(reps) == 2


@pytest.mark.parametrize("ctype", ["text/plain", None])
def test_key_with_non_json_content_type(hid, raw, ctype):
    """API.md says bodies are JSON with Content-Type application/json; a JSON body with another (or no)
    content type must either be rejected with 400 and nothing emitted, or executed exactly once."""
    m = hid.mark()
    headers = {"Content-Type": ctype} if ctype else {}
    r = raw.post("/api/key", data=b'{"key":"a"}', headers=headers)
    hid.wait_idle(200)
    reps = hid.reports(m)
    if r.status_code == 200:
        assert len(reps) == 2
    else:
        expect_http_error(r, 400, "bad_request")
        assert reps == []


WRONG_METHODS = [("GET", "/api/type"), ("GET", "/api/key"), ("GET", "/api/mouse"), ("GET", "/api/seq"),
                 ("GET", "/api/release"), ("GET", "/api/wake"), ("GET", "/api/reboot"), ("POST", "/api/status"),
                 ("POST", "/api/keys"), ("POST", "/api"), ("POST", "/api/wifi/scan"), ("PUT", "/api/key"),
                 ("DELETE", "/api/config"), ("PATCH", "/api/config"), ("PUT", "/api/status")]


@pytest.mark.parametrize("method,path", WRONG_METHODS, ids=lambda v: str(v))
def test_wrong_method(hid, raw, method, path):
    m = hid.mark()
    r = raw.request(method, path, json_body={"key": "a"} if method != "GET" else None)
    expect_http_error(r, 405, "method_not_allowed")
    assert_nothing_emitted(hid, m, wait_ms=100)


@pytest.mark.parametrize("method,path", [("GET", "/api/nope"), ("POST", "/api/nope"), ("GET", "/api/statuss"),
                                         ("POST", "/api/type/extra"), ("GET", "/api/status/x"),
                                         ("GET", "/api/wifi"), ("POST", "/api/keyboard")], ids=lambda v: str(v))
def test_unknown_path(hid, raw, method, path):
    m = hid.mark()
    r = raw.request(method, path, json_body={"key": "a"} if method == "POST" else None)
    expect_http_error(r, 404, "not_found")
    assert_nothing_emitted(hid, m, wait_ms=100)


def test_query_string_on_get_is_ignored(raw):
    r = raw.get("/api/status", params={"foo": "bar"})
    assert r.status_code == 200 and resp_json(r)["ok"] is True


@pytest.mark.parametrize("path", ["/api/release", "/api/wake"])
def test_bodyless_endpoints_accept_empty_and_json_bodies(raw, path):
    for kw in ({"data": b""}, {"json_body": {}}, {"json_body": {"ignored": 1}}):
        r = raw.post(path, **kw)
        assert r.status_code == 200, (kw, r.text)
        assert resp_json(r)["ok"] is True


def test_type_body_too_large(hid, raw):
    m = hid.mark()
    body = ('{"text":"' + "a" * 65600 + '"}').encode()
    expect_http_error(raw.post("/api/type", data=body, headers=JSON), 413, "too_large")
    assert_nothing_emitted(hid, m)


def test_text_plain_too_large(hid, raw):
    m = hid.mark()
    expect_http_error(raw.post("/api/type", data=b"a" * 65537, headers={"Content-Type": "text/plain"}), 413,
                      "too_large")
    assert_nothing_emitted(hid, m)


def test_success_responses_are_json(raw):
    for path in ("/api/status", "/api", "/api/keys", "/api/config"):
        r = raw.get(path)
        assert r.status_code == 200
        assert resp_json(r)["ok"] is True


def test_error_object_shape(raw):
    r = raw.get("/api/definitely-missing")
    obj = expect_http_error(r, 404, "not_found")
    assert set(obj) >= {"ok", "error", "message"}


def test_device_survives_garbage_requests(dev, hid, raw):
    import socket
    hid.assert_isolated()  # raw sockets bypass the guarded clients
    host, _, port = support.host_only(raw.base).partition(":")
    for payload in (b"GARBAGE\r\n\r\n", b"GET\r\n\r\n", b"POST /api/key HTTP/1.1\r\nContent-Length: 5\r\n\r\n",
                    b"\x00" * 64):
        s = socket.create_connection((host, int(port or 80)), timeout=3)
        try:
            s.sendall(payload)
            s.settimeout(3)
            try:
                s.recv(1024)
            except (socket.timeout, OSError):
                pass
        finally:
            s.close()
    assert dev.status()["ok"] is True
    _, reps = support.do(hid, lambda: dev.key("a"))
    assert len(reps) == 2
