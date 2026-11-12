#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "config.h"
#include "hid_engine.h"
#include "http_api.h"
#include "http_sync.h"
#include "http_util.h"
#include "keymap.h"
#include "ota.h"
#include "status.h"
#include "steps.h"
#include "usb_hid.h"
#include "wifi_mgr.h"
#include "ws.h"

static const char *TAG = "http";

extern const uint8_t web_page_gz[];
extern const size_t web_page_gz_len;
extern const int web_page_is_placeholder;

typedef enum { EP_TYPE, EP_KEY, EP_MOUSE, EP_SEQ } endpoint_t;

#define CHECK_ACCESS(req)                                          \
    do {                                                           \
        espkb_err_t _e = http_access_check(req);                   \
        if (_e) return http_send_error(req, _e, NULL, -1, -1, 0);  \
    } while (0)

static cJSON *ok_obj(void)
{
    cJSON *o = cJSON_CreateObject();
    if (o) {
        cJSON_AddBoolToObject(o, "ok", true);
    }
    return o;
}

/* ---------- web page ---------- */

static esp_err_t h_index(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
    return httpd_resp_send(req, (const char *)web_page_gz, (ssize_t)web_page_gz_len);
}

/* ---------- actions ---------- */

static void async_job_done(job_t *job)
{
    job_free(job);
}

static bool has_any(const cJSON *o, const char *const *names, int n)
{
    for (int i = 0; i < n; i++) {
        if (cJSON_GetObjectItemCaseSensitive(o, names[i])) {
            return true;
        }
    }
    return false;
}

static job_t *parse_action(endpoint_t ep, cJSON *root, const keysim_t *sim, parse_err_t *pe)
{
    static const char *const mouse_verbs[] = {"move", "move_to", "click", "button", "scroll"};
    static const char *const key_verb[] = {"key"};
    static const char *const text_verb[] = {"text"};
    if (ep == EP_SEQ) {
        if (cJSON_IsArray(root)) {
            return job_from_array(root, sim, pe);
        }
        if (!cJSON_IsObject(root)) {
            pe->err = EK_BAD_REQUEST;
            snprintf(pe->msg, sizeof(pe->msg), "body must be {\"steps\":[...]} or an array of steps");
            return NULL;
        }
        const cJSON *steps = cJSON_GetObjectItemCaseSensitive(root, "steps");
        if (!steps) {
            pe->err = EK_BAD_REQUEST;
            snprintf(pe->msg, sizeof(pe->msg), "missing field \"steps\"");
            return NULL;
        }
        return job_from_array(steps, sim, pe);
    }
    if (!cJSON_IsObject(root)) {
        pe->err = EK_BAD_REQUEST;
        snprintf(pe->msg, sizeof(pe->msg), "body must be a JSON object");
        return NULL;
    }
    const char *const *verbs = ep == EP_TYPE ? text_verb : ep == EP_KEY ? key_verb : mouse_verbs;
    int nverbs = ep == EP_MOUSE ? (int)(sizeof(mouse_verbs) / sizeof(mouse_verbs[0])) : 1;
    if (!has_any(root, verbs, nverbs)) {
        pe->err = EK_BAD_REQUEST;
        snprintf(pe->msg, sizeof(pe->msg), ep == EP_TYPE ? "missing field \"text\""
                 : ep == EP_KEY ? "missing field \"key\""
                 : "missing field: one of move, move_to, click, button, scroll");
        return NULL;
    }
    uint32_t mask = ep == EP_TYPE ? STEP_MASK(STEP_TEXT) : ep == EP_KEY ? STEP_MASK(STEP_KEY) : STEP_MASK_MOUSE;
    return job_from_step(root, mask, sim, pe);
}

