#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "config.h"
#include "hid_engine.h"
#include "keymap.h"
#include "usb_hid.h"

static const char *TAG = "engine";

#define EV_JOB        (1u << 0)
#define EV_ABORT      (1u << 1)
#define EV_USB        (1u << 2)
#define EV_OWNER      (1u << 3)
#define EV_DONE_KBD   (1u << 4)
#define EV_DONE_MOUSE (1u << 5)
#define EV_WAKE       (1u << 6)
#define EV_DONE_ABS   (1u << 7)
#define EV_ALL        (EV_JOB | EV_ABORT | EV_USB | EV_OWNER | EV_DONE_KBD | EV_DONE_MOUSE | EV_WAKE | EV_DONE_ABS)

#define MAX_WS_OWNERS      8
#define WS_KEEPALIVE_US    (1000 * 1000LL)
#define HTTP_DEFAULT_TTL   5000
#define REPORT_TIMEOUT_US  (ESPKB_REPORT_TIMEOUT_MS * 1000LL)
#define WAKE_TIMEOUT_US    (3000 * 1000LL)
#define HOUSEKEEP_US       (20 * 1000LL)
#define MAX_TRANSIENT      16

typedef struct {
    uint32_t id;           /* 0 = free slot */
    int64_t last_rx_us;
    int64_t last_end_us;
    int pending;           /* jobs queued or running */
} ws_owner_t;

static struct {
    SemaphoreHandle_t lock;
    EventGroupHandle_t ev;

    job_t *queue[ESPKB_QUEUE_CAP];
    int qhead;
    int qcount;
    job_t *current;
    bool release_pending;
    bool wake_requested;
    uint32_t next_job_id;
    uint32_t next_owner_id;

    ws_owner_t ws[MAX_WS_OWNERS];
    int http_pending;
    uint32_t http_ttl_ms;
    int64_t http_deadline_us;

    /* held keys/buttons with their owner */
    uint8_t held_keys[6];
    uint32_t held_key_owner[6];
    int nheld;
    uint32_t mod_owner[8];
    uint32_t btn_owner[MOUSE_BUTTON_COUNT];

    /* transient presses of the running step (tap, text, click) */
    uint8_t tr_mods;
    uint8_t tr_keys[MAX_TRANSIENT];
    int ntr;
    uint8_t tr_btns;

    /* last report submitted per interface, used to detect needed sync reports */
    uint8_t last_kbd[8];
    uint8_t last_btns;
    bool force_sync[2];
    /* Last position reported on the absolute interface. A host starts out at
     * (0, 0) for a newly enumerated device, and so does this. */
    uint16_t abs_x, abs_y;
    uint32_t gen_seen;

    uint32_t jobs_done;
    uint32_t jobs_failed;
} E;

static void lock(void) { xSemaphoreTake(E.lock, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(E.lock); }

/* ---------- owners (call with lock held) ---------- */

static int ws_find(uint32_t id)
{
    if (id <= OWNER_HTTP) {
        return -1;
    }
    for (int i = 0; i < MAX_WS_OWNERS; i++) {
        if (E.ws[i].id == id) {
            return i;
        }
    }
    return -1;
}

static bool owner_alive(uint32_t owner)
{
    return owner == OWNER_HTTP || ws_find(owner) >= 0;
}

static void owner_pending_add(uint32_t owner, int delta, int64_t now)
{
    if (owner == OWNER_HTTP) {
        E.http_pending += delta;
        if (delta < 0) {
            /* The countdown restarts when the owner's last job ends. */
            int64_t d = now + (int64_t)E.http_ttl_ms * 1000;
            if (d > E.http_deadline_us) {
                E.http_deadline_us = d;
            }
        }
        return;
    }
    int i = ws_find(owner);
    if (i >= 0) {
        E.ws[i].pending += delta;
        if (delta < 0) {
            E.ws[i].last_end_us = now;
        }
    }
}

static bool owner_expired(uint32_t owner, int64_t now)
{
    if (owner == OWNER_HTTP) {
        return E.http_pending <= 0 && now >= E.http_deadline_us;
    }
    int i = ws_find(owner);
    if (i < 0) {
        return true; /* connection closed */
    }
    const ws_owner_t *o = &E.ws[i];
    if (o->pending > 0) {
        return false;
    }
    int64_t last = o->last_rx_us > o->last_end_us ? o->last_rx_us : o->last_end_us;
    return now - last >= WS_KEEPALIVE_US;
}

/* ---------- key state (call with lock held) ---------- */

static bool is_mod(uint8_t k) { return k >= HID_MOD_FIRST && k <= HID_MOD_LAST; }

static int held_index(uint8_t k)
{
    for (int i = 0; i < E.nheld; i++) {
        if (E.held_keys[i] == k) {
            return i;
        }
    }
    return -1;
}

static int tr_index(uint8_t k)
{
    for (int i = 0; i < E.ntr; i++) {
        if (E.tr_keys[i] == k) {
            return i;
        }
    }
    return -1;
}

/* Number of distinct non-modifier keys currently in the report. */
static int pressed_count(void)
{
    int n = E.nheld;
    for (int i = 0; i < E.ntr; i++) {
        if (held_index(E.tr_keys[i]) < 0) {
            n++;
        }
    }
    return n;
}

static bool any_held(void)
{
    if (E.nheld) {
        return true;
    }
    for (int i = 0; i < 8; i++) {
        if (E.mod_owner[i]) {
            return true;
        }
    }
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (E.btn_owner[i]) {
            return true;
        }
    }
    return false;
}

