#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "keymap.h"
#include "steps.h"

#define MAX_MS        60000
#define MAX_DELAY_MS  1000
#define MAX_TTL_MS    600000
#define MAX_COORD     32767
#define JSON_MAX_DEPTH 32

static const char *const s_verbs[STEP_TYPE_COUNT] = {
    [STEP_TEXT] = "text", [STEP_KEY] = "key", [STEP_MOVE] = "move", [STEP_CLICK] = "click",
    [STEP_BUTTON] = "button", [STEP_SCROLL] = "scroll", [STEP_DELAY] = "delay", [STEP_RELEASE] = "release",
};

job_t *job_alloc(size_t nsteps)
{
    job_t *job = espkb_calloc(1, sizeof(job_t));
    if (!job) {
        return NULL;
    }
    if (nsteps > 0) {
        job->steps = espkb_calloc(nsteps, sizeof(step_t));
        if (!job->steps) {
            espkb_free(job);
            return NULL;
        }
    }
    job->nsteps = (uint16_t)nsteps;
    job->fail_index = -1;
    return job;
}

void job_free(job_t *job)
{
    if (!job) {
        return;
    }
    for (int i = 0; i < job->nsteps; i++) {
        espkb_free(job->steps[i].text);
    }
    espkb_free(job->steps);
    espkb_free(job);
}

static bool fail(parse_err_t *err, espkb_err_t code, int index, long offset, const char *fmt, ...)
    __attribute__((format(printf, 5, 6)));

static bool fail(parse_err_t *err, espkb_err_t code, int index, long offset, const char *fmt, ...)
{
    err->err = code;
    err->index = index;
    err->offset = offset;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err->msg, sizeof(err->msg), fmt, ap);
    va_end(ap);
    return false;
}

static bool json_int(const cJSON *item, long lo, long hi, long *out)
{
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    double d = item->valuedouble;
    if (!isfinite(d) || d != floor(d) || d < (double)lo || d > (double)hi) {
        return false;
    }
    *out = (long)d;
    return true;
}

/* Optional integer field: returns false (and fills err) if present but invalid. */
static bool opt_int(const cJSON *obj, const char *name, long lo, long hi, long def, long *out,
                    int index, parse_err_t *err)
{
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!it) {
        *out = def;
        return true;
    }
    if (!json_int(it, lo, hi, out)) {
        return fail(err, EK_BAD_STEP, index, -1, "%s must be an integer %ld..%ld", name, lo, hi);
    }
    return true;
}

static bool sim_has(const keysim_t *sim, uint8_t k)
{
    return (sim->bits[k >> 3] >> (k & 7)) & 1;
}

static bool sim_add(keysim_t *sim, uint8_t k)
{
    if (k >= HID_MOD_FIRST && k <= HID_MOD_LAST) {
        return true;
    }
    if (!sim_has(sim, k)) {
        if (sim->count >= 6) {
            return false;
        }
        sim->bits[k >> 3] |= (uint8_t)(1u << (k & 7));
        sim->count++;
    }
    return true;
}

static void sim_remove(keysim_t *sim, uint8_t k)
{
    if (k < HID_MOD_FIRST && sim_has(sim, k)) {
        sim->bits[k >> 3] &= (uint8_t)~(1u << (k & 7));
        sim->count--;
    }
}

/* Number of keys of a combo that would be newly pressed (non-modifiers not yet held). */
static int sim_new_keys(const keysim_t *sim, const uint8_t *keys, int n)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (keys[i] < HID_MOD_FIRST && !sim_has(sim, keys[i])) {
            c++;
        }
    }
    return c;
}

/* Validates text bytes; returns the offset of the first unsupported byte or -1. */
static long text_check(const char *t, size_t len, const keysim_t *sim, bool *rollover)
{
    *rollover = false;
    for (size_t i = 0; i < len; i++) {
        uint8_t usage;
        bool shift;
        if (!keymap_char((uint8_t)t[i], &usage, &shift)) {
            return (long)i;
        }
        if (sim->count >= 6 && !sim_has(sim, usage)) {
            *rollover = true;
            return (long)i;
        }
    }
    return -1;
}

