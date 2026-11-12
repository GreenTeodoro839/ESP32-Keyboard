#include "esp_system.h"
#include "config.h"
#include "espkb.h"
#include "hid_engine.h"
#include "keymap.h"
#include "ota.h"
#include "status.h"
#include "usb_hid.h"
#include "wifi_mgr.h"
#include "ws.h"

static const char *reset_reason_str(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "pwr_glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu_lockup";
    default: return "unknown";
    }
}

cJSON *status_build(bool with_ok)
{
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return NULL;
    }
    if (with_ok) {
        cJSON_AddBoolToObject(root, "ok", true);
    }

    cJSON *dev = cJSON_AddObjectToObject(root, "device");
    if (dev) {
        cJSON_AddStringToObject(dev, "name", ESPKB_NAME);
        cJSON_AddNumberToObject(dev, "api", ESPKB_API_VERSION);
        cJSON_AddStringToObject(dev, "version", ESPKB_FW_VERSION);
        cJSON_AddNumberToObject(dev, "uptime_ms", (double)(now_us() / 1000));
        cJSON_AddNumberToObject(dev, "heap_free", (double)esp_get_free_heap_size());
        cJSON_AddNumberToObject(dev, "heap_min", (double)esp_get_minimum_free_heap_size());
        cJSON_AddStringToObject(dev, "reset_reason", reset_reason_str());
        ota_add_device_info(dev);
    }

    cJSON *usb = cJSON_AddObjectToObject(root, "usb");
    if (usb) {
        uint8_t leds = usb_hid_leds();
        cJSON_AddBoolToObject(usb, "mounted", usb_hid_mounted());
        cJSON_AddBoolToObject(usb, "suspended", usb_hid_suspended());
        cJSON_AddBoolToObject(usb, "remote_wakeup", usb_hid_remote_wakeup_allowed());
        cJSON *l = cJSON_AddObjectToObject(usb, "leds");
        if (l) {
            cJSON_AddBoolToObject(l, "num", leds & 0x01);
            cJSON_AddBoolToObject(l, "caps", leds & 0x02);
            cJSON_AddBoolToObject(l, "scroll", leds & 0x04);
        }
        cJSON_AddStringToObject(usb, "kbd_protocol", usb_hid_boot_protocol(ITF_KBD) ? "boot" : "report");
        cJSON_AddStringToObject(usb, "mouse_protocol", usb_hid_boot_protocol(ITF_MOUSE) ? "boot" : "report");
    }

    engine_status_t es;
    engine_get_status(&es);
    cJSON *hid = cJSON_AddObjectToObject(root, "hid");
    if (hid) {
        cJSON_AddBoolToObject(hid, "busy", es.busy);
        cJSON_AddNumberToObject(hid, "queued", es.queued);
        cJSON *keys = cJSON_AddArrayToObject(hid, "keys");
        for (int i = 0; keys && i < es.nkeys; i++) {
            cJSON_AddItemToArray(keys, cJSON_CreateNumber(es.keys[i]));
        }
        cJSON *btns = cJSON_AddArrayToObject(hid, "buttons");
        for (int i = 0; btns && i < MOUSE_BUTTON_COUNT; i++) {
            if (es.buttons & (1u << i)) {
                cJSON_AddItemToArray(btns, cJSON_CreateString(keymap_button_name(i)));
            }
        }
        cJSON_AddNumberToObject(hid, "reports_sent", es.reports_sent);
        cJSON_AddNumberToObject(hid, "jobs_done", es.jobs_done);
        cJSON_AddNumberToObject(hid, "jobs_failed", es.jobs_failed);
    }

    wifi_status_t ws;
    wifi_mgr_get_status(&ws);
    cJSON *wifi = cJSON_AddObjectToObject(root, "wifi");
    if (wifi) {
        cJSON_AddBoolToObject(wifi, "sta_connected", ws.sta_connected);
        cJSON_AddStringToObject(wifi, "ssid", ws.ssid);
        cJSON_AddStringToObject(wifi, "ip", ws.ip);
        cJSON_AddNumberToObject(wifi, "rssi", ws.rssi);
        cJSON_AddBoolToObject(wifi, "ap_active", ws.ap_active);
        cJSON_AddStringToObject(wifi, "ap_ssid", ws.ap_ssid);
        cJSON_AddStringToObject(wifi, "ap_ip", ESPKB_AP_IP);
        cJSON_AddStringToObject(wifi, "hostname", ws.hostname);
    }

    ota_add_status(root);
    cJSON_AddNumberToObject(root, "ws_clients", ws_client_count());
    cJSON_AddBoolToObject(root, "auth_required", config_token_required());
    return root;
}
