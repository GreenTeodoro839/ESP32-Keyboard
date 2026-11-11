/* Completion of HTTP requests that wait for something (synchronous jobs, WiFi
 * scans) without holding the HTTP server task: the request is detached with
 * httpd_req_async_handler_begin() and answered later by a responder task.
 * The responder also notices clients that disappeared while waiting and
 * releases their requests early. */
#pragma once

#include "esp_http_server.h"
#include "espkb.h"
#include "steps.h"

typedef struct sreq sreq_t;

void http_sync_init(void);

/* Detaches req. Returns NULL (nothing taken over, caller must respond) if too
 * many requests are already waiting or memory is short. */
sreq_t *http_sync_begin(httpd_req_t *req);

/* Finish a detached request (any task, never blocks). Exactly one call per sreq. */
void http_sync_finish_job(sreq_t *s, const job_t *job);
void http_sync_finish_json(sreq_t *s, espkb_err_t err, const char *msg, char *json /* malloc'd or NULL */);

/* Runs a job synchronously for an HTTP request: always responds (now or when
 * the job ends). Takes ownership of the job. */
void http_sync_run_job(httpd_req_t *req, job_t *job);