static void clear_all_keys(void)
{
    E.nheld = 0;
    memset(E.mod_owner, 0, sizeof(E.mod_owner));
    memset(E.btn_owner, 0, sizeof(E.btn_owner));
    E.tr_mods = 0;
    E.ntr = 0;
    E.tr_btns = 0;
    E.http_ttl_ms = HTTP_DEFAULT_TTL;
}

static void remove_held_at(int i)
{
    memmove(&E.held_keys[i], &E.held_keys[i + 1], (size_t)(E.nheld - i - 1));
    memmove(&E.held_key_owner[i], &E.held_key_owner[i + 1], (size_t)(E.nheld - i - 1) * sizeof(uint32_t));
    E.nheld--;
}

static bool http_holds_anything(void)
{
    for (int i = 0; i < E.nheld; i++) {
        if (E.held_key_owner[i] == OWNER_HTTP) {
            return true;
        }
    }
    for (int i = 0; i < 8; i++) {
        if (E.mod_owner[i] == OWNER_HTTP) {
            return true;
        }
    }
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (E.btn_owner[i] == OWNER_HTTP) {
            return true;
        }
    }
    return false;
}

/* Releases keys of expired owners and handles USB re-enumeration.
 * Returns true if the held state changed. */
static bool housekeeping(void)
{
    bool changed = false;
    uint32_t gen = usb_hid_generation();
    if (gen != E.gen_seen) {
        E.gen_seen = gen;
        if (any_held() || E.ntr || E.tr_mods || E.tr_btns) {
            changed = true;
        }
        clear_all_keys();
        memset(E.last_kbd, 0, sizeof(E.last_kbd));
        E.last_btns = 0;
        E.force_sync[0] = E.force_sync[1] = false;
        E.abs_x = E.abs_y = 0;
    }
    if (!any_held()) {
        return changed;
    }
    int64_t now = now_us();
    for (int i = E.nheld - 1; i >= 0; i--) {
        if (owner_expired(E.held_key_owner[i], now)) {
            remove_held_at(i);
            changed = true;
        }
    }
    for (int i = 0; i < 8; i++) {
        if (E.mod_owner[i] && owner_expired(E.mod_owner[i], now)) {
            E.mod_owner[i] = 0;
            changed = true;
        }
    }
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (E.btn_owner[i] && owner_expired(E.btn_owner[i], now)) {
            E.btn_owner[i] = 0;
            changed = true;
        }
    }
    if (changed && !http_holds_anything()) {
        E.http_ttl_ms = HTTP_DEFAULT_TTL;
    }
    return changed;
}

static espkb_err_t tr_press(uint8_t k)
{
    if (is_mod(k)) {
        E.tr_mods |= (uint8_t)(1u << (k - HID_MOD_FIRST));
        return EK_NONE;
    }
    if (tr_index(k) >= 0) {
        return EK_NONE;
    }
    if (held_index(k) < 0 && pressed_count() >= 6) {
        return EK_BAD_STEP;
    }
    if (E.ntr >= MAX_TRANSIENT) {
        return EK_BAD_STEP;
    }
    E.tr_keys[E.ntr++] = k;
    return EK_NONE;
}

static void tr_release(uint8_t k)
{
    if (is_mod(k)) {
        E.tr_mods &= (uint8_t)~(1u << (k - HID_MOD_FIRST));
        return;
    }
    int i = tr_index(k);
    if (i >= 0) {
        memmove(&E.tr_keys[i], &E.tr_keys[i + 1], (size_t)(E.ntr - i - 1));
        E.ntr--;
    }
}

