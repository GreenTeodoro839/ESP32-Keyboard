#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "espkb.h"
#include "hid_engine.h"
#include "http_util.h"
#include "ota.h"
#include "wifi_mgr.h"

#ifndef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
/* Without it a new image is never on probation and a broken one would stay. An
 * sdkconfig.<env> generated before sdkconfig.defaults enabled the option keeps
 * it off: delete that (generated) file and build again. */
#error "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is off: delete the stale sdkconfig.esp32-s3-devkitc-1 and rebuild"
#endif

static const char *TAG = "ota";

/* One flash erase block per write: esp_ota_write() then erases whole 64 KB
 * blocks, which is several times faster than erasing 4 KB sectors. */
#define OTA_BLOCK_SIZE     65536
#define OTA_HEAD_SIZE      (sizeof(esp_image_header_t) + sizeof(esp_image_segment_header_t) + sizeof(esp_app_desc_t))
#define OTA_STALL_US       (15 * 1000000LL)   /* upload fails after this long without a byte */
#define OTA_DRAIN_STALL_US (3 * 1000000LL)    /* same while discarding the rest of a rejected image */
#define OTA_REBOOT_MS      1000               /* lets the response and a last status push go out */
#define CONFIRM_SETTLE_US  (10 * 1000000LL)   /* network up this long -> new image confirmed */
#define CONFIRM_TIMEOUT_US (120 * 1000000LL)  /* no network for this long -> back to the old image */

typedef enum { OTA_IDLE, OTA_RECEIVING, OTA_VERIFYING, OTA_DONE, OTA_FAILED } ota_state_t;
static const char *const STATE_NAMES[] = {"idle", "receiving", "verifying", "done", "failed"};

static struct {
    SemaphoreHandle_t mx;
    bool busy;              /* an upload owns the inactive slot */
    ota_state_t state;
    uint32_t received;
    uint32_t total;
    char message[128];      /* why the last upload failed */
    bool pending_verify;    /* the running image has not been confirmed yet */
} S;

typedef struct {
    httpd_req_t *req;       /* detached request; NULL = detaching failed, just clean up */
    const esp_partition_t *part;
    char *buf;
    size_t total;
    bool reboot;
} upload_t;

static void lock(void) { xSemaphoreTake(S.mx, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(S.mx); }

static void set_state(ota_state_t st, uint32_t received, const char *msg)
{
    lock();
    S.state = st;
    S.received = received;
    snprintf(S.message, sizeof(S.message), "%s", msg ? msg : "");
    unlock();
    espkb_status_changed();
}

/* ---------- probation of a new image ---------- */

static void confirm_running(const char *why)
{
    lock();
    bool pending = S.pending_verify;
    S.pending_verify = false;
    unlock();
    if (pending) {
        esp_err_t e = esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGW(TAG, "new firmware confirmed (%s): %s", why, esp_err_to_name(e));
    }
}

static void confirm_task(void *arg)
{
    (void)arg;
    int64_t t0 = now_us();
    int64_t up_since = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
        lock();
        bool pending = S.pending_verify;
        unlock();
        if (!pending) {
            break; /* confirmed by an update request */
        }
        wifi_status_t w;
        wifi_mgr_get_status(&w);
        int64_t now = now_us();
        if (w.sta_connected || w.ap_active) {
            if (!up_since) {
                up_since = now;
            }
            if (now - up_since >= CONFIRM_SETTLE_US) {
                confirm_running("network is up");
                break;
            }
        } else {
            up_since = 0;
            if (now - t0 >= CONFIRM_TIMEOUT_US) {
                ESP_LOGE(TAG, "new firmware has no network after %d s: returning to the previous firmware",
                         (int)(CONFIRM_TIMEOUT_US / 1000000));
                engine_release_all();
                esp_err_t e = esp_ota_mark_app_invalid_rollback_and_reboot(); /* returns only on failure */
                ESP_LOGE(TAG, "cannot go back (%s); staying on this firmware, unconfirmed", esp_err_to_name(e));
                break;
            }
        }
    }
    vTaskDelete(NULL);
}

const char *ota_running_partition(void)
{
    const esp_partition_t *run = esp_ota_get_running_partition();
    return run ? run->label : "";
}

