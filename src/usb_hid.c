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

/* Absolute mouse: the report of TinyUSB's TUD_HID_REPORT_DESC_ABSMOUSE
 * (hid_abs_mouse_report_t), but with three buttons instead of five. Linux
 * takes a device with absolute X/Y for a joystick as well (and creates
 * /dev/input/jsN for it) unless its buttons are exactly left, right and
 * middle, as on the absolute mice of virtual machines and remote consoles. */
static const uint8_t s_abs_report_desc[] = {
    HID_USAGE_PAGE(HID_USAGE_PAGE_DESKTOP),
    HID_USAGE(HID_USAGE_DESKTOP_MOUSE),
    HID_COLLECTION(HID_COLLECTION_APPLICATION),
        HID_USAGE(HID_USAGE_DESKTOP_POINTER),
        HID_COLLECTION(HID_COLLECTION_PHYSICAL),
            /* byte 0: buttons 1-3, 5 bits of padding */
            HID_USAGE_PAGE(HID_USAGE_PAGE_BUTTON),
            HID_USAGE_MIN(1),
            HID_USAGE_MAX(3),
            HID_LOGICAL_MIN(0),
            HID_LOGICAL_MAX(1),
            HID_REPORT_COUNT(3),
            HID_REPORT_SIZE(1),
            HID_INPUT(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
            HID_REPORT_COUNT(1),
            HID_REPORT_SIZE(5),
            HID_INPUT(HID_CONSTANT),
            /* bytes 1-4: X, Y absolute, 0..32767 */
            HID_USAGE_PAGE(HID_USAGE_PAGE_DESKTOP),
            HID_USAGE(HID_USAGE_DESKTOP_X),
            HID_USAGE(HID_USAGE_DESKTOP_Y),
            HID_LOGICAL_MIN(0),
            HID_LOGICAL_MAX_N(ABS_MAX, 2),
            HID_REPORT_SIZE(16),
            HID_REPORT_COUNT(2),
            HID_INPUT(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
            /* byte 5: wheel */
            HID_USAGE(HID_USAGE_DESKTOP_WHEEL),
            HID_LOGICAL_MIN(0x81),
            HID_LOGICAL_MAX(0x7f),
            HID_REPORT_COUNT(1),
            HID_REPORT_SIZE(8),
            HID_INPUT(HID_DATA | HID_VARIABLE | HID_RELATIVE),
            /* byte 6: AC Pan */
            HID_USAGE_PAGE(HID_USAGE_PAGE_CONSUMER),
            HID_USAGE_N(HID_USAGE_CONSUMER_AC_PAN, 2),
            HID_LOGICAL_MIN(0x81),
            HID_LOGICAL_MAX(0x7f),
            HID_REPORT_COUNT(1),
            HID_REPORT_SIZE(8),
            HID_INPUT(HID_DATA | HID_VARIABLE | HID_RELATIVE),
        HID_COLLECTION_END,
    HID_COLLECTION_END,
};

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

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + ITF_COUNT * TUD_HID_DESC_LEN)

static const uint8_t s_config_desc[] = {
    /* config 1, 3 interfaces, no string, bus powered + remote wakeup, 500 mA */
    TUD_CONFIG_DESCRIPTOR(1, ITF_COUNT, 0, CONFIG_TOTAL_LEN, TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 500),
    /* interface 0: boot keyboard, EP 0x81, 8 bytes, 1 ms */
    TUD_HID_DESCRIPTOR(0, 4, HID_ITF_PROTOCOL_KEYBOARD, sizeof(s_kbd_report_desc), 0x81, 8, 1),
    /* interface 1: boot mouse, EP 0x82, 8 bytes, 1 ms */
    TUD_HID_DESCRIPTOR(1, 5, HID_ITF_PROTOCOL_MOUSE, sizeof(s_mouse_report_desc), 0x82, 8, 1),
    /* interface 2: absolute mouse (no boot protocol), EP 0x83, 8 bytes, 1 ms */
    TUD_HID_DESCRIPTOR(2, 6, HID_ITF_PROTOCOL_NONE, sizeof(s_abs_report_desc), 0x83, 8, 1),
};

static char s_serial[13];
static const char *s_strings[] = {
    (const char[]){0x09, 0x04},   /* 0: English (0x0409) */
    "ESPKB",                      /* 1: manufacturer */
    "ESPKB Keyboard Mouse",       /* 2: product */
    s_serial,                     /* 3: serial number (MAC) */
    "ESPKB Keyboard",             /* 4: interface 0 */
    "ESPKB Mouse",                /* 5: interface 1 */
    "ESPKB Absolute Mouse",       /* 6: interface 2 */
};

/* ---------- state ---------- */

static usb_hid_notify_fn s_notify;
static volatile bool s_inflight[ITF_COUNT];
static volatile bool s_failed[ITF_COUNT];
static volatile uint8_t s_leds;
static volatile bool s_boot[2];   /* keyboard and relative mouse only */
static volatile bool s_remote_wakeup;
static volatile uint32_t s_generation;
static volatile uint32_t s_completed;
static portMUX_TYPE s_spin = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_last[ITF_COUNT][8];   /* last submitted report, for GET_REPORT */

static const uint32_t s_done_evt[ITF_COUNT] = {USB_EVT_DONE_KBD, USB_EVT_DONE_MOUSE, USB_EVT_DONE_ABS};

static void notify(uint32_t bits)
{
    if (s_notify) {
        s_notify(bits);
    }
}

static void reset_link_state(void)
{
    for (int i = 0; i < ITF_COUNT; i++) {
        s_inflight[i] = false;
        s_failed[i] = false;
    }
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
    switch (instance) {
    case ITF_MOUSE:
        return s_mouse_report_desc;
    case ITF_ABS:
        return s_abs_report_desc;
    default:
        return s_kbd_report_desc;
    }
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen)
{
    (void)report_id;
    if (instance >= ITF_COUNT) {
        return 0;
    }
    if (report_type == HID_REPORT_TYPE_INPUT) {
        uint16_t len = instance == ITF_KBD ? 8 : instance == ITF_ABS ? ABS_REPORT_LEN : (s_boot[ITF_MOUSE] ? 3 : 5);
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
        return; /* the absolute interface has no boot protocol: its report format is fixed */
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
    if (instance >= ITF_COUNT) {
        return;
    }
    s_completed++;
    s_inflight[instance] = false;
    notify(s_done_evt[instance]);
}

void tud_hid_report_failed_cb(uint8_t instance, hid_report_type_t report_type, uint8_t const *report,
                              uint16_t xferred_bytes)
{
    (void)report;
    (void)xferred_bytes;
    if (instance >= ITF_COUNT || report_type != HID_REPORT_TYPE_INPUT) {
        return;
    }
    s_failed[instance] = true;
    s_inflight[instance] = false;
    notify(s_done_evt[instance]);
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
    if (itf < 0 || itf >= ITF_COUNT || len > 8 || !tud_ready() || s_inflight[itf]) {
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