static espkb_err_t hold_key(uint32_t owner, uint8_t k, bool *changed)
{
    *changed = false;
    if (is_mod(k)) {
        int b = k - HID_MOD_FIRST;
        *changed = E.mod_owner[b] == 0;
        E.mod_owner[b] = owner;
        return EK_NONE;
    }
    int i = held_index(k);
    if (i >= 0) {
        E.held_key_owner[i] = owner;
        return EK_NONE;
    }
    if (tr_index(k) < 0 && pressed_count() >= 6) {
        return EK_BAD_STEP;
    }
    if (E.nheld >= 6) {
        return EK_BAD_STEP;
    }
    E.held_keys[E.nheld] = k;
    E.held_key_owner[E.nheld] = owner;
    E.nheld++;
    *changed = true;
    return EK_NONE;
}

static bool unhold_key(uint8_t k)
{
    if (is_mod(k)) {
        int b = k - HID_MOD_FIRST;
        bool was = E.mod_owner[b] != 0;
        E.mod_owner[b] = 0;
        return was;
    }
    int i = held_index(k);
    if (i < 0) {
        return false;
    }
    remove_held_at(i);
    return true;
}

static void build_kbd(uint8_t r[8])
{
    uint8_t mods = E.tr_mods;
    for (int b = 0; b < 8; b++) {
        if (E.mod_owner[b]) {
            mods |= (uint8_t)(1u << b);
        }
    }
    memset(r, 0, 8);
    r[0] = mods;
    int n = 0;
    for (int i = 0; i < E.nheld && n < 6; i++) {
        r[2 + n++] = E.held_keys[i];
    }
    for (int i = 0; i < E.ntr && n < 6; i++) {
        if (held_index(E.tr_keys[i]) < 0) {
            r[2 + n++] = E.tr_keys[i];
        }
    }
}

static uint8_t cur_buttons(void)
{
    uint8_t b = E.tr_btns;
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (E.btn_owner[i]) {
            b |= (uint8_t)(1u << i);
        }
    }
    return b;
}

static uint16_t build_mouse(uint8_t r[5], int8_t dx, int8_t dy, int8_t wheel, int8_t pan)
{
    r[0] = cur_buttons();
    r[1] = (uint8_t)dx;
    r[2] = (uint8_t)dy;
    r[3] = (uint8_t)wheel;
    r[4] = (uint8_t)pan;
    return usb_hid_boot_protocol(ITF_MOUSE) ? 3 : 5;
}

/* The absolute interface reports the position only. Buttons and wheel stay on
 * the relative mouse interface, so every button has exactly one source and the
 * held-button logic (owners, release, sync) covers them unchanged. */
static uint16_t build_abs(uint8_t r[ABS_REPORT_LEN], uint16_t x, uint16_t y)
{
    r[0] = 0;
    r[1] = (uint8_t)(x & 0xff);
    r[2] = (uint8_t)(x >> 8);
    r[3] = (uint8_t)(y & 0xff);
    r[4] = (uint8_t)(y >> 8);
    r[5] = 0;
    r[6] = 0;
    return ABS_REPORT_LEN;
}

/* ---------- report path (engine task only) ---------- */

static void wait_ev(EventBits_t bits, int64_t max_us)
{
    TickType_t t = max_us <= 0 ? 1 : (TickType_t)((max_us + 999) / 1000);
    if (t == 0) {
        t = 1;
    }
    xEventGroupWaitBits(E.ev, bits, pdTRUE, pdFALSE, t);
}

static void housekeeping_and_notify(void)
{
    lock();
    bool changed = housekeeping();
    unlock();
    if (changed) {
        espkb_status_changed();
    }
}

/* Submits a report and records it as the last one sent on that interface. */
static bool submit(int itf, const uint8_t *r, uint16_t len)
{
    if (!usb_hid_submit(itf, r, len)) {
        return false;
    }
    lock();
    if (itf == ITF_KBD) {
        memcpy(E.last_kbd, r, 8);
        E.force_sync[itf] = false;
    } else if (itf == ITF_MOUSE) {
        E.last_btns = r[0];
        E.force_sync[itf] = false;
    } else {
        E.abs_x = (uint16_t)(r[1] | (r[2] << 8));
        E.abs_y = (uint16_t)(r[3] | (r[4] << 8));
    }
    unlock();
    return true;
}

/* Non-blocking: sends a report reflecting the current state on any idle
 * interface whose last report differs from it (owner timeouts, releases after
 * a failure, resume after suspend) or where a release was forced. The absolute
 * interface holds no state, so there is never anything to sync on it. */
static void try_sync(void)
{
    if (!usb_hid_ready()) {
        return;
    }
    for (int itf = ITF_KBD; itf <= ITF_MOUSE; itf++) {
        if (usb_hid_inflight(itf)) {
            continue;
        }
        uint8_t r[8] = {0};
        uint16_t len;
        bool need;
        lock();
        if (itf == ITF_KBD) {
            build_kbd(r);
            len = 8;
            need = E.force_sync[itf] || memcmp(r, E.last_kbd, 8) != 0;
        } else {
            len = build_mouse(r, 0, 0, 0, 0);
            need = E.force_sync[itf] || r[0] != E.last_btns;
        }
        unlock();
        if (need) {
            submit(itf, r, len);
        }
    }
}