bool ota_in_progress(void)
{
    lock();
    bool busy = S.state == OTA_RECEIVING || S.state == OTA_VERIFYING;
    unlock();
    return busy;
}

void ota_init(void)
{
    S.mx = xSemaphoreCreateMutex();
    configASSERT(S.mx);
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        S.pending_verify = true;
        ESP_LOGW(TAG, "first boot of a new firmware in %s: confirmed once the network has been up for %d s",
                 run->label, (int)(CONFIRM_SETTLE_US / 1000000));
        BaseType_t ok = xTaskCreate(confirm_task, "ota_confirm", 4096, NULL, 2, NULL);
        configASSERT(ok == pdPASS);
        (void)ok;
    }
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    ESP_LOGI(TAG, "running from %s, update slot %s", ota_running_partition(), next ? next->label : "none");
}

/* ---------- upload ---------- */

/* Reads exactly `want` body bytes. False if no byte arrived for stall_us or the connection failed. */
static bool recv_block(httpd_req_t *req, char *buf, size_t want, int64_t stall_us)
{
    size_t got = 0;
    int64_t last = now_us();
    while (got < want) {
        int r = httpd_req_recv(req, buf + got, want - got);
        if (r > 0) {
            got += (size_t)r;
            last = now_us();
        } else if (r != HTTPD_SOCK_ERR_TIMEOUT || now_us() - last >= stall_us) {
            return false;
        }
        /* The slow-client guard budgets 3 s of waiting per request; an upload
         * is long, so the budget restarts and stall_us is the limit instead. */
        http_guard_reset(req);
    }
    return true;
}

/* Checks the start of an image; returns NULL if it may be flashed, else the reason. */
static const char *check_head(const char *p, esp_app_desc_t *desc)
{
    esp_image_header_t ih;
    memcpy(&ih, p, sizeof(ih));
    memcpy(desc, p + sizeof(esp_image_header_t) + sizeof(esp_image_segment_header_t), sizeof(*desc));
    if (ih.magic != ESP_IMAGE_HEADER_MAGIC || desc->magic_word != ESP_APP_DESC_MAGIC_WORD) {
        return "not an ESP application image (send the build's firmware.bin)";
    }
    desc->version[sizeof(desc->version) - 1] = '\0';
    desc->project_name[sizeof(desc->project_name) - 1] = '\0';
    if (strcmp(desc->project_name, ESPKB_NAME) != 0) {
        return "image is not an " ESPKB_NAME " firmware";
    }
    switch (esp_ota_check_image_validity(ESP_PARTITION_TYPE_APP, &ih, desc)) {
    case ESP_OK:
        return NULL;
    case ESP_ERR_OTA_SPI_MODE_MISMATCH:
        return "image is built for a different SPI flash mode";
    default:
        return "image is built for a different chip or chip revision";
    }
}