static bool parse_text(const char *t, size_t len, int index, step_t *out, const keysim_t *sim,
                       parse_err_t *err)
{
    bool rollover;
    long off = text_check(t, len, sim, &rollover);
    if (off >= 0) {
        if (rollover) {
            return fail(err, EK_BAD_STEP, index, off, "more than 6 non-modifier keys would be held");
        }
        uint8_t c = (uint8_t)t[off];
        if (c >= 0x20 && c < 0x7f) {
            return fail(err, EK_BAD_STEP, index, off, "unsupported character '%c' at offset %ld", c, off);
        }
        return fail(err, EK_BAD_STEP, index, off, "unsupported character 0x%02x at offset %ld (only ASCII, \\n, \\t, \\b, ESC)", c, off);
    }
    out->text = espkb_malloc(len + 1);
    if (!out->text) {
        return fail(err, EK_BUSY, index, -1, "out of memory");
    }
    memcpy(out->text, t, len);
    out->text[len] = '\0';
    out->text_len = (uint32_t)len;
    return true;
}

static bool parse_key_value(const cJSON *v, int index, step_t *out, parse_err_t *err)
{
    out->nkeys = 0;
    if (cJSON_IsNumber(v)) {
        long code;
        if (!json_int(v, 1, 255, &code)) {
            return fail(err, EK_BAD_STEP, index, -1, "key usage code must be an integer 1..255");
        }
        out->keys[out->nkeys++] = (uint8_t)code;
        return true;
    }
    if (!cJSON_IsString(v)) {
        return fail(err, EK_BAD_STEP, index, -1, "key must be a name, a combo like \"ctrl+c\" or a usage code");
    }
    const char *s = v->valuestring;
    size_t total = strlen(s);
    size_t pos = 0;
    while (true) {
        const char *plus = memchr(s + pos, '+', total - pos);
        size_t end = plus ? (size_t)(plus - s) : total;
        size_t len = end - pos;
        if (len == 0) {
            return fail(err, EK_BAD_STEP, index, -1, "empty key name in \"%.40s\" (use \"shift+=\" or text for '+')", s);
        }
        int code = keymap_lookup(s + pos, len);
        if (code <= 0 || code > 255) {
            return fail(err, EK_BAD_STEP, index, -1, "unknown key name \"%.*s\"", (int)(len > 40 ? 40 : len), s + pos);
        }
        for (int i = 0; i < out->nkeys; i++) {
            if (out->keys[i] == code) {
                return fail(err, EK_BAD_STEP, index, -1, "key \"%.*s\" appears twice", (int)(len > 40 ? 40 : len), s + pos);
            }
        }
        if (out->nkeys >= STEP_MAX_COMBO) {
            return fail(err, EK_BAD_STEP, index, -1, "too many keys in combo");
        }
        out->keys[out->nkeys++] = (uint8_t)code;
        if (!plus) {
            break;
        }
        pos = end + 1;
    }
    return true;
}

static bool parse_action(const cJSON *obj, bool required, bool allow_tap, int index, uint8_t *out,
                         parse_err_t *err)
{
    const cJSON *a = cJSON_GetObjectItemCaseSensitive(obj, "action");
    if (!a) {
        if (required) {
            return fail(err, EK_BAD_STEP, index, -1, "action is required (\"down\" or \"up\")");
        }
        *out = ACT_TAP;
        return true;
    }
    if (cJSON_IsString(a)) {
        if (allow_tap && str_ieq(a->valuestring, "tap")) {
            *out = ACT_TAP;
            return true;
        }
        if (str_ieq(a->valuestring, "down")) {
            *out = ACT_DOWN;
            return true;
        }
        if (str_ieq(a->valuestring, "up")) {
            *out = ACT_UP;
            return true;
        }
    }
    return fail(err, EK_BAD_STEP, index, -1, allow_tap ? "action must be \"tap\", \"down\" or \"up\""
                                                        : "action must be \"down\" or \"up\"");
}

static bool parse_pair(const cJSON *v, int32_t *a, int32_t *b)
{
    long x, y;
    if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) != 2) {
        return false;
    }
    if (!json_int(cJSON_GetArrayItem(v, 0), -MAX_COORD, MAX_COORD, &x) ||
        !json_int(cJSON_GetArrayItem(v, 1), -MAX_COORD, MAX_COORD, &y)) {
        return false;
    }
    *a = (int32_t)x;
    *b = (int32_t)y;
    return true;
}

