/* Step and job model (API.md §3) and JSON -> job parsing with full validation.
 * A request is parsed and validated completely before anything is queued, so
 * an invalid step never causes partial execution. */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "espkb.h"

typedef enum {
    STEP_TEXT = 0,
    STEP_KEY,
    STEP_MOVE,
    STEP_CLICK,
    STEP_BUTTON,
    STEP_SCROLL,
    STEP_DELAY,
    STEP_RELEASE,
    STEP_MOVE_TO,
    STEP_TYPE_COUNT
} step_type_t;

#define STEP_MASK(t) (1u << (t))
#define STEP_MASK_ALL ((1u << STEP_TYPE_COUNT) - 1)
#define STEP_MASK_MOUSE (STEP_MASK(STEP_MOVE) | STEP_MASK(STEP_MOVE_TO) | STEP_MASK(STEP_CLICK) | \
                         STEP_MASK(STEP_BUTTON) | STEP_MASK(STEP_SCROLL))

typedef enum { ACT_TAP = 0, ACT_DOWN, ACT_UP } step_action_t;

#define STEP_MAX_COMBO 14   /* 8 modifiers + 6 keys */

typedef struct {
    uint8_t type;
    uint8_t action;
    uint8_t nkeys;
    uint8_t keys[STEP_MAX_COMBO];   /* usage codes in press order */
    uint8_t button;                 /* mouse button bit index */
    uint8_t mstep;                  /* move: max displacement per report */
    uint16_t repeat;                /* key tap repetitions */
    uint16_t count;                 /* click count */
    int32_t a, b;                   /* move dx,dy / scroll v,h / move_to x,y (0..32767, already scaled) */
    int32_t hold_ms;                /* -1 = default */
    int32_t delay_ms;               /* -1 = default */
    int32_t interval_ms;            /* click interval */
    int32_t ttl_ms;                 /* 0 = not given */
    uint32_t delay;                 /* delay step */
    char *text;                     /* text step (owned by the step) */
    uint32_t text_len;
} step_t;

typedef struct job job_t;
typedef void (*job_done_cb_t)(job_t *job);

struct job {
    job_t *next;
    uint32_t id;
    uint32_t owner;           /* engine owner id (OWNER_HTTP or a WebSocket owner) */
    uint16_t nsteps;
    step_t *steps;
    int64_t t_submit_us;
    int64_t t_done_us;
    espkb_err_t result;
    int fail_index;           /* step index of a runtime failure, -1 if none */
    const char *fail_msg;     /* static message for runtime failures, may be NULL */
    volatile bool abort;      /* set by engine_release_all() */
    job_done_cb_t done;       /* called exactly once; takes ownership of the job */
    void *ctx;
};

typedef struct {
    espkb_err_t err;
    int index;                /* step index or -1 */
    long offset;              /* byte offset in text or -1 */
    char msg[112];
} parse_err_t;

/* Rollover simulation: set of non-modifier keys held during validation. */
typedef struct {
    uint8_t bits[32];
    int count;
} keysim_t;

job_t *job_alloc(size_t nsteps);
void job_free(job_t *job);

/* Parses a JSON array of steps (max ESPKB_MAX_STEPS). */
job_t *job_from_array(const cJSON *arr, const keysim_t *held, parse_err_t *err);
/* Parses one step object whose type must be in allowed_mask. */
job_t *job_from_step(const cJSON *obj, uint32_t allowed_mask, const keysim_t *held, parse_err_t *err);
/* Builds a text job from raw bytes (text/plain body of /api/type). */
job_t *job_from_text(const char *text, size_t len, const keysim_t *held, parse_err_t *err);

/* Parses a request/message body as JSON with the safety pre-scan applied
 * (which may rewrite the body in place, see json_prescan()).
 * body must be NUL-terminated (body[len] == 0).
 * Returns NULL and fills err on failure. */
cJSON *json_parse_body(char *body, size_t len, parse_err_t *err);