static esp_err_t h_action(httpd_req_t *req)
{
    endpoint_t ep = (endpoint_t)(intptr_t)req->user_ctx;
    CHECK_ACCESS(req);
    engine_http_activity();
    bool async = http_query_flag(req, "async");

    char *body = NULL;
    size_t len = 0;
    switch (http_read_body(req, &body, &len)) {
    case BODY_TOO_LARGE:
        return http_reject_too_large(req);
    case BODY_FAILED:
        return ESP_FAIL;
    case BODY_OK:
        break;
    }

    keysim_t sim;
    engine_held_sim(&sim);
    parse_err_t pe = {.err = EK_BAD_REQUEST, .index = -1, .offset = -1};
    job_t *job = NULL;
    if (ep == EP_TYPE && http_content_type_is(req, "text/plain")) {
        job = job_from_text(body, len, &sim, &pe);
    } else {
        cJSON *root = json_parse_body(body, len, &pe);
        if (root) {
            job = parse_action(ep, root, &sim, &pe);
            cJSON_Delete(root);
        }
    }
    espkb_free(body);
    if (!job) {
        return http_send_error(req, pe.err, pe.msg, pe.index, pe.offset, 0);
    }
    if (!usb_hid_mounted()) {
        job_free(job);
        return http_send_error(req, EK_USB_NOT_READY, NULL, -1, -1, 0);
    }
    if (usb_hid_suspended() && !usb_hid_remote_wakeup_allowed()) {
        job_free(job);
        return http_send_error(req, EK_USB_SUSPENDED, NULL, -1, -1, 0);
    }
    job->owner = OWNER_HTTP;
    if (!async) {
        http_sync_run_job(req, job);
        return ESP_OK;
    }
    job->done = async_job_done;
    uint32_t id = 0;
    espkb_err_t e = engine_submit(job, &id);
    if (e) {
        job_free(job);
        return http_send_error(req, e, NULL, -1, -1, 0);
    }
    cJSON *o = ok_obj();
    if (o) {
        cJSON_AddNumberToObject(o, "job", id);
        cJSON_AddBoolToObject(o, "queued", true);
    }
    return http_send_obj(req, EK_NONE, o);
}

static esp_err_t h_release(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    int n = engine_release_all();
    cJSON *o = ok_obj();
    if (o) {
        cJSON_AddNumberToObject(o, "aborted", n);
    }
    return http_send_obj(req, EK_NONE, o);
}

static esp_err_t h_wake(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    engine_http_activity();
    if (!usb_hid_mounted()) {
        return http_send_error(req, EK_USB_NOT_READY, NULL, -1, -1, 0);
    }
    if (usb_hid_suspended()) {
        if (!usb_hid_remote_wakeup_allowed()) {
            return http_send_error(req, EK_USB_SUSPENDED, NULL, -1, -1, 0);
        }
        engine_request_wake();
    }
    return http_send_obj(req, EK_NONE, ok_obj());
}

static void reboot_cb(void *arg)
{
    (void)arg;
    esp_restart();
}

static esp_err_t h_reboot(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    engine_release_all();
    static esp_timer_handle_t t;
    if (!t) {
        const esp_timer_create_args_t a = {.callback = reboot_cb, .name = "reboot"};
        esp_timer_create(&a, &t);
    }
    esp_err_t r = http_send_obj(req, EK_NONE, ok_obj());
    ESP_LOGW(TAG, "reboot requested over HTTP");
    if (t) {
        esp_timer_stop(t);
        esp_timer_start_once(t, 500 * 1000);
    }
    return r;
}

/* ---------- information ---------- */

static esp_err_t h_status(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    return http_send_obj(req, EK_NONE, status_build(true));
}

static esp_err_t h_keys(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    cJSON *o = ok_obj();
    cJSON *keys = o ? cJSON_AddObjectToObject(o, "keys") : NULL;
    if (keys) {
        char n[2] = {0, 0};
        for (char c = 'a'; c <= 'z'; c++) {
            n[0] = c;
            cJSON_AddNumberToObject(keys, n, 4 + (c - 'a'));
        }
        for (char c = '1'; c <= '9'; c++) {
            n[0] = c;
            cJSON_AddNumberToObject(keys, n, 30 + (c - '1'));
        }
        cJSON_AddNumberToObject(keys, "0", 39);
        size_t count;
        const keyname_t *t = keymap_names(&count);
        for (size_t i = 0; i < count; i++) {
            cJSON_AddNumberToObject(keys, t[i].name, t[i].usage);
        }
        cJSON *b = cJSON_AddArrayToObject(o, "buttons");
        for (int i = 0; b && i < MOUSE_BUTTON_COUNT; i++) {
            cJSON_AddItemToArray(b, cJSON_CreateString(keymap_button_name(i)));
        }
    }
    return http_send_obj(req, EK_NONE, o);
}