/* Parses and validates one step; sim tracks held keys for the 6-key rollover rule. */
static bool step_parse(const cJSON *obj, int index, uint32_t allowed, step_t *out, keysim_t *sim,
                       parse_err_t *err)
{
    if (!cJSON_IsObject(obj)) {
        return fail(err, EK_BAD_STEP, index, -1, "step must be a JSON object");
    }
    int type = -1;
    int nverbs = 0;
    for (int t = 0; t < STEP_TYPE_COUNT; t++) {
        if (cJSON_GetObjectItemCaseSensitive(obj, s_verbs[t])) {
            type = t;
            nverbs++;
        }
    }
    if (nverbs != 1) {
        return fail(err, EK_BAD_STEP, index, -1, nverbs == 0
                    ? "step has no verb (text, key, move, click, button, scroll, delay, release)"
                    : "step must have exactly one verb");
    }
    if (!(allowed & STEP_MASK(type))) {
        return fail(err, EK_BAD_STEP, index, -1, "\"%s\" step not accepted by this endpoint", s_verbs[type]);
    }
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, s_verbs[type]);
    long n;
    memset(out, 0, sizeof(*out));
    out->type = (uint8_t)type;
    out->hold_ms = -1;
    out->delay_ms = -1;

    switch (type) {
    case STEP_TEXT:
        if (!cJSON_IsString(v)) {
            return fail(err, EK_BAD_STEP, index, -1, "text must be a string");
        }
        if (!opt_int(obj, "delay_ms", 0, MAX_DELAY_MS, -1, &n, index, err)) {
            return false;
        }
        out->delay_ms = (int32_t)n;
        return parse_text(v->valuestring, strlen(v->valuestring), index, out, sim, err);

    case STEP_KEY: {
        if (!parse_key_value(v, index, out, err) ||
            !parse_action(obj, false, true, index, &out->action, err)) {
            return false;
        }
        if (!opt_int(obj, "hold_ms", 0, MAX_MS, -1, &n, index, err)) {
            return false;
        }
        out->hold_ms = (int32_t)n;
        if (!opt_int(obj, "repeat", 1, 1000, 1, &n, index, err)) {
            return false;
        }
        out->repeat = (uint16_t)n;
        if (!opt_int(obj, "delay_ms", 0, MAX_DELAY_MS, -1, &n, index, err)) {
            return false;
        }
        out->delay_ms = (int32_t)n;
        if (!opt_int(obj, "ttl_ms", 1, MAX_TTL_MS, 0, &n, index, err)) {
            return false;
        }
        out->ttl_ms = (int32_t)n;
        if (out->action == ACT_TAP) {
            if (sim->count + sim_new_keys(sim, out->keys, out->nkeys) > 6) {
                return fail(err, EK_BAD_STEP, index, -1, "more than 6 non-modifier keys would be held");
            }
        } else if (out->action == ACT_DOWN) {
            for (int i = 0; i < out->nkeys; i++) {
                if (!sim_add(sim, out->keys[i])) {
                    return fail(err, EK_BAD_STEP, index, -1, "more than 6 non-modifier keys would be held");
                }
            }
        } else {
            for (int i = 0; i < out->nkeys; i++) {
                sim_remove(sim, out->keys[i]);
            }
        }
        return true;
    }

    case STEP_MOVE:
        if (!parse_pair(v, &out->a, &out->b)) {
            return fail(err, EK_BAD_STEP, index, -1, "move must be [dx, dy] with integers in -32767..32767");
        }
        if (!opt_int(obj, "step", 1, 127, 127, &n, index, err)) {
            return false;
        }
        out->mstep = (uint8_t)n;
        if (!opt_int(obj, "delay_ms", 0, MAX_DELAY_MS, 0, &n, index, err)) {
            return false;
        }
        out->delay_ms = (int32_t)n;
        return true;

    case STEP_CLICK:
    case STEP_BUTTON: {
        int b = cJSON_IsString(v) ? keymap_button(v->valuestring) : -1;
        if (b < 0) {
            return fail(err, EK_BAD_STEP, index, -1, "unknown mouse button (left, right, middle, back, forward)");
        }
        out->button = (uint8_t)b;
        if (type == STEP_BUTTON) {
            if (!parse_action(obj, true, false, index, &out->action, err) ||
                !opt_int(obj, "ttl_ms", 1, MAX_TTL_MS, 0, &n, index, err)) {
                return false;
            }
            out->ttl_ms = (int32_t)n;
            return true;
        }
        if (!opt_int(obj, "count", 1, 100, 1, &n, index, err)) {
            return false;
        }
        out->count = (uint16_t)n;
        if (!opt_int(obj, "hold_ms", 0, MAX_MS, 20, &n, index, err)) {
            return false;
        }
        out->hold_ms = (int32_t)n;
        if (!opt_int(obj, "interval_ms", 0, MAX_MS, 60, &n, index, err)) {
            return false;
        }
        out->interval_ms = (int32_t)n;
        return true;
    }

    case STEP_SCROLL:
        if (cJSON_IsNumber(v)) {
            if (!json_int(v, -MAX_COORD, MAX_COORD, &n)) {
                return fail(err, EK_BAD_STEP, index, -1, "scroll must be an integer in -32767..32767 or [v, h]");
            }
            out->a = (int32_t)n;
            out->b = 0;
        } else if (!parse_pair(v, &out->a, &out->b)) {
            return fail(err, EK_BAD_STEP, index, -1, "scroll must be an integer in -32767..32767 or [v, h]");
        }
        return true;

    case STEP_DELAY:
        if (!json_int(v, 0, MAX_MS, &n)) {
            return fail(err, EK_BAD_STEP, index, -1, "delay must be an integer 0..60000 (ms)");
        }
        out->delay = (uint32_t)n;
        return true;

    case STEP_RELEASE:
        if (!cJSON_IsTrue(v)) {
            return fail(err, EK_BAD_STEP, index, -1, "release must be true");
        }
        memset(sim, 0, sizeof(*sim));
        return true;
    }
    return fail(err, EK_BAD_STEP, index, -1, "invalid step");
}

