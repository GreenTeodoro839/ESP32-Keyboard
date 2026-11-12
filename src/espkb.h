/* Common definitions shared by all ESPKB modules. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_timer.h"

#define ESPKB_NAME        "espkb"
#define ESPKB_API_VERSION 1
#define ESPKB_FW_VERSION  "1.2.0"   /* also the image version, see the top-level CMakeLists.txt */

#define ESPKB_MAX_BODY      65536   /* HTTP request body limit (API.md §1); POST /api/ota is exempt */
#define ESPKB_MAX_STEPS     512     /* steps per request (API.md §4) */
#define ESPKB_QUEUE_CAP     32      /* job queue capacity (API.md §3.2) */
#define ESPKB_WS_MAX_FRAME  8192    /* WebSocket frame limit (API.md §5) */
#define ESPKB_WS_MAX_CLIENTS 4
#define ESPKB_REPORT_TIMEOUT_MS 1000
#define ESPKB_AP_IP         "192.168.4.1"

/* Error codes of API.md §1. */
typedef enum {
    EK_NONE = 0,
    EK_BAD_REQUEST,
    EK_BAD_STEP,
    EK_UNAUTHORIZED,
    EK_FORBIDDEN,
    EK_NOT_FOUND,
    EK_METHOD_NOT_ALLOWED,
    EK_ABORTED,
    EK_TOO_LARGE,
    EK_BUSY,
    EK_USB_NOT_READY,
    EK_USB_SUSPENDED,
    EK_TIMEOUT,
    EK_INTERNAL,
    EK_COUNT
} espkb_err_t;

const char *espkb_err_code(espkb_err_t e);
int espkb_err_http_status(espkb_err_t e);
const char *espkb_err_http_status_line(espkb_err_t e);
const char *espkb_err_message(espkb_err_t e);

static inline int64_t now_us(void) { return esp_timer_get_time(); }
static inline uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

/* Allocation helpers that prefer PSRAM, keeping internal RAM for WiFi/lwIP. */
void *espkb_malloc(size_t size);
void *espkb_calloc(size_t n, size_t size);
void espkb_free(void *p);

/* Called by any module when something shown in the status changed (USB state,
 * LEDs, WiFi, held keys). Triggers a WebSocket status push. Never blocks. */
void espkb_status_changed(void);

/* Rejects JSON nested deeper than max_depth (protects the stack): returns a
 * static message, or NULL if OK. Also rewrites every escaped NUL ("\u0000")
 * in place to "\u0001": cJSON would silently truncate the string at the NUL,
 * while U+0001 is equally untypeable (a text step then fails with bad_step at
 * the right byte offset) and invalid in every other string field. */
const char *json_prescan(char *s, size_t len, int max_depth);

/* Constant-time comparison of a candidate token with the expected one. */
bool espkb_token_equal(const char *a, size_t alen, const char *b);

/* Case-insensitive ASCII comparisons. */
bool str_ieq(const char *a, const char *b);
bool strn_ieq(const char *a, size_t alen, const char *b);