static const char API_DESC[] =
    "{\"ok\":true,\"name\":\"" ESPKB_NAME "\",\"api\":1,\"version\":\"" ESPKB_FW_VERSION "\","
    "\"notes\":["
    "\"Bodies are JSON (Content-Type: application/json), max 65536 bytes (POST /api/ota takes a raw image instead); responses are JSON.\","
    "\"Action endpoints are synchronous (reply when the job has run: {ok,job,elapsed_ms}); add ?async=1 to reply at once ({ok,job,queued}).\","
    "\"Errors: {ok:false,error,message[,index][,offset]}. Validation is atomic: an invalid step rejects the whole request.\","
    "\"If a token is configured send it as 'Authorization: Bearer <t>', 'X-Token: <t>' or ?token=<t> (WebSocket: ?token=).\","
    "\"Text uses the US layout: printable ASCII plus \\\\n (Enter), \\\\t, \\\\b, ESC.\","
    "\"Held keys are released after 5000 ms without HTTP action requests (ttl_ms on a down step changes this), after 1000 ms without messages on the owning WebSocket, or when it closes.\"],"
    "\"endpoints\":["
    "{\"method\":\"POST\",\"path\":\"/api/type\",\"desc\":\"Type text: {\\\"text\\\":\\\"...\\\",\\\"delay_ms\\\":5} or a text/plain body\"},"
    "{\"method\":\"POST\",\"path\":\"/api/key\",\"desc\":\"One key step: tap/down/up a key or combo, e.g. {\\\"key\\\":\\\"ctrl+alt+t\\\"}\"},"
    "{\"method\":\"POST\",\"path\":\"/api/mouse\",\"desc\":\"One mouse step: move, move_to, click, button or scroll, e.g. {\\\"move\\\":[120,-40]}\"},"
    "{\"method\":\"POST\",\"path\":\"/api/seq\",\"desc\":\"Several steps in order: {\\\"steps\\\":[...]} or a bare array, max 512 steps\"},"
    "{\"method\":\"POST\",\"path\":\"/api/release\",\"desc\":\"Emergency stop: abort the running job, flush the queue, release all keys and buttons\"},"
    "{\"method\":\"POST\",\"path\":\"/api/wake\",\"desc\":\"Send USB remote wakeup if the host is suspended\"},"
    "{\"method\":\"GET\",\"path\":\"/api/status\",\"desc\":\"Device, USB, HID queue and WiFi state\"},"
    "{\"method\":\"GET\",\"path\":\"/api/keys\",\"desc\":\"Key names with HID usage codes, and mouse button names\"},"
    "{\"method\":\"GET\",\"path\":\"/api\",\"desc\":\"This description\"},"
    "{\"method\":\"GET\",\"path\":\"/api/config\",\"desc\":\"Configuration (passwords and token are not returned)\"},"
    "{\"method\":\"POST\",\"path\":\"/api/config\",\"desc\":\"Partial config update: hostname, wifi_ssid, wifi_password, ap_password, token, key_delay_ms\"},"
    "{\"method\":\"GET\",\"path\":\"/api/wifi/scan\",\"desc\":\"Scan for WiFi networks (takes about 3 s)\"},"
    "{\"method\":\"POST\",\"path\":\"/api/reboot\",\"desc\":\"Reboot about 500 ms after replying\"},"
    "{\"method\":\"GET\",\"path\":\"/api/ota\",\"desc\":\"Firmware update state: running image, free slot and its size, progress of an upload\"},"
    "{\"method\":\"POST\",\"path\":\"/api/ota\",\"desc\":\"Firmware update: the raw app image (firmware.bin) as the body with Content-Length; it is verified, activated and the device reboots (?reboot=0: stay up). Settings are kept\"},"
    "{\"method\":\"GET\",\"path\":\"/ws\",\"desc\":\"WebSocket: send steps as JSON text frames (optional id for an ack), {\\\"ping\\\":x} keep-alive, {\\\"release\\\":true} emergency stop; receives status pushes\"},"
    "{\"method\":\"GET\",\"path\":\"/\",\"desc\":\"Web UI\"}],"
    "\"steps\":["
    "\"{\\\"text\\\":\\\"ls -l\\\\n\\\"} type text; optional delay_ms (0-1000, default key_delay_ms) between reports\","
    "\"{\\\"key\\\":\\\"ctrl+alt+del\\\"} tap a key or combo (press in order, hold hold_ms, release in reverse); optional hold_ms, repeat (1-1000), delay_ms; key may be a name, a combo, a usage code 1-255 or \\\"0x28\\\"\","
    "\"{\\\"key\\\":\\\"shift\\\",\\\"action\\\":\\\"down\\\"} hold (down) or release (up) keys; optional ttl_ms (1-600000) for HTTP holds\","
    "\"{\\\"move\\\":[dx,dy]} relative mouse move, -32767..32767 per axis, split into straight-line reports; optional step (1-127, default 127), delay_ms (default 0)\","
    "\"{\\\"move_to\\\":[x,y]} absolute mouse position, 0..32767 per axis = left/top..right/bottom of the screen; with \\\"screen\\\":[w,h] x,y are pixels (0..w-1, 0..h-1)\","
    "\"{\\\"click\\\":\\\"left\\\"} click left/right/middle/back/forward; optional count (1-100), hold_ms (default 20), interval_ms (default 60)\","
    "\"{\\\"button\\\":\\\"left\\\",\\\"action\\\":\\\"down\\\"} hold or release a mouse button (action required); optional ttl_ms\","
    "\"{\\\"scroll\\\":3} or {\\\"scroll\\\":[v,h]} wheel: v>0 up, h>0 right, -32767..32767\","
    "\"{\\\"delay\\\":250} wait 0-60000 ms\","
    "\"{\\\"release\\\":true} release all keys and buttons as a queued step\"]}";

