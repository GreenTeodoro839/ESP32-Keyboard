#include <ctype.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "espkb.h"

static const struct {
    const char *code;
    int status;
    const char *status_line;
    const char *message;
} s_errors[EK_COUNT] = {
    [EK_NONE]               = {"ok", 200, "200 OK", "ok"},
    [EK_BAD_REQUEST]        = {"bad_request", 400, "400 Bad Request", "malformed request"},
    [EK_BAD_STEP]           = {"bad_step", 400, "400 Bad Request", "invalid step"},
    [EK_UNAUTHORIZED]       = {"unauthorized", 401, "401 Unauthorized", "a valid token is required"},
    [EK_FORBIDDEN]          = {"forbidden", 403, "403 Forbidden", "origin or host check failed"},
    [EK_NOT_FOUND]          = {"not_found", 404, "404 Not Found", "no such path"},
    [EK_METHOD_NOT_ALLOWED] = {"method_not_allowed", 405, "405 Method Not Allowed", "method not allowed for this path"},
    [EK_ABORTED]            = {"aborted", 409, "409 Conflict", "job aborted by release"},
    [EK_TOO_LARGE]          = {"too_large", 413, "413 Payload Too Large", "request too large"},
    [EK_BUSY]               = {"busy", 503, "503 Service Unavailable", "job queue is full"},
    [EK_USB_NOT_READY]      = {"usb_not_ready", 503, "503 Service Unavailable", "USB is not enumerated by a host"},
    [EK_USB_SUSPENDED]      = {"usb_suspended", 503, "503 Service Unavailable", "host is suspended and did not allow remote wakeup"},
    [EK_TIMEOUT]            = {"timeout", 504, "504 Gateway Timeout", "host did not take a report within 1000 ms"},
};

const char *espkb_err_code(espkb_err_t e)
{
    return (e >= 0 && e < EK_COUNT) ? s_errors[e].code : "internal";
}

int espkb_err_http_status(espkb_err_t e)
{
    return (e >= 0 && e < EK_COUNT) ? s_errors[e].status : 500;
}

const char *espkb_err_http_status_line(espkb_err_t e)
{
    return (e >= 0 && e < EK_COUNT) ? s_errors[e].status_line : "500 Internal Server Error";
}

const char *espkb_err_message(espkb_err_t e)
{
    return (e >= 0 && e < EK_COUNT) ? s_errors[e].message : "internal error";
}

void *espkb_malloc(size_t size)
{
    if (size == 0) {
        size = 1;
    }
    return heap_caps_malloc_prefer(size, 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT, MALLOC_CAP_DEFAULT);
}

void *espkb_calloc(size_t n, size_t size)
{
    if (n == 0 || size == 0) {
        n = 1;
        size = 1;
    }
    return heap_caps_calloc_prefer(n, size, 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT, MALLOC_CAP_DEFAULT);
}

void espkb_free(void *p)
{
    heap_caps_free(p);
}

const char *json_prescan(char *s, size_t len, int max_depth)
{
    int depth = 0;
    bool in_str = false;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (in_str) {
            if (c == '\\') {
                if (i + 1 < len && s[i + 1] == 'u' && i + 5 < len &&
                    s[i + 2] == '0' && s[i + 3] == '0' && s[i + 4] == '0' && s[i + 5] == '0') {
                    s[i + 5] = '1';
                }
                i++; /* skip the escaped character */
            } else if (c == '"') {
                in_str = false;
            }
        } else if (c == '"') {
            in_str = true;
        } else if (c == '[' || c == '{') {
            if (++depth > max_depth) {
                return "JSON nested too deeply";
            }
        } else if (c == ']' || c == '}') {
            depth--;
        }
    }
    return NULL;
}

bool espkb_token_equal(const char *a, size_t alen, const char *b)
{
    size_t blen = strlen(b);
    unsigned diff = (unsigned)(alen ^ blen);
    for (size_t i = 0; i < blen; i++) {
        unsigned char ca = (i < alen) ? (unsigned char)a[i] : 0;
        diff |= ca ^ (unsigned char)b[i];
    }
    return diff == 0 && blen > 0;
}

bool str_ieq(const char *a, const char *b)
{
    while (*a && *b) {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) {
            return false;
        }
        a++;
        b++;
    }
    return *a == *b;
}

bool strn_ieq(const char *a, size_t alen, const char *b)
{
    size_t i = 0;
    for (; i < alen; i++) {
        if (b[i] == '\0' || tolower((unsigned char)a[i]) != tolower((unsigned char)b[i])) {
            return false;
        }
    }
    return b[i] == '\0';
}
