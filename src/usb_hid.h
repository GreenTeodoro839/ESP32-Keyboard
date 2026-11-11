/* USB device: descriptors (API.md §9), TinyUSB callbacks and the low-level
 * report path. Only the HID engine task submits reports. */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#define ITF_KBD   0
#define ITF_MOUSE 1

#define USB_EVT_DONE_KBD   (1u << 0)
#define USB_EVT_DONE_MOUSE (1u << 1)
#define USB_EVT_STATE      (1u << 2)   /* mount/unmount/suspend/resume */

/* Called from the TinyUSB task with USB_EVT_* bits. Must not block. */
typedef void (*usb_hid_notify_fn)(uint32_t bits);

esp_err_t usb_hid_init(usb_hid_notify_fn notify);

bool usb_hid_mounted(void);
bool usb_hid_suspended(void);
bool usb_hid_ready(void);                /* mounted and not suspended */
bool usb_hid_remote_wakeup_allowed(void);
uint8_t usb_hid_leds(void);              /* bit0 Num, bit1 Caps, bit2 Scroll */
bool usb_hid_boot_protocol(int itf);
/* Incremented on every mount and unmount; a change means the host re-enumerated. */
uint32_t usb_hid_generation(void);

/* Report path. usb_hid_submit() returns false if the report was not queued
 * (not ready, or a transfer is still in flight). */
bool usb_hid_inflight(int itf);
bool usb_hid_submit(int itf, const uint8_t *data, uint16_t len);
uint32_t usb_hid_reports_completed(void);
bool usb_hid_remote_wakeup(void);
