#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/ip4_addr.h"
#include "mdns.h"
#include "config.h"
#include "wifi_mgr.h"

static const char *TAG = "wifi";

#define AP_FALLBACK_US     (20 * 1000000LL)   /* API.md §7: 20 s without STA -> AP */
#define AP_STOP_SETTLE_US  (3 * 1000000LL)
#define ATTEMPT_STUCK_US   (25 * 1000000LL)
#define SCAN_TIMEOUT_US    (10 * 1000000LL)
#define SCAN_START_WAIT_US (6 * 1000000LL)
#define MAX_SCAN_WAITERS   4
#define MAX_SCAN_RESULTS   32

typedef struct {
    wifi_scan_cb_t cb;
    void *arg;
} scan_waiter_t;

static struct {
    SemaphoreHandle_t mx;
    TaskHandle_t task;
    esp_netif_t *sta_netif;
    char hostname[33];
    char ssid[33];
    char password[64];
    char ap_ssid[33];
    char ap_password[64];
    bool sta_configured;

    bool sta_connected;
    uint32_t ip;               /* network byte order, 0 = none */
    bool connecting;
    int fails;
    int64_t attempt_start_us;
    int64_t last_fail_us;
    int64_t disc_since_us;
    int64_t connected_since_us;

    bool ap_active;
    int ap_clients;
    int64_t ap_idle_since_us;

    scan_waiter_t waiters[MAX_SCAN_WAITERS];
    int nwaiters;
    bool scanning;
    bool scan_done;
    int64_t scan_req_us;
    int64_t scan_start_us;
} W;

static void lock(void) { xSemaphoreTake(W.mx, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(W.mx); }
static void kick(void)
{
    if (W.task) {
        xTaskNotifyGive(W.task);
    }
}

/* ---------- events (default event loop task) ---------- */

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    int64_t now = now_us();
    bool changed = false;
    lock();
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_STA_DISCONNECTED: {
            const wifi_event_sta_disconnected_t *d = data;
            if (W.sta_connected) {
                W.disc_since_us = now;
                ESP_LOGW(TAG, "STA disconnected (reason %d)", d->reason);
                changed = true;
            } else if (W.fails < 3 || W.fails % 20 == 0) {
                ESP_LOGI(TAG, "STA connect attempt failed (reason %d)", d->reason);
            }
            W.sta_connected = false;
            W.ip = 0;
            W.connecting = false;
            W.fails++;
            W.last_fail_us = now;
            break;
        }
        case WIFI_EVENT_AP_STACONNECTED:
            W.ap_clients++;
            changed = true;
            break;
        case WIFI_EVENT_AP_STADISCONNECTED:
            if (W.ap_clients > 0) {
                W.ap_clients--;
            }
            if (W.ap_clients == 0) {
                W.ap_idle_since_us = now;
            }
            changed = true;
            break;
        case WIFI_EVENT_SCAN_DONE:
            W.scan_done = true;
            break;
        default:
            break;
        }
    } else if (base == IP_EVENT) {
        if (id == IP_EVENT_STA_GOT_IP) {
            const ip_event_got_ip_t *e = data;
            W.ip = e->ip_info.ip.addr;
            W.sta_connected = true;
            W.connecting = false;
            W.fails = 0;
            W.connected_since_us = now;
            ESP_LOGI(TAG, "STA connected, IP " IPSTR, IP2STR(&e->ip_info.ip));
            changed = true;
        } else if (id == IP_EVENT_STA_LOST_IP) {
            W.ip = 0;
            changed = true;
        }
    }
    unlock();
    if (changed) {
        espkb_status_changed();
    }
    kick();
}

/* ---------- AP control (manager task) ---------- */