static espkb_err_t job_alive(job_t *job, uint32_t gen0)
{
    if (job && job->abort) {
        return EK_ABORTED;
    }
    if (usb_hid_generation() != gen0 || !usb_hid_mounted()) {
        return EK_USB_NOT_READY;
    }
    return EK_NONE;
}

/* Waits until no report is in flight on itf (the host took it). */
static espkb_err_t wait_idle(job_t *job, uint32_t gen0, int itf)
{
    int64_t deadline = now_us() + REPORT_TIMEOUT_US;
    EventBits_t done = itf == ITF_KBD ? EV_DONE_KBD : itf == ITF_MOUSE ? EV_DONE_MOUSE : EV_DONE_ABS;
    while (usb_hid_inflight(itf)) {
        espkb_err_t e = job_alive(job, gen0);
        if (e) {
            return e;
        }
        int64_t rem = deadline - now_us();
        if (rem <= 0) {
            return EK_TIMEOUT;
        }
        wait_ev(done | EV_ABORT | EV_USB | EV_OWNER, rem < 10000 ? rem : 10000);
        housekeeping_and_notify();
    }
    return job_alive(job, gen0) == EK_USB_NOT_READY ? EK_USB_NOT_READY : EK_NONE;
}

/* If the host is suspended: remote wakeup (if allowed) and wait up to 3 s. */
static espkb_err_t ensure_awake(job_t *job, uint32_t gen0)
{
    if (!usb_hid_suspended()) {
        return EK_NONE;
    }
    if (!usb_hid_remote_wakeup_allowed()) {
        return EK_USB_SUSPENDED;
    }
    usb_hid_remote_wakeup();
    int64_t deadline = now_us() + WAKE_TIMEOUT_US;
    while (usb_hid_suspended()) {
        espkb_err_t e = job_alive(job, gen0);
        if (e) {
            return e;
        }
        if (now_us() >= deadline) {
            return EK_USB_SUSPENDED;
        }
        wait_ev(EV_USB | EV_ABORT, 20000);
    }
    return EK_NONE;
}

/* Sends one report built from the current state and waits for the host to
 * take it (at most 1000 ms). a, b: the relative movement on ITF_MOUSE (int8
 * range), the position on ITF_ABS (0..ABS_MAX); unused on ITF_KBD. */
static espkb_err_t send_report(job_t *job, uint32_t gen0, int itf, int16_t a, int16_t b, int8_t wheel,
                               int8_t pan)
{
    espkb_err_t e = wait_idle(job, gen0, itf);
    if (e) {
        return e;
    }
    int64_t deadline = now_us() + REPORT_TIMEOUT_US;
    for (;;) {
        e = ensure_awake(job, gen0);
        if (e) {
            return e;
        }
        uint8_t r[8] = {0};
        uint16_t len;
        lock();
        if (itf == ITF_KBD) {
            build_kbd(r);
            len = 8;
        } else if (itf == ITF_MOUSE) {
            len = build_mouse(r, (int8_t)a, (int8_t)b, wheel, pan);
        } else {
            len = build_abs(r, (uint16_t)a, (uint16_t)b);
        }
        unlock();
        if (submit(itf, r, len)) {
            break;
        }
        e = job_alive(job, gen0);
        if (e) {
            return e;
        }
        if (now_us() >= deadline) {
            return EK_TIMEOUT;
        }
        wait_ev(EV_ABORT | EV_USB, 1000);
    }
    return wait_idle(job, gen0, itf);
}

#define SEND_KBD()                                                        \
    do {                                                                  \
        espkb_err_t _e = send_report(job, gen0, ITF_KBD, 0, 0, 0, 0);     \
        if (_e) return _e;                                                \
    } while (0)

#define SEND_MOUSE(dx, dy, wh, pan)                                       \
    do {                                                                  \
        espkb_err_t _e = send_report(job, gen0, ITF_MOUSE, dx, dy, wh, pan); \
        if (_e) return _e;                                                \
    } while (0)

#define SEND_ABS(x, y)                                                    \
    do {                                                                  \
        espkb_err_t _e = send_report(job, gen0, ITF_ABS, x, y, 0, 0);     \
        if (_e) return _e;                                                \
    } while (0)

#define SLEEP_MS(ms)                                                      \
    do {                                                                  \
        espkb_err_t _e = sleep_ms(job, gen0, (ms));                       \
        if (_e) return _e;                                                \
    } while (0)