static esp_err_t h_api(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, API_DESC, sizeof(API_DESC) - 1);
}

static esp_err_t h_config_get(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    espkb_config_t c;
    config_get(&c);
    wifi_status_t ws;
    wifi_mgr_get_status(&ws);
    cJSON *o = ok_obj();
    if (o) {
        cJSON_AddStringToObject(o, "hostname", c.hostname);
        cJSON_AddStringToObject(o, "wifi_ssid", c.wifi_ssid);
        cJSON_AddBoolToObject(o, "wifi_password_set", c.wifi_password[0] != '\0');
        cJSON_AddStringToObject(o, "ap_ssid", ws.ap_ssid);
        cJSON_AddBoolToObject(o, "ap_password_set", c.ap_password_custom);
        cJSON_AddBoolToObject(o, "token_set", c.token[0] != '\0');
        cJSON_AddNumberToObject(o, "key_delay_ms", c.key_delay_ms);
    }
    memset(&c, 0, sizeof(c));
    return http_send_obj(req, EK_NONE, o);
}

static esp_err_t h_config_post(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    char *body = NULL;
    size_t len = 0;
    switch (http_read_body(req, &body, &len)) {
    case BODY_TOO_LARGE:
        return http_reject_too_large(req);
    case BODY_FAILED:
        return ESP_FAIL;
    case BODY_OK:
        break;
    }
    parse_err_t pe = {.index = -1, .offset = -1};
    cJSON *root = json_parse_body(body, len, &pe);
    espkb_free(body);
    if (!root) {
        return http_send_error(req, pe.err, pe.msg, -1, -1, 0);
    }
    bool reboot = false;
    char msg[112];
    bool ok = config_update_json(root, &reboot, msg, sizeof(msg));
    cJSON_Delete(root);
    if (!ok) {
        return http_send_error(req, EK_BAD_REQUEST, msg, -1, -1, 0);
    }
    cJSON *o = ok_obj();
    if (o) {
        cJSON_AddBoolToObject(o, "reboot_required", reboot);
    }
    return http_send_obj(req, EK_NONE, o);
}

