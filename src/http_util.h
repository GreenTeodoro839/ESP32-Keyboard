/* Shared HTTP helpers: JSON responses, access control (API.md §2), body reading. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"
#include "esp_http_server.h"
#include "espkb.h"

/* Sends a JSON response; consumes obj (may be NULL -> generic error). */
esp_err_t http_send_obj(httpd_req_t *req, espkb_err_t status, cJSON *obj);
/* Sends {"ok":false,"error":..,"message":..[,"index"][,"offset"][,"job"]}. index/offset < 0 omitted, job 0 omitted. */
esp_err_t http_send_error(httpd_req_t *req, espkb_err_t err, const char *msg, int index, long offset, uint32_t job);
/* Builds the error object used by HTTP and WebSocket replies (without "ok"). */
void json_add_error(cJSON *obj, espkb_err_t err, const char *msg, int index, long offset);

/* Origin check, then token or Host check. Returns EK_NONE, EK_FORBIDDEN or EK_UNAUTHORIZED. */
espkb_err_t http_access_check(httpd_req_t *req);

typedef enum { BODY_OK = 0, BODY_TOO_LARGE, BODY_FAILED } body_result_t;
/* Reads the whole body (<= ESPKB_MAX_BODY) into a NUL-terminated buffer
 * freed with espkb_free(). BODY_FAILED means the client stalled or vanished:
 * close the connection without a response. */
body_result_t http_read_body(httpd_req_t *req, char **out, size_t *len);

/* If the request announced a body with "Expect: 100-continue", sends the
 * interim "100 Continue" response and returns true. */
bool http_expect_continue(httpd_req_t *req);

/* Sends an error for a request whose body has not been read and returns the
 * handler's result. A small body is left to the server to discard; a large
 * one is drained with a bound and the connection closed (ESP_FAIL). */
esp_err_t http_reject(httpd_req_t *req, espkb_err_t err, const char *msg);

/* Sends 413 and makes the server close the connection (returns ESP_FAIL). */
esp_err_t http_reject_too_large(httpd_req_t *req);
/* Reads and discards an unread request body (up to 512 KB, at most 3 s) so a
 * following close is graceful. Returns true if the whole body was consumed. */
bool http_drain_body(httpd_req_t *req);

/* True if the query string has key=1/true/yes. */
bool http_query_flag(httpd_req_t *req, const char *key);
/* Like http_query_flag, but returns def when the key is absent and false for any other value. */
bool http_query_bool(httpd_req_t *req, const char *key, bool def);
/* True if the request's Content-Type starts with the given type. */
bool http_content_type_is(httpd_req_t *req, const char *type);

/* Slow-client guard. Every blocking read waits at most 700 ms and the waiting
 * time per request (or WebSocket frame) is capped at 3 s, so a client that
 * stalls or trickles mid-request can only hold the single server task briefly
 * before it is disconnected. http_guard_open() is the server's open_fn hook;
 * http_guard_reset() is called when a handler starts and finishes. */
esp_err_t http_guard_open(httpd_handle_t hd, int fd);
void http_guard_reset(httpd_req_t *req);
