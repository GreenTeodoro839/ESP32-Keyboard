#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hid_engine.h"
#include "http_util.h"
#include "status.h"
#include "steps.h"
#include "usb_hid.h"
#include "ws.h"

static const char *TAG = "ws";

#define MAX_OUTBOX         64
#define STATUS_PERIOD_US   (2000 * 1000LL)
#define STATUS_MIN_GAP_US  (50 * 1000LL)
#define ERROR_GAP_US       (1000 * 1000LL)

typedef struct outmsg {
    struct outmsg *next;
    size_t len;
    char data[];
} outmsg_t;

typedef struct {
    int fd;                 /* -1 = free slot */
    uint32_t owner;
    uint32_t gen;
    outmsg_t *head;
    outmsg_t *tail;
    int nmsg;
    int64_t err_last_us[EK_COUNT];
} client_t;

typedef struct {
    int slot;
    uint32_t gen;
    char *id_json;          /* printed "id" of the message, NULL if none */
} wsjob_ctx_t;

static client_t s_c[ESPKB_WS_MAX_CLIENTS];
static SemaphoreHandle_t s_mx;
static httpd_handle_t s_hd;
static TaskHandle_t s_push_task;
static volatile bool s_status_dirty;
static volatile int s_flush_pending;
static uint32_t s_gen_counter;

static void lock(void) { xSemaphoreTake(s_mx, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_mx); }

void espkb_status_changed(void)
{
    s_status_dirty = true;
    if (s_push_task) {
        xTaskNotifyGive(s_push_task);
    }
}

int ws_client_count(void)
{
    int n = 0;
    if (!s_mx) {
        return 0;
    }
    lock();
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        if (s_c[i].fd >= 0) {
            n++;
        }
    }
    unlock();
    return n;
}

static void free_outbox(client_t *c)
{
    outmsg_t *m = c->head;
    while (m) {
        outmsg_t *n = m->next;
        espkb_free(m);
        m = n;
    }
    c->head = c->tail = NULL;
    c->nmsg = 0;
}

/* ---------- sending (always from the HTTP server task) ---------- */

static void flush_work(void *arg)
{
    (void)arg;
    s_flush_pending = 0;
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        lock();
        int fd = s_c[i].fd;
        outmsg_t *m = s_c[i].head;
        s_c[i].head = s_c[i].tail = NULL;
        s_c[i].nmsg = 0;
        unlock();
        bool failed = fd < 0 || httpd_ws_get_fd_info(s_hd, fd) != HTTPD_WS_CLIENT_WEBSOCKET;
        while (m) {
            outmsg_t *n = m->next;
            if (!failed) {
                httpd_ws_frame_t f = {.type = HTTPD_WS_TYPE_TEXT, .payload = (uint8_t *)m->data, .len = m->len};
                if (httpd_ws_send_frame_async(s_hd, fd, &f) != ESP_OK) {
                    failed = true;
                    if (fd >= 0) {
                        /* Not httpd_sess_trigger_close(): it closes the session
                         * slot later, and by then the slot may belong to a new
                         * connection. A shutdown makes the server see EOF on
                         * exactly this socket and close it itself. */
                        ESP_LOGW(TAG, "send to fd %d failed, closing", fd);
                        shutdown(fd, SHUT_RDWR);
                    }
                }
            }
            espkb_free(m);
            m = n;
        }
    }
}

static void schedule_flush(void)
{
    if (__atomic_exchange_n(&s_flush_pending, 1, __ATOMIC_SEQ_CST) == 0) {
        if (httpd_queue_work(s_hd, flush_work, NULL) != ESP_OK) {
            s_flush_pending = 0; /* the push task retries */
        }
    }
}

/* Appends a message to a client's outbox. rate_code != EK_NONE applies the
 * "same error at most once per second" rule. */
static void push(int slot, uint32_t gen, const char *text, espkb_err_t rate_code)
{
    if (!text || slot < 0 || slot >= ESPKB_WS_MAX_CLIENTS) {
        return;
    }
    size_t len = strlen(text);
    outmsg_t *m = espkb_malloc(sizeof(outmsg_t) + len);
    if (!m) {
        return;
    }
    m->next = NULL;
    m->len = len;
    memcpy(m->data, text, len);
    int64_t now = now_us();
    bool queued = false;
    lock();
    client_t *c = &s_c[slot];
    if (c->fd >= 0 && c->gen == gen && c->nmsg < MAX_OUTBOX) {
        bool limited = rate_code != EK_NONE && c->err_last_us[rate_code] != 0 &&
                       now - c->err_last_us[rate_code] < ERROR_GAP_US;
        if (!limited) {
            if (rate_code != EK_NONE) {
                c->err_last_us[rate_code] = now;
            }
            if (c->tail) {
                c->tail->next = m;
            } else {
                c->head = m;
            }
            c->tail = m;
            c->nmsg++;
            queued = true;
        }
    }
    unlock();
    if (queued) {
        schedule_flush();
    } else {
        espkb_free(m);
    }
}

