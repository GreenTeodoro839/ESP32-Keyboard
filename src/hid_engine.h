/* The HID engine: one task owns all HID output. Every source submits jobs into
 * one FIFO; jobs and their steps run strictly in arrival order and each report
 * waits for the host to take it before the next one is sent. It also owns the
 * held-key state and the stuck-key protection of API.md §6. */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "espkb.h"
#include "steps.h"

#define OWNER_NONE 0
#define OWNER_HTTP 1   /* all HTTP requests share this owner */

typedef struct {
    bool busy;
    int queued;
    uint8_t keys[14];      /* held modifiers (224..231) then held keys */
    int nkeys;
    uint8_t buttons;       /* held mouse buttons, bit per button */
    uint32_t reports_sent;
    uint32_t jobs_done;
    uint32_t jobs_failed;
} engine_status_t;

void engine_init(void);

/* Queues a job. On success the engine owns the job, *job_id is set, and
 * job->done is called exactly once when it finishes (possibly before this
 * function returns). On failure (EK_BUSY) the caller still owns the job. */
espkb_err_t engine_submit(job_t *job, uint32_t *job_id);

/* Emergency stop: aborts the running job, flushes the queue (those jobs
 * finish with EK_ABORTED) and releases every key and button. Returns the
 * number of aborted jobs. Takes effect immediately. */
int engine_release_all(void);

/* WebSocket owners: open on connect, touch on every message, close on disconnect. */
uint32_t engine_owner_open(void);
void engine_owner_close(uint32_t owner);
void engine_owner_touch(uint32_t owner);

/* Every HTTP action request resets the HTTP owner's hold deadline. */
void engine_http_activity(void);

/* Asks the engine to signal remote wakeup (if the host is suspended and allows it). */
void engine_request_wake(void);

void engine_get_status(engine_status_t *st);
/* Current held non-modifier keys, used to validate the 6-key rollover rule. */
void engine_held_sim(keysim_t *sim);
