#include <math.h>
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "config.h"
#include "espkb.h"

static const char *TAG = "config";
static const char *NS = "espkb";

static SemaphoreHandle_t s_mx;
static espkb_config_t s_cfg;

static void lock(void) { xSemaphoreTake(s_mx, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_mx); }

/* ---------- validation ---------- */

static bool valid_hostname(const char *s)
{
    size_t n = strlen(s);
    if (n < 1 || n > 32 || s[0] == '-' || s[n - 1] == '-') {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            return false;
        }
    }
    return true;
}

/* Valid UTF-8 without control characters. */
static bool valid_utf8_text(const char *s)
{
    const uint8_t *p = (const uint8_t *)s;
    while (*p) {
        if (*p < 0x20 || *p == 0x7f) {
            return false;
        }
        if (*p < 0x80) {
            p++;
            continue;
        }
        int extra;
        uint32_t cp;
        if ((*p & 0xE0) == 0xC0) {
            extra = 1;
            cp = *p & 0x1F;
        } else if ((*p & 0xF0) == 0xE0) {
            extra = 2;
            cp = *p & 0x0F;
        } else if ((*p & 0xF8) == 0xF0) {
            extra = 3;
            cp = *p & 0x07;
        } else {
            return false;
        }
        p++;
        for (int i = 0; i < extra; i++, p++) {
            if ((*p & 0xC0) != 0x80) {
                return false;
            }
            cp = (cp << 6) | (*p & 0x3F);
        }
        if ((extra == 1 && cp < 0x80) || (extra == 2 && cp < 0x800) || (extra == 3 && cp < 0x10000) ||
            cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
            return false;
        }
    }
    return true;
}

static bool printable_ascii(const char *s, bool allow_space)
{
    for (; *s; s++) {
        if (*s < (allow_space ? 0x20 : 0x21) || *s > 0x7e) {
            return false;
        }
    }
    return true;
}

static bool valid_ssid(const char *s) { return strlen(s) <= 32 && valid_utf8_text(s); }
static bool valid_wifi_password(const char *s)
{
    size_t n = strlen(s);
    return (n == 0 || (n >= 8 && n <= 63)) && printable_ascii(s, true);
}
static bool valid_ap_password(const char *s)
{
    size_t n = strlen(s);
    return n >= 8 && n <= 63 && printable_ascii(s, true);
}
static bool valid_token(const char *s) { return strlen(s) <= 64 && printable_ascii(s, false); }

/* ---------- NVS ---------- */

static void load_str(nvs_handle_t h, const char *key, char *dst, size_t cap, const char *def, bool *found)
{
    size_t len = cap;
    if (nvs_get_str(h, key, dst, &len) != ESP_OK) {
        snprintf(dst, cap, "%s", def);
        if (found) {
            *found = false;
        }
    } else if (found) {
        *found = true;
    }
}

static esp_err_t save_locked(const espkb_config_t *c)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(h, "hostname", c->hostname);
    if (err == ESP_OK) err = nvs_set_str(h, "ssid", c->wifi_ssid);
    if (err == ESP_OK) err = nvs_set_str(h, "wpass", c->wifi_password);
    if (err == ESP_OK) err = nvs_set_str(h, "appass", c->ap_password);
    if (err == ESP_OK) err = nvs_set_str(h, "token", c->token);
    if (err == ESP_OK) err = nvs_set_u16(h, "kdelay", c->key_delay_ms);
    if (err == ESP_OK) err = nvs_set_u8(h, "lquiet", c->led_quiet ? 1 : 0);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "saving config failed: %s", esp_err_to_name(err));
    }
    return err;
}