static void push_obj(int slot, uint32_t gen, cJSON *obj, espkb_err_t rate_code)
{
    char *s = obj ? cJSON_PrintUnformatted(obj) : NULL;
    cJSON_Delete(obj);
    push(slot, gen, s, rate_code);
    cJSON_free(s);
}

/* Acknowledgement (with id) or error message (without id) for a message/job. */
static void reply(int slot, uint32_t gen, const char *id_json, espkb_err_t err, const char *msg, int index,
                  long offset)
{
    cJSON *o = cJSON_CreateObject();
    if (!o) {
        return;
    }
    if (id_json) {
        cJSON_AddItemToObject(o, "ack", cJSON_CreateRaw(id_json));
        cJSON_AddBoolToObject(o, "ok", err == EK_NONE);
        if (err != EK_NONE) {
            json_add_error(o, err, msg, index, offset);
        }
        push_obj(slot, gen, o, EK_NONE);
    } else if (err != EK_NONE) {
        json_add_error(o, err, msg, index, offset);
        push_obj(slot, gen, o, err);
    } else {
        cJSON_Delete(o);
    }
}

static char *status_message(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *st = status_build(false);
    if (!root || !st) {
        cJSON_Delete(root);
        cJSON_Delete(st);
        return NULL;
    }
    cJSON_AddItemToObject(root, "status", st);
    char *s = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return s;
}

/* ---------- job completion (engine task or release caller) ---------- */

static void ws_job_done(job_t *job)
{
    wsjob_ctx_t *ctx = job->ctx;
    if (ctx->id_json || job->result != EK_NONE) {
        reply(ctx->slot, ctx->gen, ctx->id_json, job->result, job->fail_msg, job->fail_index, -1);
    }
    cJSON_free(ctx->id_json);
    espkb_free(ctx);
    job_free(job);
}

/* ---------- receiving (HTTP server task) ---------- */

static void handle_message(int slot, uint32_t gen, uint32_t owner, char *text, size_t len)
{
    parse_err_t pe = {.index = -1, .offset = -1};
    char *id_json = NULL;
    job_t *job = NULL;
    keysim_t sim;
    cJSON *root = json_parse_body(text, len, &pe);
    if (!root) {
        reply(slot, gen, NULL, pe.err, pe.msg, -1, -1);
        return;
    }
    engine_held_sim(&sim);
    if (cJSON_IsObject(root)) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "id");
        if (id && !cJSON_IsNumber(id) && !cJSON_IsString(id)) {
            reply(slot, gen, NULL, EK_BAD_REQUEST, "id must be a number or a string", -1, -1);
            goto out;
        }
        if (id) {
            id_json = cJSON_PrintUnformatted(id);
        }
        const cJSON *ping = cJSON_GetObjectItemCaseSensitive(root, "ping");
        if (ping) {
            cJSON *o = cJSON_CreateObject();
            if (o) {
                cJSON_AddItemToObject(o, "pong", cJSON_Duplicate(ping, true));
                cJSON_AddNumberToObject(o, "t", (double)(now_us() / 1000));
                push_obj(slot, gen, o, EK_NONE);
            }
            goto out;
        }
        const cJSON *steps = cJSON_GetObjectItemCaseSensitive(root, "steps");
        const cJSON *rel = cJSON_GetObjectItemCaseSensitive(root, "release");
        if (!steps && rel) {
            if (!cJSON_IsTrue(rel)) {
                reply(slot, gen, id_json, EK_BAD_REQUEST, "release must be true", -1, -1);
                goto out;
            }
            int n = engine_release_all();
            if (id_json) {
                cJSON *o = cJSON_CreateObject();
                if (o) {
                    cJSON_AddItemToObject(o, "ack", cJSON_CreateRaw(id_json));
                    cJSON_AddBoolToObject(o, "ok", true);
                    cJSON_AddNumberToObject(o, "aborted", n);
                    push_obj(slot, gen, o, EK_NONE);
                }
            }
            goto out;
        }
        job = steps ? job_from_array(steps, &sim, &pe) : job_from_step(root, STEP_MASK_ALL, &sim, &pe);
    } else if (cJSON_IsArray(root)) {
        job = job_from_array(root, &sim, &pe);
    } else {
        pe.err = EK_BAD_REQUEST;
        snprintf(pe.msg, sizeof(pe.msg), "message must be a JSON object or array");
    }
    if (!job) {
        reply(slot, gen, id_json, pe.err, pe.msg, pe.index, pe.offset);
        goto out;
    }
    if (!usb_hid_mounted()) {
        job_free(job);
        reply(slot, gen, id_json, EK_USB_NOT_READY, NULL, -1, -1);
        goto out;
    }
    wsjob_ctx_t *ctx = espkb_calloc(1, sizeof(wsjob_ctx_t));
    if (!ctx) {
        job_free(job);
        reply(slot, gen, id_json, EK_BUSY, "out of memory", -1, -1);
        goto out;
    }
    ctx->slot = slot;
    ctx->gen = gen;
    ctx->id_json = id_json;
    id_json = NULL;
    job->owner = owner;
    job->done = ws_job_done;
    job->ctx = ctx;
    uint32_t jid;
    espkb_err_t e = engine_submit(job, &jid);
    if (e) {
        reply(slot, gen, ctx->id_json, e, NULL, -1, -1);
        cJSON_free(ctx->id_json);
        espkb_free(ctx);
        job_free(job);
    }