/* Interruptible delay; keeps stuck-key protection running meanwhile. */
static espkb_err_t sleep_ms(job_t *job, uint32_t gen0, int32_t ms)
{
    int64_t deadline = now_us() + (int64_t)ms * 1000;
    for (;;) {
        espkb_err_t e = job_alive(job, gen0);
        if (e) {
            return e;
        }
        housekeeping_and_notify();
        try_sync();
        int64_t rem = deadline - now_us();
        if (rem <= 0) {
            return EK_NONE;
        }
        wait_ev(EV_ABORT | EV_USB | EV_OWNER | EV_DONE_KBD | EV_DONE_MOUSE,
                rem < HOUSEKEEP_US ? rem : HOUSEKEEP_US);
    }
}

static espkb_err_t locked_tr_press(uint8_t k)
{
    lock();
    espkb_err_t e = tr_press(k);
    unlock();
    return e;
}

static void locked_tr_release(uint8_t k)
{
    lock();
    tr_release(k);
    unlock();
}

/* ---------- step execution ---------- */

static int32_t eff_delay(const step_t *st)
{
    return st->delay_ms >= 0 ? st->delay_ms : (int32_t)config_key_delay();
}

static espkb_err_t exec_text(job_t *job, uint32_t gen0, const step_t *st)
{
    int32_t delay = eff_delay(st);
    bool first = true;
    for (uint32_t i = 0; i < st->text_len; i++) {
        uint8_t c = (uint8_t)st->text[i];
        if (c == '\n' && i > 0 && st->text[i - 1] == '\r') {
            continue; /* \r\n counts as one Enter */
        }
        uint8_t usage;
        bool shift;
        if (!keymap_char(c, &usage, &shift)) {
            continue; /* cannot happen: validated at parse time */
        }
        if (keymap_is_letter(c) && (usb_hid_leds() & 0x02)) {
            shift = !shift; /* CapsLock compensation */
        }
        if (!first) {
            SLEEP_MS(delay);
        }
        first = false;
        lock();
        espkb_err_t e = tr_press(usage);
        if (!e && shift) {
            E.tr_mods |= HID_KEY_LSHIFT_BIT;
        }
        unlock();
        if (e) {
            job->fail_msg = "more than 6 non-modifier keys would be held";
            return e;
        }
        SEND_KBD();
        SLEEP_MS(delay);
        lock();
        tr_release(usage);
        E.tr_mods &= (uint8_t)~HID_KEY_LSHIFT_BIT;
        unlock();
        SEND_KBD();
    }
    return EK_NONE;
}

static void set_http_ttl(const step_t *st)
{
    if (st->ttl_ms > 0) {
        lock();
        E.http_ttl_ms = (uint32_t)st->ttl_ms;
        E.http_deadline_us = now_us() + (int64_t)st->ttl_ms * 1000;
        unlock();
    }
}

/* Lock held: true if k is currently held with "down" (as a modifier bit or in the key array). */
static bool is_held(uint8_t k)
{
    return is_mod(k) ? E.mod_owner[k - HID_MOD_FIRST] != 0 : held_index(k) >= 0;
}

/* Lock held: true if a transient press of k would change the keyboard report. */
static bool press_changes_report(uint8_t k)
{
    if (is_mod(k)) {
        return !is_held(k) && !(E.tr_mods & (1u << (k - HID_MOD_FIRST)));
    }
    return !is_held(k) && tr_index(k) < 0;
}

/* Every state change below is made right before its SEND_KBD(), never before a
 * SLEEP_MS(): the sleep runs try_sync(), which would otherwise send the new
 * state early and the SEND_KBD() would then repeat it. */
