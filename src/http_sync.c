#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hid_engine.h"
#include "http_sync.h"
#include "http_util.h"

static const char *TAG = "http_sync";

/* Leaves sockets free for quick requests, /api/release and WebSockets. */
#define MAX_PENDING        20
#define LIVENESS_PERIOD_US (1000 * 1000LL)

struct sreq {
    sreq_t *next;
    httpd_req_t *areq;      /* detached request; NULL once released early */
    int fd;
    bool done;
    bool is_job;
    espkb_err_t err;
    int fail_index;
    const char *fail_msg;
    char msg[96];
    uint32_t job_id;
    int64_t t_submit_us;
    int64_t t_done_us;
    char *json;
};

static SemaphoreHandle_t s_mx;
static sreq_t *s_list;
static int s_count;
static TaskHandle_t s_task;

static void lock(void) { xSemaphoreTake(s_mx, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_mx); }

sreq_t *http_sync_begin(httpd_req_t *req)
{
    lock();
    if (s_count >= MAX_PENDING) {
        unlock();
        return NULL;
    }
    s_count++;
    unlock();
    sreq_t *s = espkb_calloc(1, sizeof(sreq_t));
    httpd_req_t *a = NULL;
    if (!s || httpd_req_async_handler_begin(req, &a) != ESP_OK) {
        espkb_free(s);
        lock();
        s_count--;
        unlock();
        return NULL;
    }
    s->areq = a;
    s->fd = httpd_req_to_sockfd(req);
    s->t_submit_us = now_us();
    s->fail_index = -1;
    lock();
    s->next = s_list;
    s_list = s;
    unlock();
    return s;
}

void http_sync_finish_job(sreq_t *s, const job_t *job)
{
    lock();
    s->is_job = true;
    s->err = job->result;
    s->fail_index = job->fail_index;
    s->fail_msg = job->fail_msg;
    s->job_id = job->id;
    s->t_submit_us = job->t_submit_us;
    s->t_done_us = job->t_done_us;
    s->done = true;
    unlock();
    xTaskNotifyGive(s_task);
}

void http_sync_finish_json(sreq_t *s, espkb_err_t err, const char *msg, char *json)
{
    lock();
    s->is_job = false;
    s->err = err;
    snprintf(s->msg, sizeof(s->msg), "%s", msg ? msg : "");
    s->json = json;
    s->done = true;
    unlock();
    xTaskNotifyGive(s_task);
}

static void job_done(job_t *job)
{
    http_sync_finish_job((sreq_t *)job->ctx, job);
    job_free(job);
}

void http_sync_run_job(httpd_req_t *req, job_t *job)
{
    sreq_t *s = http_sync_begin(req);
    if (!s) {
        job_free(job);
        http_send_error(req, EK_BUSY, "too many synchronous requests waiting; retry or use ?async=1", -1, -1, 0);
        return;
    }
    job->done = job_done;
    job->ctx = s;
    uint32_t id;
    espkb_err_t e = engine_submit(job, &id);
    if (e) {
        job_free(job);
        http_sync_finish_json(s, e, NULL, NULL);
    }
}

static void respond(sreq_t *s)
{
    httpd_req_t *r = s->areq;
    if (r) {
        if (s->is_job && s->err == EK_NONE) {
            cJSON *o = cJSON_CreateObject();
            if (o) {
                cJSON_AddBoolToObject(o, "ok", true);
                cJSON_AddNumberToObject(o, "job", s->job_id);
                cJSON_AddNumberToObject(o, "elapsed_ms", (double)((s->t_done_us - s->t_submit_us) / 1000));
            }
            http_send_obj(r, EK_NONE, o);
        } else if (s->is_job) {
            http_send_error(r, s->err, s->fail_msg, s->fail_index, -1, s->job_id);
        } else if (s->err == EK_NONE && s->json) {
            httpd_resp_set_type(r, "application/json");
            httpd_resp_set_hdr(r, "Cache-Control", "no-store");
            httpd_resp_send(r, s->json, HTTPD_RESP_USE_STRLEN);
        } else {
            http_send_error(r, s->err ? s->err : EK_BUSY, s->msg, -1, -1, 0);
        }
        httpd_req_async_handler_complete(r);
    }
    free(s->json);
    espkb_free(s);
}

static bool client_gone(int fd)
{
    char c;
    int r = recv(fd, &c, 1, MSG_PEEK | MSG_DONTWAIT);
    if (r == 0) {
        return true; /* orderly close */
    }
    if (r < 0) {
        return !(errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR);
    }
    return false;
}

static void check_liveness(void)
{
    struct {
        sreq_t *s;
        httpd_req_t *areq;
        int fd;
    } c[MAX_PENDING];
    int n = 0;
    lock();
    for (sreq_t *s = s_list; s && n < MAX_PENDING; s = s->next) {
        if (!s->done && s->areq) {
            c[n].s = s;
            c[n].areq = s->areq;
            c[n].fd = s->fd;
            n++;
        }
    }
    unlock();
    for (int i = 0; i < n; i++) {
        if (!client_gone(c[i].fd)) {
            continue;
        }
        lock();
        bool release = !c[i].s->done && c[i].s->areq == c[i].areq;
        if (release) {
            c[i].s->areq = NULL;
        }
        unlock();
        if (release) {
            /* The job keeps running; only the HTTP side is cleaned up. */
            ESP_LOGI(TAG, "client of a waiting request went away (fd %d)", c[i].fd);
            httpd_req_async_handler_complete(c[i].areq);
        }
    }
}

static void responder_task(void *arg)
{
    (void)arg;
    int64_t last_check = 0;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));
        sreq_t *done = NULL;
        lock();
        for (sreq_t **pp = &s_list; *pp;) {
            sreq_t *s = *pp;
            if (s->done) {
                *pp = s->next;
                s->next = done;
                done = s;
                s_count--;
            } else {
                pp = &s->next;
            }
        }
        unlock();
        while (done) {
            sreq_t *next = done->next;
            respond(done);
            done = next;
        }
        int64_t now = now_us();
        if (now - last_check >= LIVENESS_PERIOD_US) {
            last_check = now;
            check_liveness();
        }
    }
}

void http_sync_init(void)
{
    s_mx = xSemaphoreCreateMutex();
    configASSERT(s_mx);
    BaseType_t ok = xTaskCreate(responder_task, "http_resp", 6144, NULL, 6, &s_task);
    configASSERT(ok == pdPASS);
    (void)ok;
}