out:
    cJSON_free(id_json);
    cJSON_Delete(root);
}

static int find_slot(int fd, uint32_t *owner, uint32_t *gen)
{
    int slot = -1;
    lock();
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        if (s_c[i].fd == fd) {
            slot = i;
            *owner = s_c[i].owner;
            *gen = s_c[i].gen;
            break;
        }
    }
    unlock();
    return slot;
}

static esp_err_t ws_handler_inner(httpd_req_t *req)
{
    if (req->method == HTTP_GET) {
        /* A plain GET without the Upgrade handshake. */
        espkb_err_t e = http_access_check(req);
        if (e) {
            return http_send_error(req, e, NULL, -1, -1, 0);
        }
        return http_send_error(req, EK_BAD_REQUEST, "WebSocket upgrade required", -1, -1, 0);
    }
    int fd = httpd_req_to_sockfd(req);
    httpd_ws_frame_t f = {0};
    if (httpd_ws_recv_frame(req, &f, 0) != ESP_OK) {
        return ESP_FAIL;
    }
    uint32_t owner = 0, gen = 0;
    int slot = find_slot(fd, &owner, &gen);
    if (slot < 0) {
        return ESP_FAIL;
    }
    engine_owner_touch(owner); /* any message counts as keep-alive */
    if (f.len > ESPKB_WS_MAX_FRAME) {
        /* Drain a moderately sized payload so the close is graceful (an
         * unread receive buffer would turn our close into a TCP reset). */
        if (f.len <= ESPKB_MAX_BODY) {
            uint8_t chunk[512];
            f.payload = chunk;
            while (f.left_len > 0) {
                if (httpd_ws_recv_frame_part(req, &f, sizeof(chunk)) != ESP_OK) {
                    return ESP_FAIL;
                }
            }
        }
        static const char err[] = "{\"error\":\"too_large\",\"message\":\"frame larger than 8192 bytes\"}";
        httpd_ws_frame_t e = {.type = HTTPD_WS_TYPE_TEXT, .payload = (uint8_t *)err, .len = sizeof(err) - 1};
        httpd_ws_send_frame(req, &e);
        uint8_t code[2] = {0x03, 0xF1}; /* 1009 message too big */
        httpd_ws_frame_t c = {.type = HTTPD_WS_TYPE_CLOSE, .payload = code, .len = 2};
        httpd_ws_send_frame(req, &c);
        return ESP_FAIL;
    }
    uint8_t *buf = espkb_malloc(f.len + 1);
    if (!buf) {
        return ESP_FAIL;
    }
    if (f.len > 0) {
        f.payload = buf;
        if (httpd_ws_recv_frame(req, &f, f.len) != ESP_OK) {
            espkb_free(buf);
            return ESP_FAIL;
        }
    }
    buf[f.len] = '\0';
    if (f.type == HTTPD_WS_TYPE_TEXT && f.final) {
        handle_message(slot, gen, owner, (char *)buf, f.len);
    } else if (f.type != HTTPD_WS_TYPE_PONG) {
        reply(slot, gen, NULL, EK_BAD_REQUEST, "only unfragmented text frames are supported", -1, -1);
    }
    espkb_free(buf);
    return ESP_OK;
}