static void scan_done(void *arg, espkb_err_t err, char *json)
{
    http_sync_finish_json((sreq_t *)arg, err, err ? "WiFi scan failed, try again" : NULL, json);
}

static esp_err_t h_scan(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    sreq_t *s = http_sync_begin(req);
    if (!s) {
        return http_send_error(req, EK_BUSY, "too many requests waiting", -1, -1, 0);
    }
    if (!wifi_mgr_scan(scan_done, s)) {
        http_sync_finish_json(s, EK_BUSY, "too many scans pending", NULL);
    }
    return ESP_OK;
}

/* ---------- firmware update ---------- */

static esp_err_t h_ota_get(httpd_req_t *req)
{
    CHECK_ACCESS(req);
    cJSON *o = ok_obj();
    if (o) {
        ota_add_info(o);
    }
    return http_send_obj(req, EK_NONE, o);
}

static esp_err_t h_ota_post(httpd_req_t *req)
{
    /* The body is a whole firmware image: a refused request must not make the server read all of it. */
    espkb_err_t e = http_access_check(req);
    if (e) {
        return http_reject(req, e, NULL);
    }
    return ota_handle_upload(req);
}

/* ---------- errors from the server itself ---------- */

static esp_err_t err_reply(httpd_req_t *req, espkb_err_t err)
{
    http_guard_reset(req);
    return http_reject(req, err, NULL);
}

static esp_err_t err_not_found(httpd_req_t *req, httpd_err_code_t code)
{
    (void)code;
    return err_reply(req, EK_NOT_FOUND);
}

static esp_err_t err_method(httpd_req_t *req, httpd_err_code_t code)
{
    (void)code;
    return err_reply(req, EK_METHOD_NOT_ALLOWED);
}

/* Reading a request timed out (after 700 ms, see the slow-client guard).
 * The server hands a request with a body to its handler only once the first
 * body byte is there, but a client that sent "Expect: 100-continue" (curl does
 * for bodies over 1 MB, i.e. for firmware images) sends none until it is told
 * to. So the go-ahead is given here; ESP_OK makes the server read on. The
 * guard still drops a client that keeps stalling. */
static esp_err_t err_timeout(httpd_req_t *req, httpd_err_code_t code)
{
    if (http_expect_continue(req)) {
        return ESP_OK;
    }
    httpd_resp_send_err(req, code, NULL);
    return ESP_FAIL;
}

static esp_err_t err_bad(httpd_req_t *req, httpd_err_code_t code)
{
    http_guard_reset(req);
    char msg[48];
    snprintf(msg, sizeof(msg), "malformed HTTP request (%d)", (int)code);
    httpd_resp_set_hdr(req, "Connection", "close");
    http_send_error(req, EK_BAD_REQUEST, msg, -1, -1, 0);
    return ESP_FAIL; /* parser state is unknown: close */
}

typedef struct {
    const char *uri;
    httpd_method_t method;
    esp_err_t (*fn)(httpd_req_t *req);
    void *ctx;
} route_t;