static espkb_err_t exec_key(job_t *job, uint32_t gen0, const step_t *st)
{
    int32_t delay = eff_delay(st);
    if (st->action == ACT_TAP) {
        int32_t hold = st->hold_ms >= 0 ? st->hold_ms : delay;
        bool sent = false;
        for (int r = 0; r < st->repeat; r++) {
            /* A key that is already held stays held and is neither pressed
             * nor released again (no report that changes nothing). */
            bool pressed[STEP_MAX_COMBO] = {false};
            for (int k = 0; k < st->nkeys; k++) {
                lock();
                bool change = press_changes_report(st->keys[k]);
                unlock();
                if (!change) {
                    continue;
                }
                if (sent) {
                    SLEEP_MS(delay);
                }
                if (locked_tr_press(st->keys[k])) {
                    job->fail_msg = "more than 6 non-modifier keys would be held";
                    return EK_BAD_STEP;
                }
                SEND_KBD();
                sent = true;
                pressed[k] = true;
            }
            bool first = true;
            for (int k = st->nkeys - 1; k >= 0; k--) {
                if (!pressed[k]) {
                    continue;
                }
                SLEEP_MS(first ? hold : delay);
                first = false;
                locked_tr_release(st->keys[k]);
                SEND_KBD();
            }
        }
        return EK_NONE;
    }

    bool first = true;
    if (st->action == ACT_DOWN) {
        lock();
        bool alive = owner_alive(job->owner);
        unlock();
        if (!alive) {
            return EK_NONE; /* its connection is gone: nothing may be held for it */
        }
        for (int k = 0; k < st->nkeys; k++) {
            lock();
            bool already = is_held(st->keys[k]);
            unlock();
            if (!already && !first) {
                SLEEP_MS(delay);
            }
            bool changed;
            lock();
            espkb_err_t e = hold_key(job->owner, st->keys[k], &changed);
            unlock();
            if (e) {
                job->fail_msg = "more than 6 non-modifier keys would be held";
                return e;
            }
            if (changed) {
                espkb_status_changed();
                first = false;
                SEND_KBD();
            }
        }
        if (job->owner == OWNER_HTTP) {
            set_http_ttl(st);
        }
        return EK_NONE;
    }

    for (int k = st->nkeys - 1; k >= 0; k--) {
        lock();
        bool held = is_held(st->keys[k]);
        unlock();
        if (!held) {
            continue;
        }
        if (!first) {
            SLEEP_MS(delay);
        }
        lock();
        bool was = unhold_key(st->keys[k]);
        unlock();
        if (was) {
            espkb_status_changed();
            first = false;
            SEND_KBD();
        }
    }
    return EK_NONE;
}

static int32_t div_round(int64_t a, int64_t n)
{
    int64_t q = (llabs(a) * 2 + n) / (2 * n);
    return (int32_t)(a < 0 ? -q : q);
}

/* Splits (a, b) into reports of at most m per axis along a straight line. */
static espkb_err_t exec_split(job_t *job, uint32_t gen0, int32_t a, int32_t b, int m, int32_t delay,
                              bool scroll)
{
    int32_t big = abs(a) > abs(b) ? abs(a) : abs(b);
    int32_t n = (big + m - 1) / m;
    int32_t pa = 0, pb = 0;
    for (int32_t i = 1; i <= n; i++) {
        int32_t ca = div_round((int64_t)a * i, n);
        int32_t cb = div_round((int64_t)b * i, n);
        int8_t da = (int8_t)(ca - pa), db = (int8_t)(cb - pb);
        pa = ca;
        pb = cb;
        if (scroll) {
            SEND_MOUSE(0, 0, da, db);
        } else {
            SEND_MOUSE(da, db, 0, 0);
        }
        if (i < n && delay > 0) {
            SLEEP_MS(delay);
        }
    }
    return EK_NONE;
}

/* Puts the pointer at an absolute position. Linux drops an absolute value equal
 * to the previous one from the same device, although the pointer may have been
 * moved since (by the relative interface or by another mouse). So when the
 * target is the position reported last, go there through the point next to it. */
static espkb_err_t exec_move_to(job_t *job, uint32_t gen0, const step_t *st)
{
    int16_t x = (int16_t)st->a, y = (int16_t)st->b;
    lock();
    bool same = E.abs_x == x && E.abs_y == y;
    unlock();
    if (same) {
        int16_t beside = (int16_t)(x < ABS_MAX ? x + 1 : x - 1);
        SEND_ABS(beside, y);
    }
    SEND_ABS(x, y);
    return EK_NONE;
}

static espkb_err_t exec_click(job_t *job, uint32_t gen0, const step_t *st)
{
    uint8_t bit = (uint8_t)(1u << st->button);
    for (int c = 0; c < st->count; c++) {
        if (c > 0) {
            SLEEP_MS(st->interval_ms);
        }
        lock();
        E.tr_btns |= bit;
        unlock();
        SEND_MOUSE(0, 0, 0, 0);
        SLEEP_MS(st->hold_ms);
        lock();
        E.tr_btns &= (uint8_t)~bit;
        unlock();
        SEND_MOUSE(0, 0, 0, 0);
    }
    return EK_NONE;
}

static espkb_err_t exec_button(job_t *job, uint32_t gen0, const step_t *st)
{
    bool changed;
    lock();
    if (st->action == ACT_DOWN) {
        if (!owner_alive(job->owner)) {
            unlock();
            return EK_NONE;
        }
        changed = E.btn_owner[st->button] == 0;
        E.btn_owner[st->button] = job->owner;
    } else {
        changed = E.btn_owner[st->button] != 0;
        E.btn_owner[st->button] = 0;
    }
    unlock();
    if (st->action == ACT_DOWN && job->owner == OWNER_HTTP) {
        set_http_ttl(st);
    }
    if (changed) {
        espkb_status_changed();
        SEND_MOUSE(0, 0, 0, 0);
    }
    return EK_NONE;
}