/* Receives, flashes and verifies the image, then answers the request. Returns true if the device should reboot. */
static bool run_upload(upload_t *u)
{
    httpd_req_t *req = u->req;
    espkb_err_t err = EK_NONE;
    char msg[128] = "";
    char version[sizeof(((esp_app_desc_t *)0)->version)] = "";
    esp_ota_handle_t h = 0;
    bool begun = false;
    bool conn_ok = true;
    size_t got = 0;

    ESP_LOGW(TAG, "receiving %u bytes for %s", (unsigned)u->total, u->part->label);
    while (!err && got < u->total) {
        size_t n = u->total - got < OTA_BLOCK_SIZE ? u->total - got : OTA_BLOCK_SIZE;
        if (!recv_block(req, u->buf, n, OTA_STALL_US)) {
            conn_ok = false;
            err = EK_BAD_REQUEST;
            snprintf(msg, sizeof(msg), "upload interrupted after %u of %u bytes", (unsigned)got, (unsigned)u->total);
            break;
        }
        esp_err_t e = ESP_OK;
        if (got == 0) {
            esp_app_desc_t desc;
            const char *why = check_head(u->buf, &desc);
            if (why) {
                err = EK_BAD_REQUEST;
                snprintf(msg, sizeof(msg), "%s", why);
            } else {
                snprintf(version, sizeof(version), "%s", desc.version);
                e = esp_ota_begin(u->part, OTA_WITH_SEQUENTIAL_WRITES, &h);
                begun = e == ESP_OK;
                if (!begun) {
                    err = EK_INTERNAL;
                    snprintf(msg, sizeof(msg), "cannot start the update: %s", esp_err_to_name(e));
                }
            }
        }
        if (!err) {
            e = esp_ota_write(h, u->buf, n);
            if (e != ESP_OK) {
                err = EK_INTERNAL;
                snprintf(msg, sizeof(msg), "writing flash failed at offset %u: %s", (unsigned)got, esp_err_to_name(e));
            }
        }
        got += n;
        if (!err) {
            set_state(OTA_RECEIVING, (uint32_t)got, NULL);
        }
    }
    if (err && conn_ok) {
        /* Take the rest of the body off the wire: answering (and closing) with
         * unread data pending resets the connection and the client would
         * never see why the image was refused. */
        while (got < u->total) {
            size_t n = u->total - got < OTA_BLOCK_SIZE ? u->total - got : OTA_BLOCK_SIZE;
            if (!recv_block(req, u->buf, n, OTA_DRAIN_STALL_US)) {
                conn_ok = false;
                break;
            }
            got += n;
        }
    }
    if (!err) {
        set_state(OTA_VERIFYING, (uint32_t)got, NULL);
        esp_err_t e = esp_ota_end(h); /* checks the image's checksum and SHA-256; releases the handle */
        begun = false;
        if (e == ESP_OK) {
            e = esp_ota_set_boot_partition(u->part);
        }
        if (e == ESP_ERR_OTA_VALIDATE_FAILED) {
            err = EK_BAD_REQUEST;
            snprintf(msg, sizeof(msg), "image verification failed (corrupt or incomplete file)");
        } else if (e != ESP_OK) {
            err = EK_INTERNAL;
            snprintf(msg, sizeof(msg), "cannot activate the new image: %s", esp_err_to_name(e));
        }
    }
    if (begun) {
        esp_ota_abort(h);
    }

    if (!err) {
        ESP_LOGW(TAG, "firmware %s (%u bytes) written to %s%s", version, (unsigned)got, u->part->label,
                 u->reboot ? ", rebooting" : "; it starts at the next reboot");
        set_state(OTA_DONE, (uint32_t)got, NULL);
        cJSON *o = cJSON_CreateObject();
        if (o) {
            cJSON_AddBoolToObject(o, "ok", true);
            cJSON_AddNumberToObject(o, "bytes", (double)got);
            cJSON_AddStringToObject(o, "partition", u->part->label);
            cJSON_AddStringToObject(o, "version", version);
            cJSON_AddBoolToObject(o, "reboot", u->reboot);
        }
        http_send_obj(req, EK_NONE, o);
    } else {
        ESP_LOGE(TAG, "update failed: %s", msg);
        set_state(OTA_FAILED, (uint32_t)got, msg);
        if (!conn_ok) {
            httpd_resp_set_hdr(req, "Connection", "close");
        }
        http_send_error(req, err, msg, -1, -1, 0);
    }
    if (!conn_ok) {
        /* Part of the body may still arrive; it must not be parsed as a request. */
        httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));
    }
    httpd_req_async_handler_complete(req);
    return !err && u->reboot;
}

static void upload_task(void *arg)
{
    upload_t *u = arg;
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY); /* until the handler has detached the request */
    if (u->req && run_upload(u)) {
        engine_release_all();
        vTaskDelay(pdMS_TO_TICKS(OTA_REBOOT_MS));
        esp_restart();
    }
    espkb_free(u->buf);
    free(u);
    lock();
    S.busy = false;
    unlock();
    vTaskDelete(NULL);
}