void config_init(void)
{
    s_mx = xSemaphoreCreateMutex();
    memset(&s_cfg, 0, sizeof(s_cfg));
    nvs_handle_t h;
    bool have_nvs = nvs_open(NS, NVS_READONLY, &h) == ESP_OK;
    if (have_nvs) {
        load_str(h, "hostname", s_cfg.hostname, sizeof(s_cfg.hostname), CFG_DEFAULT_HOSTNAME, NULL);
        load_str(h, "ssid", s_cfg.wifi_ssid, sizeof(s_cfg.wifi_ssid), "", NULL);
        load_str(h, "wpass", s_cfg.wifi_password, sizeof(s_cfg.wifi_password), "", NULL);
        load_str(h, "appass", s_cfg.ap_password, sizeof(s_cfg.ap_password), CFG_DEFAULT_AP_PASS, NULL);
        load_str(h, "token", s_cfg.token, sizeof(s_cfg.token), "", NULL);
        if (nvs_get_u16(h, "kdelay", &s_cfg.key_delay_ms) != ESP_OK) {
            s_cfg.key_delay_ms = CFG_DEFAULT_KEY_DELAY;
        }
        uint8_t quiet = 0;
        if (nvs_get_u8(h, "lquiet", &quiet) != ESP_OK) {
            quiet = 0;
        }
        s_cfg.led_quiet = quiet != 0;
        nvs_close(h);
    } else {
        snprintf(s_cfg.hostname, sizeof(s_cfg.hostname), "%s", CFG_DEFAULT_HOSTNAME);
        snprintf(s_cfg.ap_password, sizeof(s_cfg.ap_password), "%s", CFG_DEFAULT_AP_PASS);
        s_cfg.key_delay_ms = CFG_DEFAULT_KEY_DELAY;
    }
    /* Defend against corrupted or hand-edited values. */
    if (!valid_hostname(s_cfg.hostname)) {
        snprintf(s_cfg.hostname, sizeof(s_cfg.hostname), "%s", CFG_DEFAULT_HOSTNAME);
    }
    if (!valid_ap_password(s_cfg.ap_password)) {
        snprintf(s_cfg.ap_password, sizeof(s_cfg.ap_password), "%s", CFG_DEFAULT_AP_PASS);
    }
    /* API.md §10: ap_password_set is false while the (publicly known) default is in use. */
    s_cfg.ap_password_custom = strcmp(s_cfg.ap_password, CFG_DEFAULT_AP_PASS) != 0;
    if (!valid_ssid(s_cfg.wifi_ssid)) {
        s_cfg.wifi_ssid[0] = '\0';
    }
    if (!valid_wifi_password(s_cfg.wifi_password)) {
        s_cfg.wifi_password[0] = '\0';
    }
    if (!valid_token(s_cfg.token)) {
        s_cfg.token[0] = '\0';
    }
    if (s_cfg.key_delay_ms > 1000) {
        s_cfg.key_delay_ms = CFG_DEFAULT_KEY_DELAY;
    }
    ESP_LOGI(TAG, "hostname=%s ssid=\"%s\" token=%s key_delay=%u", s_cfg.hostname, s_cfg.wifi_ssid,
             s_cfg.token[0] ? "set" : "none", s_cfg.key_delay_ms);
}

void config_get(espkb_config_t *out)
{
    lock();
    *out = s_cfg;
    unlock();
}

uint16_t config_key_delay(void)
{
    return s_cfg.key_delay_ms; /* 16-bit aligned read is atomic */
}

bool config_led_quiet(void)
{
    return s_cfg.led_quiet; /* single-byte read is atomic */
}

bool config_token_required(void)
{
    lock();
    bool r = s_cfg.token[0] != '\0';
    unlock();
    return r;
}

bool config_token_ok(const char *tok, size_t len)
{
    lock();
    bool ok = espkb_token_equal(tok, len, s_cfg.token);
    unlock();
    return ok;
}

/* ---------- updates ---------- */

static bool get_str_field(const cJSON *obj, const char *name, const char **out, char *msg, size_t msglen)
{
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!it) {
        *out = NULL;
        return true;
    }
    if (!cJSON_IsString(it)) {
        snprintf(msg, msglen, "%s must be a string", name);
        return false;
    }
    *out = it->valuestring;
    return true;
}