static void start_ap(void)
{
    wifi_config_t ap = {0};
    size_t n = strlen(W.ap_ssid);
    memcpy(ap.ap.ssid, W.ap_ssid, n);
    ap.ap.ssid_len = (uint8_t)n;
    snprintf((char *)ap.ap.password, sizeof(ap.ap.password), "%s", W.ap_password);
    ap.ap.channel = 1;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.max_connection = 4;
    ap.ap.pmf_cfg.required = false;
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_APSTA);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "starting AP failed: %s", esp_err_to_name(err));
        return;
    }
    lock();
    W.ap_active = true;
    W.ap_clients = 0;
    W.ap_idle_since_us = now_us();
    unlock();
    ESP_LOGI(TAG, "provisioning AP \"%s\" active at " ESPKB_AP_IP, W.ap_ssid);
    espkb_status_changed();
}

static void stop_ap(void)
{
    if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK) {
        return;
    }
    lock();
    W.ap_active = false;
    W.ap_clients = 0;
    unlock();
    ESP_LOGI(TAG, "provisioning AP stopped");
    espkb_status_changed();
}

/* Reconnect pacing. While the fallback AP is up, STA attempts (which scan all
 * channels) disturb AP clients, so they are spaced out much more. */
static int64_t retry_delay_us(void)
{
    if (W.ap_active) {
        return (W.ap_clients > 0 ? 30 : 6) * 1000000LL;
    }
    static const int steps[] = {1, 2, 3, 5};
    int i = W.fails - 1;
    if (i < 0) {
        i = 0;
    }
    if (i > 3) {
        i = 3;
    }
    return steps[i] * 1000000LL;
}

/* ---------- scanning ---------- */

static void sanitize_ssid(const uint8_t *in, char *out, size_t cap)
{
    size_t o = 0;
    for (size_t i = 0; i < 32 && in[i] && o + 5 < cap;) {
        uint8_t c = in[i];
        int extra = (c & 0xE0) == 0xC0 ? 1 : (c & 0xF0) == 0xE0 ? 2 : (c & 0xF8) == 0xF0 ? 3 : 0;
        bool ok = c >= 0x20 && c != 0x7f && (c < 0x80 || extra > 0);
        for (int k = 1; ok && k <= extra; k++) {
            ok = i + k < 32 && (in[i + k] & 0xC0) == 0x80;
        }
        if (!ok) {
            out[o++] = '?';
            i++;
            continue;
        }
        memcpy(out + o, in + i, (size_t)extra + 1);
        o += (size_t)extra + 1;
        i += (size_t)extra + 1;
    }
    out[o] = '\0';
}

static char *scan_results_json(void)
{
    uint16_t n = MAX_SCAN_RESULTS;
    wifi_ap_record_t *recs = espkb_calloc(n, sizeof(wifi_ap_record_t));
    if (!recs) {
        esp_wifi_clear_ap_list();
        return NULL;
    }
    if (esp_wifi_scan_get_ap_records(&n, recs) != ESP_OK) {
        n = 0;
    }
    cJSON *root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON *aps = cJSON_AddArrayToObject(root, "aps");
    char (*names)[33 * 4] = espkb_calloc(MAX_SCAN_RESULTS, sizeof(*names));
    int nnames = 0;
    for (int i = 0; i < n && aps && names; i++) {
        char ssid[33 * 4];
        sanitize_ssid(recs[i].ssid, ssid, sizeof(ssid));
        if (!ssid[0]) {
            continue; /* hidden network */
        }
        bool dup = false;
        for (int k = 0; k < nnames; k++) {
            if (strcmp(names[k], ssid) == 0) {
                dup = true; /* records are sorted by RSSI: keep the strongest */
                break;
            }
        }
        if (dup) {
            continue;
        }
        snprintf(names[nnames++], sizeof(names[0]), "%s", ssid);
        cJSON *ap = cJSON_CreateObject();
        cJSON_AddStringToObject(ap, "ssid", ssid);
        cJSON_AddNumberToObject(ap, "rssi", recs[i].rssi);
        cJSON_AddNumberToObject(ap, "channel", recs[i].primary);
        cJSON_AddBoolToObject(ap, "secure", recs[i].authmode != WIFI_AUTH_OPEN);
        cJSON_AddItemToArray(aps, ap);
    }
    espkb_free(names);
    espkb_free(recs);
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}

