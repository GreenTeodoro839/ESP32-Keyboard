#include <stdio.h>
#include <string.h>
#include "class/hid/hid_device.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "espkb.h"
#include "usb_hid.h"

static const char *TAG = "usb";

/* ---------- descriptors (API.md §9) ---------- */

static const uint8_t s_kbd_report_desc[] = {TUD_HID_REPORT_DESC_KEYBOARD()};
static const uint8_t s_mouse_report_desc[] = {TUD_HID_REPORT_DESC_MOUSE()};

static const tusb_desc_device_t s_device_desc = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0x00,      /* class defined per interface */
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x303A,
    .idProduct = 0x4004,
    .bcdDevice = 0x0100,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + 2 * TUD_HID_DESC_LEN)

static const uint8_t s_config_desc[] = {
    /* config 1, 2 interfaces, no string, bus powered + remote wakeup, 500 mA */
    TUD_CONFIG_DESCRIPTOR(1, 2, 0, CONFIG_TOTAL_LEN, TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 500),
    /* interface 0: boot keyboard, EP 0x81, 8 bytes, 1 ms */
    TUD_HID_DESCRIPTOR(0, 4, HID_ITF_PROTOCOL_KEYBOARD, sizeof(s_kbd_report_desc), 0x81, 8, 1),
    /* interface 1: boot mouse, EP 0x82, 8 bytes, 1 ms */
    TUD_HID_DESCRIPTOR(1, 5, HID_ITF_PROTOCOL_MOUSE, sizeof(s_mouse_report_desc), 0x82, 8, 1),
};

static char s_serial[13];
static const char *s_strings[] = {
    (const char[]){0x09, 0x04},   /* 0: English (0x0409) */
    "ESPKB",                      /* 1: manufacturer */
    "ESPKB Keyboard Mouse",       /* 2: product */
    s_serial,                     /* 3: serial number (MAC) */
    "ESPKB Keyboard",             /* 4: interface 0 */
    "ESPKB Mouse",                /* 5: interface 1 */
};

/* ---------- state ---------- */

static usb_hid_notify_fn s_notify;
static volatile bool s_inflight[2];
static volatile bool s_failed[2];
static volatile uint8_t s_leds;
static volatile bool s_boot[2];
static volatile bool s_remote_wakeup;
static volatile uint32_t s_generation;
static volatile uint32_t s_completed;
static portMUX_TYPE s_spin = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_last[2][8];   /* last submitted report, for GET_REPORT */

static void notify(uint32_t bits)
{
    if (s_notify) {
        s_notify(bits);
    }
}

static void reset_link_state(void)
{
    s_inflight[0] = s_inflight[1] = false;
    s_failed[0] = s_failed[1] = false;
    s_boot[0] = s_boot[1] = false;
    s_leds = 0;
    portENTER_CRITICAL(&s_spin);
    memset(s_last, 0, sizeof(s_last));
    portEXIT_CRITICAL(&s_spin);
    s_generation++;
}

static void usb_event_cb(tinyusb_event_t *event, void *arg)
{
    (void)arg;
    switch (event->id) {
    case TINYUSB_EVENT_ATTACHED:
        reset_link_state();
        ESP_LOGI(TAG, "mounted by host");
        break;
    case TINYUSB_EVENT_DETACHED:
        reset_link_state();
        s_remote_wakeup = false;
        ESP_LOGI(TAG, "unmounted");
        break;
    case TINYUSB_EVENT_SUSPENDED:
        s_remote_wakeup = event->suspended.remote_wakeup;
        ESP_LOGI(TAG, "suspended (remote wakeup %s)", s_remote_wakeup ? "allowed" : "not allowed");
        break;
    case TINYUSB_EVENT_RESUMED:
        ESP_LOGI(TAG, "resumed");
        break;
    default:
        return;
    }
    notify(USB_EVT_STATE);
    espkb_status_changed();
}

