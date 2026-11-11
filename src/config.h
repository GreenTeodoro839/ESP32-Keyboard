/* Persistent configuration in NVS (API.md §7). All accessors are thread-safe. */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "esp_err.h"

#define CFG_DEFAULT_HOSTNAME   "espkb"
#define CFG_DEFAULT_AP_PASS    "espkb1234"
#define CFG_DEFAULT_KEY_DELAY  5

typedef struct {
    char hostname[33];
    char wifi_ssid[33];
    char wifi_password[64];
    char ap_password[64];
    char token[65];
    uint16_t key_delay_ms;
    bool ap_password_custom;
} espkb_config_t;

void config_init(void);
void config_get(espkb_config_t *out);
uint16_t config_key_delay(void);
bool config_token_required(void);
/* Checks a candidate token (not NUL-terminated) in constant time. */
bool config_token_ok(const char *tok, size_t len);

/* Partial update from a JSON object (POST /api/config). Validates everything
 * first; on any invalid value nothing is saved. Returns false and a message on
 * validation failure. */
bool config_update_json(const cJSON *obj, bool *reboot_required, char *msg, size_t msglen);

/* Console setters (validate, save). Return false with a message if invalid. */
bool config_set_wifi(const char *ssid, const char *password, char *msg, size_t msglen);
bool config_set_token(const char *token, char *msg, size_t msglen);
bool config_set_hostname(const char *hostname, char *msg, size_t msglen);
bool config_set_key_delay(long ms, char *msg, size_t msglen);

esp_err_t config_factory_reset(void);
