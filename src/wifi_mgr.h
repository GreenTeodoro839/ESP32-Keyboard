/* WiFi (API.md §7): STA with endless reconnect, fallback/provisioning SoftAP,
 * power save off, mDNS hostname and _http._tcp service, scanning. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "espkb.h"

typedef struct {
    bool sta_connected;
    char ssid[33];
    char ip[16];
    int rssi;
    bool ap_active;
    int ap_clients;
    char ap_ssid[33];
    char hostname[33];
} wifi_status_t;

void wifi_mgr_start(void);
void wifi_mgr_get_status(wifi_status_t *st);

/* True if the Host header value names this device (STA IP, AP IP,
 * <hostname> or <hostname>.local, optionally with ":80"). */
bool wifi_mgr_is_own_host(const char *host);

/* Asynchronous scan. cb is called once from the WiFi manager task with either
 * a malloc'd JSON body (ownership passes to cb) or an error. Returns false if
 * too many scans are already waiting. */
typedef void (*wifi_scan_cb_t)(void *arg, espkb_err_t err, char *json);
bool wifi_mgr_scan(wifi_scan_cb_t cb, void *arg);