/* ---------- TinyUSB HID callbacks (run in the TinyUSB task) ---------- */

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance)
{
    return instance == ITF_MOUSE ? s_mouse_report_desc : s_kbd_report_desc;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen)
{
    (void)report_id;
    if (instance > ITF_MOUSE) {
        return 0;
    }
    if (report_type == HID_REPORT_TYPE_INPUT) {
        uint16_t len = instance == ITF_KBD ? 8 : (s_boot[ITF_MOUSE] ? 3 : 5);
        if (len > reqlen) {
            len = reqlen;
        }
        portENTER_CRITICAL(&s_spin);
        memcpy(buffer, s_last[instance], len);
        portEXIT_CRITICAL(&s_spin);
        return len;
    }
    if (report_type == HID_REPORT_TYPE_OUTPUT && instance == ITF_KBD && reqlen >= 1) {
        buffer[0] = s_leds;
        return 1;
    }
    return 0; /* stall */
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize)
{
    (void)report_id;
    if (instance != ITF_KBD || bufsize < 1 ||
        (report_type != HID_REPORT_TYPE_OUTPUT && report_type != HID_REPORT_TYPE_INVALID)) {
        return;
    }
    uint8_t leds = buffer[0];
    if (leds != s_leds) {
        s_leds = leds;
        espkb_status_changed();
    }
}

void tud_hid_set_protocol_cb(uint8_t instance, uint8_t protocol)
{
    if (instance > ITF_MOUSE) {
        return;
    }
    s_boot[instance] = (protocol == HID_PROTOCOL_BOOT);
    ESP_LOGI(TAG, "%s protocol: %s", instance == ITF_KBD ? "keyboard" : "mouse",
             s_boot[instance] ? "boot" : "report");
    espkb_status_changed();
}

void tud_hid_report_complete_cb(uint8_t instance, uint8_t const *report, uint16_t len)
{
    (void)report;
    (void)len;
    if (instance > ITF_MOUSE) {
        return;
    }
    s_completed++;
    s_inflight[instance] = false;
    notify(instance == ITF_KBD ? USB_EVT_DONE_KBD : USB_EVT_DONE_MOUSE);
}

void tud_hid_report_failed_cb(uint8_t instance, hid_report_type_t report_type, uint8_t const *report,
                              uint16_t xferred_bytes)
{
    (void)report;
    (void)xferred_bytes;
    if (instance > ITF_MOUSE || report_type != HID_REPORT_TYPE_INPUT) {
        return;
    }
    s_failed[instance] = true;
    s_inflight[instance] = false;
    notify(instance == ITF_KBD ? USB_EVT_DONE_KBD : USB_EVT_DONE_MOUSE);
}

/* ---------- public API ---------- */

bool usb_hid_mounted(void) { return tud_mounted(); }
bool usb_hid_suspended(void) { return tud_mounted() && tud_suspended(); }
bool usb_hid_ready(void) { return tud_ready(); }
bool usb_hid_remote_wakeup_allowed(void) { return s_remote_wakeup; }
uint8_t usb_hid_leds(void) { return s_leds; }
bool usb_hid_boot_protocol(int itf) { return (itf == ITF_KBD || itf == ITF_MOUSE) && s_boot[itf]; }
uint32_t usb_hid_generation(void) { return s_generation; }
bool usb_hid_inflight(int itf) { return s_inflight[itf]; }
uint32_t usb_hid_reports_completed(void) { return s_completed; }

bool usb_hid_submit(int itf, const uint8_t *data, uint16_t len)
{
    if (itf < 0 || itf > ITF_MOUSE || len > 8 || !tud_ready() || s_inflight[itf]) {
        return false;
    }
    portENTER_CRITICAL(&s_spin);
    memset(s_last[itf], 0, sizeof(s_last[itf]));
    memcpy(s_last[itf], data, len);
    portEXIT_CRITICAL(&s_spin);
    s_failed[itf] = false;
    s_inflight[itf] = true;
    if (!tud_hid_n_report((uint8_t)itf, 0, data, len)) {
        s_inflight[itf] = false;
        return false;
    }
    return true;
}

bool usb_hid_remote_wakeup(void)
{
    if (!tud_mounted() || !tud_suspended() || !s_remote_wakeup) {
        return false;
    }
    return tud_remote_wakeup();
}

esp_err_t usb_hid_init(usb_hid_notify_fn fn)
{
    s_notify = fn;
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_serial, sizeof(s_serial), "%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3], mac[4],
             mac[5]);

    static tinyusb_config_t cfg;
    cfg = TINYUSB_DEFAULT_CONFIG(usb_event_cb, NULL);
    cfg.task = TINYUSB_TASK_CUSTOM(4096, 12, 1);
    cfg.descriptor.device = &s_device_desc;
    cfg.descriptor.full_speed_config = s_config_desc;
    cfg.descriptor.string = s_strings;
    cfg.descriptor.string_count = sizeof(s_strings) / sizeof(s_strings[0]);
    esp_err_t err = tinyusb_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_driver_install failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "USB HID ready: 303a:4004 serial %s", s_serial);
    return ESP_OK;
}
