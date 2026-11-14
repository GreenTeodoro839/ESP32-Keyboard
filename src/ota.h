/* Firmware update over WiFi (API.md §11).
 *
 * POST /api/ota streams an app image into the inactive OTA slot from a task of
 * its own (the HTTP server keeps serving), verifies it, makes it the boot
 * image and reboots. Only the app slot is written: the settings in NVS stay.
 *
 * A new image runs "on probation" after its first boot: it is confirmed once
 * the network has been up for a while. If it crashes before that, or cannot
 * bring up the network at all, the bootloader returns to the previous image. */
#pragma once

#include <stdbool.h>
#include "cJSON.h"
#include "esp_http_server.h"

/* Call once after wifi_mgr_start() and before the HTTP server starts. */
void ota_init(void);

/* POST /api/ota, after the access check. Always answers the request (now, or
 * from the upload task when the image has been taken). */
esp_err_t ota_handle_upload(httpd_req_t *req);

/* Body of GET /api/ota (without "ok"): running image, free slot, upload state. */
void ota_add_info(cJSON *obj);

/* Status members (API.md §4.1): device.build / device.sha256 / device.partition,
 * and the top-level "ota" object. */
void ota_add_device_info(cJSON *device);
void ota_add_status(cJSON *root);

/* Label of the running app partition ("ota_0"); never NULL. No locking. */
const char *ota_running_partition(void);

/* True while an upload is being received or verified (used by the status LED). */
bool ota_in_progress(void);