static void finish_scan(espkb_err_t err, char *json)
{
    scan_waiter_t w[MAX_SCAN_WAITERS];
    lock();
    int nw = W.nwaiters;
    memcpy(w, W.waiters, sizeof(w));
    W.nwaiters = 0;
    W.scanning = false;
    W.scan_done = false;
    unlock();
    for (int i = 0; i < nw; i++) {
        char *copy = NULL;
        espkb_err_t e = err;
        if (!e && json) {
            copy = strdup(json);
            if (!copy) {
                e = EK_BUSY;
            }
        }
        w[i].cb(w[i].arg, e, copy);
    }
    cJSON_free(json);
}

bool wifi_mgr_scan(wifi_scan_cb_t cb, void *arg)
{
    lock();
    if (W.nwaiters >= MAX_SCAN_WAITERS) {
        unlock();
        return false;
    }
    if (W.nwaiters == 0 && !W.scanning) {
        W.scan_req_us = now_us();
    }
    W.waiters[W.nwaiters].cb = cb;
    W.waiters[W.nwaiters].arg = arg;
    W.nwaiters++;
    unlock();
    kick();
    return true;
}

static void scan_step(int64_t now)
{
    lock();
    bool want = W.nwaiters > 0 && !W.scanning;
    bool scanning = W.scanning;
    bool done = W.scan_done;
    bool connecting = W.connecting;
    int64_t req = W.scan_req_us, started = W.scan_start_us;
    unlock();

    if (want) {
        esp_err_t err = ESP_ERR_WIFI_STATE;
        if (!connecting) {
            wifi_scan_config_t sc = {.show_hidden = false};
            err = esp_wifi_scan_start(&sc, false);
        }
        if (err == ESP_OK) {
            lock();
            W.scanning = true;
            W.scan_done = false;
            W.scan_start_us = now;
            unlock();
        } else if (now - req > SCAN_START_WAIT_US) {
            ESP_LOGW(TAG, "scan could not start: %s", esp_err_to_name(err));
            finish_scan(EK_BUSY, NULL);
        }
        return;
    }
    if (scanning && done) {
        finish_scan(EK_NONE, scan_results_json());
    } else if (scanning && now - started > SCAN_TIMEOUT_US) {
        esp_wifi_scan_stop();
        esp_wifi_clear_ap_list();
        finish_scan(EK_BUSY, NULL);
    }
}

/* ---------- manager task ---------- */

static void manager_task(void *arg)
{
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(250));
        int64_t now = now_us();
        scan_step(now);

        lock();
        bool connected = W.sta_connected;
        bool connecting = W.connecting;
        bool scanning = W.scanning;
        bool ap = W.ap_active;
        int clients = W.ap_clients;
        bool stuck = connecting && now - W.attempt_start_us > ATTEMPT_STUCK_US;
        bool attempt = W.sta_configured && !connected && !connecting && !scanning &&
                       now - W.last_fail_us >= retry_delay_us();
        bool want_ap = !W.sta_configured || (!connected && now - W.disc_since_us >= AP_FALLBACK_US);
        bool drop_ap = ap && W.sta_configured && connected && clients == 0 &&
                       now - W.connected_since_us >= AP_STOP_SETTLE_US &&
                       now - W.ap_idle_since_us >= AP_STOP_SETTLE_US;
        if (attempt) {
            W.connecting = true;
            W.attempt_start_us = now;
        }
        unlock();

        if (stuck) {
            ESP_LOGW(TAG, "connect attempt stuck, restarting it");
            esp_wifi_disconnect();
            lock();
            W.connecting = false;
            W.last_fail_us = now;
            unlock();
        }
        if (attempt) {
            esp_err_t err = esp_wifi_connect();
            if (err != ESP_OK) {
                lock();
                W.connecting = false;
                W.last_fail_us = now;
                unlock();
            }
        }
        if (want_ap && !ap) {
            start_ap();
        } else if (drop_ap) {
            stop_ap();
        }
    }
}