static espkb_err_t exec_step(job_t *job, uint32_t gen0, const step_t *st)
{
    switch (st->type) {
    case STEP_TEXT:
        return exec_text(job, gen0, st);
    case STEP_KEY:
        return exec_key(job, gen0, st);
    case STEP_MOVE:
        return exec_split(job, gen0, st->a, st->b, st->mstep, st->delay_ms, false);
    case STEP_MOVE_TO:
        if (usb_hid_boot_protocol(ITF_MOUSE)) {
            /* A host that put the mouse in boot protocol (BIOS) does not read the
             * absolute interface: the report would only time out. */
            return EK_NONE;
        }
        return exec_move_to(job, gen0, st);
    case STEP_SCROLL:
        if (usb_hid_boot_protocol(ITF_MOUSE)) {
            return EK_NONE; /* the boot mouse report has no wheel */
        }
        return exec_split(job, gen0, st->a, st->b, 127, 0, true);
    case STEP_CLICK:
        return exec_click(job, gen0, st);
    case STEP_BUTTON:
        return exec_button(job, gen0, st);
    case STEP_DELAY:
        return sleep_ms(job, gen0, (int32_t)st->delay);
    case STEP_RELEASE:
        lock();
        clear_all_keys();
        unlock();
        espkb_status_changed();
        SEND_KBD();
        SEND_MOUSE(0, 0, 0, 0);
        return EK_NONE;
    }
    return EK_NONE;
}

static void run_job(job_t *job)
{
    uint32_t gen0 = usb_hid_generation();
    espkb_err_t e = EK_NONE;
    if (job->abort) {
        e = EK_ABORTED;
    } else if (!usb_hid_mounted()) {
        e = EK_USB_NOT_READY;
    } else {
        e = ensure_awake(job, gen0);
    }
    for (int i = 0; !e && i < job->nsteps; i++) {
        e = exec_step(job, gen0, &job->steps[i]);
        if (e) {
            job->fail_index = i;
        }
    }

    int64_t now = now_us();
    bool changed = false;
    lock();
    E.current = NULL;
    owner_pending_add(job->owner, -1, now);
    if (e) {
        /* Failure or abort: forget every pressed key; try_sync() then sends the
         * all-released reports as soon as the endpoints accept them. */
        changed = any_held() || E.ntr || E.tr_mods || E.tr_btns;
        clear_all_keys();
        E.jobs_failed++;
    } else {
        E.ntr = 0;
        E.tr_mods = 0;
        E.tr_btns = 0;
        E.jobs_done++;
    }
    unlock();
    if (changed) {
        espkb_status_changed();
    }
    if (e && e != EK_ABORTED) {
        ESP_LOGW(TAG, "job %lu failed: %s (step %d)", (unsigned long)job->id, espkb_err_code(e), job->fail_index);
    }
    job->result = e;
    job->t_done_us = now;
    job->done(job);
}

static void engine_task(void *arg)
{
    (void)arg;
    for (;;) {
        bool changed;
        bool wake;
        bool held;
        job_t *job = NULL;
        lock();
        changed = housekeeping();
        if (E.release_pending) {
            E.release_pending = false;
            clear_all_keys();
            E.force_sync[ITF_KBD] = E.force_sync[ITF_MOUSE] = true;
            changed = true;
        }
        wake = E.wake_requested;
        E.wake_requested = false;
        if (E.qcount > 0) {
            job = E.queue[E.qhead];
            E.queue[E.qhead] = NULL;
            E.qhead = (E.qhead + 1) % ESPKB_QUEUE_CAP;
            E.qcount--;
            E.current = job;
        }
        held = any_held() || E.force_sync[0] || E.force_sync[1];
        unlock();
        if (changed) {
            espkb_status_changed();
        }
        if (wake) {
            usb_hid_remote_wakeup();
        }
        try_sync();
        if (job) {
            run_job(job);
            continue;
        }
        wait_ev(EV_ALL, held ? HOUSEKEEP_US : 500 * 1000LL);
    }
}

static void usb_notify(uint32_t bits)
{
    EventBits_t ev = 0;
    if (bits & USB_EVT_DONE_KBD) {
        ev |= EV_DONE_KBD;
    }
    if (bits & USB_EVT_DONE_MOUSE) {
        ev |= EV_DONE_MOUSE;
    }
    if (bits & USB_EVT_DONE_ABS) {
        ev |= EV_DONE_ABS;
    }
    if (bits & USB_EVT_STATE) {
        ev |= EV_USB;
    }
    if (ev && E.ev) {
        xEventGroupSetBits(E.ev, ev);
    }
}

/* ---------- public API ---------- */

