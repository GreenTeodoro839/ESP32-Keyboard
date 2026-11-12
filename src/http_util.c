#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <errno.h>
#include <sys/select.h>
#include <sys/socket.h>
#include "config.h"
#include "http_util.h"
#include "wifi_mgr.h"

#define BODY_DEADLINE_US (10 * 1000000LL)
#define DRAIN_MAX_BYTES  (512 * 1024)
#define DRAIN_DEADLINE_US (3 * 1000000LL)

esp_err_t http_send_obj(httpd_req_t *req, espkb_err_t status, cJSON *obj)
{
    char *s = obj ? cJSON_PrintUnformatted(obj) : NULL;
    cJSON_Delete(obj);
    httpd_resp_set_status(req, s ? espkb_err_http_status_line(status) : "503 Service Unavailable");
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_send(req, s ? s : "{\"ok\":false,\"error\":\"busy\",\"message\":\"out of memory\"}",
                                    HTTPD_RESP_USE_STRLEN);
    cJSON_free(s);
    return err;
}

void json_add_error(cJSON *obj, espkb_err_t err, const char *msg, int index, long offset)
{
    cJSON_AddStringToObject(obj, "error", espkb_err_code(err));
    cJSON_AddStringToObject(obj, "message", (msg && msg[0]) ? msg : espkb_err_message(err));
    if (index >= 0) {
        cJSON_AddNumberToObject(obj, "index", index);
    }
    if (offset >= 0) {
        cJSON_AddNumberToObject(obj, "offset", (double)offset);
    }
}

esp_err_t http_send_error(httpd_req_t *req, espkb_err_t err, const char *msg, int index, long offset, uint32_t job)
{
    cJSON *o = cJSON_CreateObject();
    if (o) {
        cJSON_AddBoolToObject(o, "ok", false);
        json_add_error(o, err, msg, index, offset);
        if (job) {
            cJSON_AddNumberToObject(o, "job", job);
        }
    }
    return http_send_obj(req, err, o);
}

/* Copies a header value; returns false if absent/empty or too long (*too_long set). */
static bool get_hdr(httpd_req_t *req, const char *name, char *buf, size_t cap, bool *too_long)
{
    size_t n = httpd_req_get_hdr_value_len(req, name);
    *too_long = n >= cap;
    if (n == 0 || n >= cap) {
        return false;
    }
    return httpd_req_get_hdr_value_str(req, name, buf, cap) == ESP_OK;
}

static void strip_default_port(char *h)
{
    size_t n = strlen(h);
    if (n > 3 && strcmp(h + n - 3, ":80") == 0) {
        h[n - 3] = '\0';
    } else if (n > 4 && strcmp(h + n - 4, ":443") == 0) {
        h[n - 4] = '\0';
    }
}

/* Origin "scheme://host[:port]" must name the same host[:port] as the Host header. */
static bool origin_matches(const char *origin, const char *host)
{
    const char *p = strstr(origin, "://");
    if (!p || !host[0]) {
        return false;
    }
    char o[128], h[128];
    snprintf(o, sizeof(o), "%s", p + 3);
    char *slash = strchr(o, '/');
    if (slash) {
        *slash = '\0';
    }
    snprintf(h, sizeof(h), "%s", host);
    strip_default_port(o);
    strip_default_port(h);
    return o[0] && str_ieq(o, h);
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    c = (char)tolower((unsigned char)c);
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static size_t percent_decode(char *s)
{
    char *w = s;
    for (char *r = s; *r; r++) {
        if (r[0] == '%' && r[1] && r[2] && hexval(r[1]) >= 0 && hexval(r[2]) >= 0) {
            *w++ = (char)(hexval(r[1]) * 16 + hexval(r[2]));
            r += 2;
        } else {
            *w++ = *r;
        }
    }
    *w = '\0';
    return (size_t)(w - s);
}

static bool token_from_request(httpd_req_t *req)
{
    char buf[160];
    bool too_long;
    if (get_hdr(req, "Authorization", buf, sizeof(buf), &too_long)) {
        if (strncasecmp(buf, "Bearer ", 7) == 0) {
            const char *t = buf + 7;
            while (*t == ' ') {
                t++;
            }
            if (config_token_ok(t, strlen(t))) {
                return true;
            }
        }
    }
    if (get_hdr(req, "X-Token", buf, sizeof(buf), &too_long) && config_token_ok(buf, strlen(buf))) {
        return true;
    }
    size_t ql = httpd_req_get_url_query_len(req);
    if (ql > 0 && ql < 600) {
        char q[600];
        if (httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK &&
            httpd_query_key_value(q, "token", buf, sizeof(buf)) == ESP_OK) {
            size_t n = percent_decode(buf);
            if (config_token_ok(buf, n)) {
                return true;
            }
        }
    }
    return false;
}

espkb_err_t http_access_check(httpd_req_t *req)
{
    char host[96] = "";
    char origin[160];
    bool too_long;
    get_hdr(req, "Host", host, sizeof(host), &too_long);
    if (get_hdr(req, "Origin", origin, sizeof(origin), &too_long) || too_long) {
        if (too_long || !origin_matches(origin, host)) {
            return EK_FORBIDDEN;
        }
    }
    if (config_token_required()) {
        return token_from_request(req) ? EK_NONE : EK_UNAUTHORIZED;
    }
    return wifi_mgr_is_own_host(host) ? EK_NONE : EK_FORBIDDEN;
}

bool http_expect_continue(httpd_req_t *req)
{
    char exp[24];
    bool too_long;
    if (req->content_len > 0 && get_hdr(req, "Expect", exp, sizeof(exp), &too_long) && str_ieq(exp, "100-continue")) {
        return httpd_send(req, "HTTP/1.1 100 Continue\r\n\r\n", 25) == 25;
    }
    return false;
}

body_result_t http_read_body(httpd_req_t *req, char **out, size_t *outlen)
{
    *out = NULL;
    *outlen = 0;
    size_t len = req->content_len;
    if (len > ESPKB_MAX_BODY) {
        return BODY_TOO_LARGE;
    }
    char *buf = espkb_malloc(len + 1);
    if (!buf) {
        return BODY_FAILED;
    }
    size_t got = 0;
    int64_t deadline = now_us() + BODY_DEADLINE_US;
    while (got < len) {
        int r = httpd_req_recv(req, buf + got, len - got);
        if (r <= 0 || now_us() > deadline) {
            espkb_free(buf);
            return BODY_FAILED;
        }
        got += (size_t)r;
    }
    buf[len] = '\0';
    *out = buf;
    *outlen = len;
    return BODY_OK;
}

bool http_drain_body(httpd_req_t *req)
{
    size_t left = req->content_len;
    if (left > DRAIN_MAX_BYTES) {
        return false;
    }
    char buf[512];
    int64_t deadline = now_us() + DRAIN_DEADLINE_US;
    while (left > 0) {
        int r = httpd_req_recv(req, buf, left < sizeof(buf) ? left : sizeof(buf));
        if (r <= 0 || now_us() > deadline) {
            return false;
        }
        left -= (size_t)r;
    }
    return true;
}

esp_err_t http_reject(httpd_req_t *req, espkb_err_t err, const char *msg)
{
    if (req->content_len <= 4096) {
        http_send_error(req, err, msg, -1, -1, 0);
        return ESP_OK;
    }
    /* The server would purge an unread body after ESP_OK without any limit. */
    httpd_resp_set_hdr(req, "Connection", "close");
    http_send_error(req, err, msg, -1, -1, 0);
    http_drain_body(req);
    return ESP_FAIL;
}

esp_err_t http_reject_too_large(httpd_req_t *req)
{
    char msg[64];
    snprintf(msg, sizeof(msg), "request body larger than %d bytes", ESPKB_MAX_BODY);
    httpd_resp_set_hdr(req, "Connection", "close");
    http_send_error(req, EK_TOO_LARGE, msg, -1, -1, 0);
    /* Closing with the client's body still unread would make lwIP send a TCP
     * reset, and the client would typically lose the response we just sent. */
    http_drain_body(req);
    return ESP_FAIL; /* close the connection */
}

bool http_query_bool(httpd_req_t *req, const char *key, bool def)
{
    size_t ql = httpd_req_get_url_query_len(req);
    if (ql == 0 || ql >= 600) {
        return def;
    }
    char q[600], v[8];
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) != ESP_OK) {
        return def;
    }
    esp_err_t e = httpd_query_key_value(q, key, v, sizeof(v));
    if (e == ESP_ERR_NOT_FOUND) {
        return def;
    }
    return e == ESP_OK && (strcmp(v, "1") == 0 || str_ieq(v, "true") || str_ieq(v, "yes"));
}