/* ---------- public ---------- */

void wifi_mgr_start(void)
{
    W.mx = xSemaphoreCreateMutex();
    espkb_config_t cfg;
    config_get(&cfg);
    snprintf(W.hostname, sizeof(W.hostname), "%s", cfg.hostname);
    snprintf(W.ssid, sizeof(W.ssid), "%s", cfg.wifi_ssid);
    snprintf(W.password, sizeof(W.password), "%s", cfg.wifi_password);
    snprintf(W.ap_password, sizeof(W.ap_password), "%s", cfg.ap_password);
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(W.ap_ssid, sizeof(W.ap_ssid), "ESPKB-%02X%02X", mac[4], mac[5]);
    W.sta_configured = W.ssid[0] != '\0';
    W.disc_since_us = now_us();
    W.last_fail_us = -1000000000LL;

    W.sta_netif = esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    esp_netif_set_hostname(W.sta_netif, W.hostname);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(W.sta_configured ? WIFI_MODE_STA : WIFI_MODE_APSTA));
    if (W.sta_configured) {
        wifi_config_t sta = {0};
        memcpy(sta.sta.ssid, W.ssid, strlen(W.ssid));
        snprintf((char *)sta.sta.password, sizeof(sta.sta.password), "%s", W.password);
        sta.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
        sta.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
        sta.sta.threshold.authmode = W.password[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
        sta.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);
    if (!W.sta_configured) {
        start_ap();
    }

    if (mdns_init() == ESP_OK) {
        mdns_hostname_set(W.hostname);
        mdns_instance_name_set("ESPKB keyboard/mouse");
        mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    } else {
        ESP_LOGE(TAG, "mDNS init failed");
    }

    xTaskCreatePinnedToCore(manager_task, "wifi_mgr", 6144, NULL, 4, &W.task, 0);
    ESP_LOGI(TAG, "hostname %s, STA %s%s%s", W.hostname, W.sta_configured ? "\"" : "",
             W.sta_configured ? W.ssid : "not configured", W.sta_configured ? "\"" : "");
}

void wifi_mgr_get_status(wifi_status_t *st)
{
    memset(st, 0, sizeof(*st));
    lock();
    st->sta_connected = W.sta_connected;
    snprintf(st->ssid, sizeof(st->ssid), "%s", W.ssid);
    if (W.ip) {
        esp_ip4_addr_t a = {.addr = W.ip};
        snprintf(st->ip, sizeof(st->ip), IPSTR, IP2STR(&a));
    }
    st->ap_active = W.ap_active;
    st->ap_clients = W.ap_clients;
    snprintf(st->ap_ssid, sizeof(st->ap_ssid), "%s", W.ap_ssid);
    snprintf(st->hostname, sizeof(st->hostname), "%s", W.hostname);
    unlock();
    if (st->sta_connected) {
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            st->rssi = ap.rssi;
        }
    }
}

bool wifi_mgr_is_own_host(const char *host)
{
    char h[80];
    size_t n = strlen(host);
    if (n == 0 || n >= sizeof(h)) {
        return false;
    }
    memcpy(h, host, n + 1);
    if (n > 3 && strcmp(h + n - 3, ":80") == 0) {
        n -= 3;
        h[n] = '\0';
    }
    if (n > 0 && h[n - 1] == '.') {
        h[--n] = '\0';
    }
    if (strcmp(h, ESPKB_AP_IP) == 0) {
        return true;
    }
    char want[48];
    lock();
    uint32_t ip = W.ip;
    snprintf(want, sizeof(want), "%s", W.hostname);
    unlock();
    if (ip) {
        char ips[16];
        esp_ip4_addr_t a = {.addr = ip};
        snprintf(ips, sizeof(ips), IPSTR, IP2STR(&a));
        if (strcmp(h, ips) == 0) {
            return true;
        }
    }
    if (str_ieq(h, want)) {
        return true;
    }
    strncat(want, ".local", sizeof(want) - strlen(want) - 1);
    return str_ieq(h, want);
}