static esp_err_t ws_handler(httpd_req_t *req)
{
    http_guard_reset(req);
    esp_err_t e = ws_handler_inner(req);
    http_guard_reset(req);
    return e;
}

static esp_err_t ws_pre_handshake(httpd_req_t *req)
{
    espkb_err_t e = http_access_check(req);
    if (e) {
        http_send_error(req, e, NULL, -1, -1, 0);
        return ESP_FAIL;
    }
    if (ws_client_count() >= ESPKB_WS_MAX_CLIENTS) {
        http_send_error(req, EK_BUSY, "too many WebSocket connections (max 4)", -1, -1, 0);
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t ws_post_handshake(httpd_req_t *req)
{
    http_guard_reset(req);
    int fd = httpd_req_to_sockfd(req);
    uint32_t owner = engine_owner_open();
    if (!owner) {
        return ESP_FAIL;
    }
    int slot = -1;
    uint32_t gen = 0;
    lock();
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        if (s_c[i].fd < 0) {
            slot = i;
            break;
        }
    }
    if (slot >= 0) {
        client_t *c = &s_c[slot];
        free_outbox(c);
        memset(c->err_last_us, 0, sizeof(c->err_last_us));
        c->fd = fd;
        c->owner = owner;
        c->gen = gen = ++s_gen_counter;
    }
    unlock();
    if (slot < 0) {
        engine_owner_close(owner);
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "client connected (fd %d)", fd);
    char *st = status_message();
    push(slot, gen, st, EK_NONE);
    cJSON_free(st);
    espkb_status_changed();
    return ESP_OK;
}

void ws_on_close(int fd)
{
    uint32_t owner = 0;
    if (!s_mx) {
        return;
    }
    lock();
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        if (s_c[i].fd == fd) {
            owner = s_c[i].owner;
            free_outbox(&s_c[i]);
            s_c[i].fd = -1;
            s_c[i].owner = 0;
            s_c[i].gen = ++s_gen_counter;
            break;
        }
    }
    unlock();
    if (owner) {
        ESP_LOGI(TAG, "client disconnected (fd %d)", fd);
        engine_owner_close(owner); /* releases everything it held */
        espkb_status_changed();
    }
}

/* ---------- status pushes ---------- */

static void push_task(void *arg)
{
    (void)arg;
    int64_t last_push = 0;
    for (;;) {
        int64_t now = now_us();
        int64_t since = now - last_push;
        TickType_t wait = pdMS_TO_TICKS(100);
        if (s_status_dirty && since < STATUS_MIN_GAP_US) {
            wait = pdMS_TO_TICKS((STATUS_MIN_GAP_US - since) / 1000 + 1);
        }
        ulTaskNotifyTake(pdTRUE, wait);
        now = now_us();
        int clients = ws_client_count();
        if (clients == 0) {
            s_status_dirty = false;
            continue;
        }
        bool due = now - last_push >= STATUS_PERIOD_US ||
                   (s_status_dirty && now - last_push >= STATUS_MIN_GAP_US);
        if (due) {
            s_status_dirty = false;
            last_push = now;
            char *st = status_message();
            if (st) {
                for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
                    lock();
                    bool live = s_c[i].fd >= 0;
                    uint32_t gen = s_c[i].gen;
                    unlock();
                    if (live) {
                        push(i, gen, st, EK_NONE);
                    }
                }
                cJSON_free(st);
            }
        }
        /* Retry a flush whose httpd_queue_work() failed. */
        bool pending = false;
        lock();
        for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
            pending |= s_c[i].head != NULL;
        }
        unlock();
        if (pending && !s_flush_pending) {
            schedule_flush();
        }
    }
}

void ws_init(httpd_handle_t hd)
{
    s_hd = hd;
    s_mx = xSemaphoreCreateMutex();
    configASSERT(s_mx);
    for (int i = 0; i < ESPKB_WS_MAX_CLIENTS; i++) {
        s_c[i].fd = -1;
    }
    BaseType_t ok = xTaskCreate(push_task, "ws_push", 4096, NULL, 5, &s_push_task);
    configASSERT(ok == pdPASS);
    (void)ok;
}

esp_err_t ws_register(httpd_handle_t hd)
{
    static const httpd_uri_t uri = {
        .uri = "/ws",
        .method = HTTP_GET,
        .handler = ws_handler,
        .is_websocket = true,
        .handle_ws_control_frames = false,
        .ws_pre_handshake_cb = ws_pre_handshake,
        .ws_post_handshake_cb = ws_post_handshake,
    };
    return httpd_register_uri_handler(hd, &uri);
}