bool config_update_json(const cJSON *obj, bool *reboot_required, char *msg, size_t msglen)
{
    *reboot_required = false;
    if (!cJSON_IsObject(obj)) {
        snprintf(msg, msglen, "body must be a JSON object");
        return false;
    }
    const char *hostname, *ssid, *wpass, *appass, *token;
    if (!get_str_field(obj, "hostname", &hostname, msg, msglen) ||
        !get_str_field(obj, "wifi_ssid", &ssid, msg, msglen) ||
        !get_str_field(obj, "wifi_password", &wpass, msg, msglen) ||
        !get_str_field(obj, "ap_password", &appass, msg, msglen) ||
        !get_str_field(obj, "token", &token, msg, msglen)) {
        return false;
    }
    if (hostname && !valid_hostname(hostname)) {
        snprintf(msg, msglen, "hostname must be 1-32 chars [a-z0-9-], not starting or ending with '-'");
        return false;
    }
    if (ssid && !valid_ssid(ssid)) {
        snprintf(msg, msglen, "wifi_ssid must be 0-32 bytes of printable UTF-8");
        return false;
    }
    if (wpass && !valid_wifi_password(wpass)) {
        snprintf(msg, msglen, "wifi_password must be empty or 8-63 printable ASCII characters");
        return false;
    }
    if (appass && !valid_ap_password(appass)) {
        snprintf(msg, msglen, "ap_password must be 8-63 printable ASCII characters");
        return false;
    }
    if (token && !valid_token(token)) {
        snprintf(msg, msglen, "token must be 0-64 printable ASCII characters without spaces");
        return false;
    }
    long kd = -1;
    const cJSON *kdi = cJSON_GetObjectItemCaseSensitive(obj, "key_delay_ms");
    if (kdi) {
        double d = cJSON_IsNumber(kdi) ? kdi->valuedouble : -1;
        if (!cJSON_IsNumber(kdi) || !isfinite(d) || d != floor(d) || d < 0 || d > 1000) {
            snprintf(msg, msglen, "key_delay_ms must be an integer 0..1000");
            return false;
        }
        kd = (long)d;
    }
    const cJSON *qi = cJSON_GetObjectItemCaseSensitive(obj, "led_quiet");
    if (qi && !cJSON_IsBool(qi)) {
        snprintf(msg, msglen, "led_quiet must be a boolean");
        return false;
    }

    lock();
    espkb_config_t c = s_cfg;
    if (hostname && strcmp(hostname, c.hostname) != 0) {
        snprintf(c.hostname, sizeof(c.hostname), "%s", hostname);
        *reboot_required = true;
    }
    if (ssid && strcmp(ssid, c.wifi_ssid) != 0) {
        snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", ssid);
        *reboot_required = true;
    }
    if (wpass && strcmp(wpass, c.wifi_password) != 0) {
        snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", wpass);
        *reboot_required = true;
    }
    if (appass) {
        if (strcmp(appass, c.ap_password) != 0) {
            *reboot_required = true;
        }
        snprintf(c.ap_password, sizeof(c.ap_password), "%s", appass);
        c.ap_password_custom = strcmp(appass, CFG_DEFAULT_AP_PASS) != 0;
    }
    if (token) {
        snprintf(c.token, sizeof(c.token), "%s", token);
    }
    if (kd >= 0) {
        c.key_delay_ms = (uint16_t)kd;
    }
    if (qi) {
        c.led_quiet = cJSON_IsTrue(qi);
    }
    esp_err_t err = save_locked(&c);
    if (err == ESP_OK) {
        s_cfg = c;
    }
    unlock();
    if (err != ESP_OK) {
        snprintf(msg, msglen, "failed to save configuration: %s", esp_err_to_name(err));
        return false;
    }
    if (token || kd >= 0) {
        espkb_status_changed();
    }
    return true;
}

static bool apply(const espkb_config_t *c, char *msg, size_t msglen)
{
    esp_err_t err = save_locked(c);
    if (err == ESP_OK) {
        s_cfg = *c;
        return true;
    }
    snprintf(msg, msglen, "failed to save: %s", esp_err_to_name(err));
    return false;
}

bool config_set_wifi(const char *ssid, const char *password, char *msg, size_t msglen)
{
    if (!valid_ssid(ssid)) {
        snprintf(msg, msglen, "ssid must be 0-32 bytes of printable UTF-8");
        return false;
    }
    if (!valid_wifi_password(password)) {
        snprintf(msg, msglen, "password must be empty or 8-63 printable ASCII characters");
        return false;
    }
    lock();
    espkb_config_t c = s_cfg;
    snprintf(c.wifi_ssid, sizeof(c.wifi_ssid), "%s", ssid);
    snprintf(c.wifi_password, sizeof(c.wifi_password), "%s", password);
    bool ok = apply(&c, msg, msglen);
    unlock();
    return ok;
}

bool config_set_token(const char *token, char *msg, size_t msglen)
{
    if (!valid_token(token)) {
        snprintf(msg, msglen, "token must be 0-64 printable ASCII characters without spaces");
        return false;
    }
    lock();
    espkb_config_t c = s_cfg;
    snprintf(c.token, sizeof(c.token), "%s", token);
    bool ok = apply(&c, msg, msglen);
    unlock();
    if (ok) {
        espkb_status_changed();
    }
    return ok;
}

bool config_set_hostname(const char *hostname, char *msg, size_t msglen)
{
    if (!valid_hostname(hostname)) {
        snprintf(msg, msglen, "hostname must be 1-32 chars [a-z0-9-], not starting or ending with '-'");
        return false;
    }
    lock();
    espkb_config_t c = s_cfg;
    snprintf(c.hostname, sizeof(c.hostname), "%s", hostname);
    bool ok = apply(&c, msg, msglen);
    unlock();
    return ok;
}

bool config_set_key_delay(long ms, char *msg, size_t msglen)
{
    if (ms < 0 || ms > 1000) {
        snprintf(msg, msglen, "key delay must be 0..1000 ms");
        return false;
    }
    lock();
    espkb_config_t c = s_cfg;
    c.key_delay_ms = (uint16_t)ms;
    bool ok = apply(&c, msg, msglen);
    unlock();
    return ok;
}

bool config_set_led_quiet(bool quiet, char *msg, size_t msglen)
{
    lock();
    espkb_config_t c = s_cfg;
    c.led_quiet = quiet;
    bool ok = apply(&c, msg, msglen);
    unlock();
    return ok;
}

esp_err_t config_factory_reset(void)
{
    lock();
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_erase_all(h);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    unlock();
    ESP_LOGW(TAG, "factory reset: %s", esp_err_to_name(err));
    return err;
}