static const route_t s_routes[] = {
    {"/", HTTP_GET, h_index, NULL},
    {"/api", HTTP_GET, h_api, NULL},
    {"/api/status", HTTP_GET, h_status, NULL},
    {"/api/keys", HTTP_GET, h_keys, NULL},
    {"/api/config", HTTP_GET, h_config_get, NULL},
    {"/api/config", HTTP_POST, h_config_post, NULL},
    {"/api/wifi/scan", HTTP_GET, h_scan, NULL},
    {"/api/type", HTTP_POST, h_action, (void *)EP_TYPE},
    {"/api/key", HTTP_POST, h_action, (void *)EP_KEY},
    {"/api/mouse", HTTP_POST, h_action, (void *)EP_MOUSE},
    {"/api/seq", HTTP_POST, h_action, (void *)EP_SEQ},
    {"/api/release", HTTP_POST, h_release, NULL},
    {"/api/wake", HTTP_POST, h_wake, NULL},
    {"/api/reboot", HTTP_POST, h_reboot, NULL},
    {"/api/ota", HTTP_GET, h_ota_get, NULL},
    {"/api/ota", HTTP_POST, h_ota_post, NULL},
};

/* Every handler runs through here so the slow-client guard restarts its
 * budget for the body phase and again for the next request. */
static esp_err_t dispatch(httpd_req_t *req)
{
    const route_t *r = req->user_ctx;
    req->user_ctx = r->ctx;
    http_guard_reset(req);
    esp_err_t e = r->fn(req);
    http_guard_reset(req);
    return e;
}

static esp_err_t on_open(httpd_handle_t hd, int fd)
{
    return http_guard_open(hd, fd);
}

static void on_close(httpd_handle_t hd, int fd)
{
    (void)hd;
    if (!ws_on_close(fd)) {
        close(fd);
    }
}

void http_api_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.stack_size = 8192;
    cfg.task_priority = 6;
    cfg.max_open_sockets = 28;     /* LWIP_MAX_SOCKETS (36) - 3 internal - 4 WS reaper - 1 spare */
    cfg.max_uri_handlers = 24;
    cfg.max_resp_headers = 8;
    cfg.backlog_conn = 12;
    cfg.lru_purge_enable = true;
    cfg.recv_wait_timeout = 3;
    cfg.send_wait_timeout = 3;
    cfg.keep_alive_enable = true;
    cfg.keep_alive_idle = 10;
    cfg.keep_alive_interval = 5;
    cfg.keep_alive_count = 3;
    cfg.open_fn = on_open;
    cfg.close_fn = on_close;

    httpd_handle_t hd = NULL;
    ESP_ERROR_CHECK(httpd_start(&hd, &cfg));
    http_sync_init();
    ws_init(hd);

    for (size_t i = 0; i < sizeof(s_routes) / sizeof(s_routes[0]); i++) {
        const httpd_uri_t u = {
            .uri = s_routes[i].uri,
            .method = s_routes[i].method,
            .handler = dispatch,
            .user_ctx = (void *)&s_routes[i],
        };
        ESP_ERROR_CHECK(httpd_register_uri_handler(hd, &u));
    }
    ESP_ERROR_CHECK(ws_register(hd));

    httpd_register_err_handler(hd, HTTPD_404_NOT_FOUND, err_not_found);
    httpd_register_err_handler(hd, HTTPD_405_METHOD_NOT_ALLOWED, err_method);
    httpd_register_err_handler(hd, HTTPD_408_REQ_TIMEOUT, err_timeout);
    httpd_register_err_handler(hd, HTTPD_400_BAD_REQUEST, err_bad);
    httpd_register_err_handler(hd, HTTPD_411_LENGTH_REQUIRED, err_bad);
    httpd_register_err_handler(hd, HTTPD_414_URI_TOO_LONG, err_bad);
    httpd_register_err_handler(hd, HTTPD_431_REQ_HDR_FIELDS_TOO_LARGE, err_bad);
    httpd_register_err_handler(hd, HTTPD_501_METHOD_NOT_IMPLEMENTED, err_bad);
    httpd_register_err_handler(hd, HTTPD_505_VERSION_NOT_SUPPORTED, err_bad);

    ESP_LOGI(TAG, "HTTP server on port 80 (web page: %s, %u bytes gzip)",
             web_page_is_placeholder ? "placeholder" : "web/index.html", (unsigned)web_page_gz_len);
}