job_t *job_from_array(const cJSON *arr, const keysim_t *held, parse_err_t *err)
{
    if (!cJSON_IsArray(arr)) {
        fail(err, EK_BAD_REQUEST, -1, -1, "steps must be an array");
        return NULL;
    }
    int n = cJSON_GetArraySize(arr);
    if (n > ESPKB_MAX_STEPS) {
        fail(err, EK_TOO_LARGE, -1, -1, "at most %d steps per request (got %d)", ESPKB_MAX_STEPS, n);
        return NULL;
    }
    job_t *job = job_alloc((size_t)n);
    if (!job) {
        fail(err, EK_BUSY, -1, -1, "out of memory");
        return NULL;
    }
    keysim_t sim = *held;
    int i = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        if (!step_parse(it, i, STEP_MASK_ALL, &job->steps[i], &sim, err)) {
            job_free(job);
            return NULL;
        }
        i++;
    }
    return job;
}

job_t *job_from_step(const cJSON *obj, uint32_t allowed_mask, const keysim_t *held, parse_err_t *err)
{
    if (!cJSON_IsObject(obj)) {
        fail(err, EK_BAD_REQUEST, -1, -1, "body must be a JSON object");
        return NULL;
    }
    job_t *job = job_alloc(1);
    if (!job) {
        fail(err, EK_BUSY, -1, -1, "out of memory");
        return NULL;
    }
    keysim_t sim = *held;
    if (!step_parse(obj, 0, allowed_mask, &job->steps[0], &sim, err)) {
        job_free(job);
        return NULL;
    }
    return job;
}

job_t *job_from_text(const char *text, size_t len, const keysim_t *held, parse_err_t *err)
{
    job_t *job = job_alloc(1);
    if (!job) {
        fail(err, EK_BUSY, -1, -1, "out of memory");
        return NULL;
    }
    step_t *st = &job->steps[0];
    st->type = STEP_TEXT;
    st->hold_ms = -1;
    st->delay_ms = -1;
    if (!parse_text(text, len, 0, st, held, err)) {
        job_free(job);
        return NULL;
    }
    return job;
}

cJSON *json_parse_body(char *body, size_t len, parse_err_t *err)
{
    if (len == 0) {
        fail(err, EK_BAD_REQUEST, -1, -1, "empty body, JSON expected");
        return NULL;
    }
    const char *bad = json_prescan(body, len, JSON_MAX_DEPTH);
    if (bad) {
        fail(err, EK_BAD_REQUEST, -1, -1, "%s", bad);
        return NULL;
    }
    /* body[len] is NUL: including it lets cJSON reject trailing garbage. */
    cJSON *root = cJSON_ParseWithLengthOpts(body, len + 1, NULL, true);
    if (!root) {
        fail(err, EK_BAD_REQUEST, -1, -1, "invalid JSON");
        return NULL;
    }
    return root;
}