bool http_query_flag(httpd_req_t *req, const char *key)
{
    return http_query_bool(req, key, false);
}

bool http_content_type_is(httpd_req_t *req, const char *type)
{
    char ct[64];
    bool too_long;
    if (!get_hdr(req, "Content-Type", ct, sizeof(ct), &too_long)) {
        return false;
    }
    size_t n = strlen(type);
    return strncasecmp(ct, type, n) == 0 && (ct[n] == '\0' || ct[n] == ';' || ct[n] == ' ');
}

/* ---------- slow-client guard ---------- */

#define GUARD_WAIT_MS   700
#define GUARD_BUDGET_MS 3000
#define GUARD_MAX_FD    256

static int32_t s_blocked_ms[GUARD_MAX_FD];   /* only touched by the server task */

static int guarded_recv(httpd_handle_t hd, int fd, char *buf, size_t len, int flags)
{
    (void)hd;
    if (buf == NULL) {
        return HTTPD_SOCK_ERR_INVALID;
    }
    if (fd >= 0 && fd < GUARD_MAX_FD) {
        int32_t left = GUARD_BUDGET_MS - s_blocked_ms[fd];
        if (left <= 0) {
            return HTTPD_SOCK_ERR_FAIL;
        }
        int32_t wait = left < GUARD_WAIT_MS ? left : GUARD_WAIT_MS;
        fd_set rf;
        FD_ZERO(&rf);
        FD_SET(fd, &rf);
        struct timeval tv = {.tv_sec = wait / 1000, .tv_usec = (wait % 1000) * 1000};
        int64_t t0 = now_us();
        int s = select(fd + 1, &rf, NULL, NULL, &tv);
        s_blocked_ms[fd] += (int32_t)((now_us() - t0) / 1000);
        if (s == 0) {
            return HTTPD_SOCK_ERR_TIMEOUT;
        }
        if (s < 0) {
            return HTTPD_SOCK_ERR_FAIL;
        }
    }
    int n = recv(fd, buf, len, flags);
    if (n < 0) {
        return (errno == EAGAIN || errno == EWOULDBLOCK) ? HTTPD_SOCK_ERR_TIMEOUT : HTTPD_SOCK_ERR_FAIL;
    }
    return n;
}

esp_err_t http_guard_open(httpd_handle_t hd, int fd)
{
    if (fd >= 0 && fd < GUARD_MAX_FD) {
        s_blocked_ms[fd] = 0;
    }
    return httpd_sess_set_recv_override(hd, fd, guarded_recv);
}

void http_guard_reset(httpd_req_t *req)
{
    int fd = httpd_req_to_sockfd(req);
    if (fd >= 0 && fd < GUARD_MAX_FD) {
        s_blocked_ms[fd] = 0;
    }
}