esp_err_t ota_handle_upload(httpd_req_t *req)
{
    size_t len = req->content_len;
    char msg[128];
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) {
        return http_reject(req, EK_INTERNAL,
                           "no OTA slot in the partition table: flash this firmware once over UART (pio run -t upload)");
    }
    if (len < OTA_HEAD_SIZE) {
        return http_reject(req, EK_BAD_REQUEST,
                           "send the firmware image (firmware.bin) as the raw request body with a Content-Length");
    }
    if (len > part->size) {
        snprintf(msg, sizeof(msg), "image of %u bytes does not fit the %u byte firmware slot", (unsigned)len,
                 (unsigned)part->size);
        return http_reject(req, EK_TOO_LARGE, msg);
    }
    lock();
    bool busy = S.busy;
    S.busy = true;
    unlock();
    if (busy) {
        return http_reject(req, EK_BUSY, "another firmware update is in progress");
    }

    upload_t *u = calloc(1, sizeof(*u));
    TaskHandle_t task = NULL;
    if (u) {
        u->buf = espkb_malloc(OTA_BLOCK_SIZE);
        u->part = part;
        u->total = len;
        u->reboot = http_query_bool(req, "reboot", true);
    }
    if (!u || !u->buf || xTaskCreate(upload_task, "ota", 8192, u, 5, &task) != pdPASS) {
        if (u) {
            espkb_free(u->buf);
            free(u);
        }
        lock();
        S.busy = false;
        unlock();
        return http_reject(req, EK_INTERNAL, "out of memory");
    }
    /* From here on the task owns u and clears S.busy. */
    if (httpd_req_async_handler_begin(req, &u->req) != ESP_OK) {
        u->req = NULL;
        xTaskNotifyGive(task);
        return http_reject(req, EK_INTERNAL, "out of memory");
    }
    /* Whoever can ask for an update over HTTP can reach this firmware. */
    confirm_running("update requested");
    lock();
    S.state = OTA_RECEIVING;
    S.received = 0;
    S.total = (uint32_t)len;
    S.message[0] = '\0';
    unlock();
    espkb_status_changed();
    xTaskNotifyGive(task);
    return ESP_OK;
}

/* ---------- information ---------- */

static void build_string(char *out, size_t len)
{
    const esp_app_desc_t *d = esp_app_get_description();
    snprintf(out, len, "%s %s", d->date, d->time);
}

void ota_add_device_info(cJSON *device)
{
    /* The first 8 bytes of the app's ELF SHA-256 identify a build; the same
     * bytes are in the image file's descriptor, so a client can tell whether
     * the device runs the image it uploaded. (The build time does not change
     * on every build.) */
    const esp_app_desc_t *d = esp_app_get_description();
    char build[40];
    char sha[17];
    build_string(build, sizeof(build));
    for (int i = 0; i < 8; i++) {
        snprintf(sha + 2 * i, 3, "%02x", d->app_elf_sha256[i]);
    }
    cJSON_AddStringToObject(device, "build", build);
    cJSON_AddStringToObject(device, "sha256", sha);
    cJSON_AddStringToObject(device, "partition", ota_running_partition());
}

static cJSON *add_update_state(cJSON *parent, const char *name, bool with_message)
{
    cJSON *o = cJSON_AddObjectToObject(parent, name);
    if (o) {
        lock();
        cJSON_AddStringToObject(o, "state", STATE_NAMES[S.state]);
        cJSON_AddNumberToObject(o, "received", S.received);
        cJSON_AddNumberToObject(o, "total", S.total);
        if (with_message) {
            cJSON_AddStringToObject(o, "message", S.message);
        }
        unlock();
    }
    return o;
}

void ota_add_status(cJSON *root)
{
    add_update_state(root, "ota", false);
}

void ota_add_info(cJSON *obj)
{
    cJSON *run = cJSON_AddObjectToObject(obj, "running");
    if (run) {
        cJSON_AddStringToObject(run, "version", ESPKB_FW_VERSION);
        ota_add_device_info(run);
        cJSON_AddStringToObject(run, "idf", esp_app_get_description()->idf_ver);
        lock();
        bool pending = S.pending_verify;
        unlock();
        cJSON_AddStringToObject(run, "state", pending ? "pending_verify" : "valid");
    }
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    if (next) {
        cJSON *n = cJSON_AddObjectToObject(obj, "next");
        if (n) {
            cJSON_AddStringToObject(n, "partition", next->label);
            cJSON_AddNumberToObject(n, "size", (double)next->size);
        }
    } else {
        cJSON_AddNullToObject(obj, "next");
    }
    add_update_state(obj, "update", true);
}