void engine_init(void)
{
    E.lock = xSemaphoreCreateMutex();
    E.ev = xEventGroupCreate();
    E.next_owner_id = 2;
    E.http_ttl_ms = HTTP_DEFAULT_TTL;
    configASSERT(E.lock && E.ev);
    ESP_ERROR_CHECK(usb_hid_init(usb_notify));
    E.gen_seen = usb_hid_generation();
    BaseType_t ok = xTaskCreatePinnedToCore(engine_task, "hid_engine", 6144, NULL, 10, NULL, 1);
    configASSERT(ok == pdPASS);
    (void)ok;
}

espkb_err_t engine_submit(job_t *job, uint32_t *job_id)
{
    lock();
    if (E.qcount >= ESPKB_QUEUE_CAP) {
        unlock();
        return EK_BUSY;
    }
    if (++E.next_job_id == 0) {
        E.next_job_id = 1;
    }
    job->id = E.next_job_id;
    if (job_id) {
        *job_id = job->id;
    }
    job->t_submit_us = now_us();
    job->result = EK_NONE;
    job->abort = false;
    owner_pending_add(job->owner, +1, job->t_submit_us);
    E.queue[(E.qhead + E.qcount) % ESPKB_QUEUE_CAP] = job;
    E.qcount++;
    unlock();
    xEventGroupSetBits(E.ev, EV_JOB);
    return EK_NONE;
}

int engine_release_all(void)
{
    job_t *drained[ESPKB_QUEUE_CAP];
    int nd = 0;
    int64_t now = now_us();
    lock();
    while (E.qcount > 0) {
        job_t *j = E.queue[E.qhead];
        E.queue[E.qhead] = NULL;
        E.qhead = (E.qhead + 1) % ESPKB_QUEUE_CAP;
        E.qcount--;
        owner_pending_add(j->owner, -1, now);
        drained[nd++] = j;
    }
    int n = nd;
    if (E.current && !E.current->abort) {
        E.current->abort = true;
        n++;
    }
    E.release_pending = true;
    clear_all_keys();
    E.jobs_failed += (uint32_t)nd;
    unlock();
    xEventGroupSetBits(E.ev, EV_ABORT);
    for (int i = 0; i < nd; i++) {
        drained[i]->result = EK_ABORTED;
        drained[i]->t_done_us = now;
        drained[i]->done(drained[i]);
    }
    espkb_status_changed();
    ESP_LOGI(TAG, "release: %d job(s) aborted", n);
    return n;
}

uint32_t engine_owner_open(void)
{
    uint32_t id = 0;
    lock();
    for (int i = 0; i < MAX_WS_OWNERS; i++) {
        if (E.ws[i].id == 0) {
            do {
                id = E.next_owner_id++;
            } while (id <= OWNER_HTTP);
            E.ws[i].id = id;
            E.ws[i].last_rx_us = now_us();
            E.ws[i].last_end_us = 0;
            E.ws[i].pending = 0;
            break;
        }
    }
    unlock();
    return id;
}

void engine_owner_close(uint32_t owner)
{
    lock();
    int i = ws_find(owner);
    if (i >= 0) {
        memset(&E.ws[i], 0, sizeof(E.ws[i]));
    }
    unlock();
    xEventGroupSetBits(E.ev, EV_OWNER);
}

void engine_owner_touch(uint32_t owner)
{
    lock();
    int i = ws_find(owner);
    if (i >= 0) {
        E.ws[i].last_rx_us = now_us();
    }
    unlock();
}

void engine_http_activity(void)
{
    lock();
    E.http_deadline_us = now_us() + (int64_t)E.http_ttl_ms * 1000;
    unlock();
}

void engine_request_wake(void)
{
    lock();
    E.wake_requested = true;
    unlock();
    xEventGroupSetBits(E.ev, EV_WAKE);
}

void engine_get_status(engine_status_t *st)
{
    memset(st, 0, sizeof(*st));
    lock();
    st->busy = E.current != NULL;
    st->queued = E.qcount;
    for (int b = 0; b < 8; b++) {
        if (E.mod_owner[b]) {
            st->keys[st->nkeys++] = (uint8_t)(HID_MOD_FIRST + b);
        }
    }
    for (int i = 0; i < E.nheld; i++) {
        st->keys[st->nkeys++] = E.held_keys[i];
    }
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (E.btn_owner[i]) {
            st->buttons |= (uint8_t)(1u << i);
        }
    }
    st->jobs_done = E.jobs_done;
    st->jobs_failed = E.jobs_failed;
    unlock();
    st->reports_sent = usb_hid_reports_completed();
}

void engine_held_sim(keysim_t *sim)
{
    memset(sim, 0, sizeof(*sim));
    lock();
    for (int i = 0; i < E.nheld; i++) {
        uint8_t k = E.held_keys[i];
        sim->bits[k >> 3] |= (uint8_t)(1u << (k & 7));
        sim->count++;
    }
    unlock();
}
