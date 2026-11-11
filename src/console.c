#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "config.h"
#include "console.h"
#include "espkb.h"
#include "hid_engine.h"
#include "usb_hid.h"
#include "wifi_mgr.h"

static const char *TAG = "console";

#define CON_UART   UART_NUM_0
#define CON_LINE_MAX   256
#define MAX_ARGS   4

static void out(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void out(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

/* Splits a line into arguments; double quotes group words, backslash escapes. */
static int tokenize(char *line, char **argv, int max)
{
    int argc = 0;
    char *r = line, *w = line;
    while (*r) {
        while (*r == ' ' || *r == '\t') {
            r++;
        }
        if (!*r) {
            break;
        }
        if (argc == max) {
            return -1;
        }
        argv[argc++] = w;
        bool quoted = false;
        while (*r && (quoted || (*r != ' ' && *r != '\t'))) {
            if (*r == '"') {
                quoted = !quoted;
                r++;
            } else if (*r == '\\' && r[1]) {
                *w++ = r[1];
                r += 2;
            } else {
                *w++ = *r++;
            }
        }
        if (*r) {
            r++;
        }
        *w++ = '\0';
    }
    return argc;
}

static void cmd_help(void)
{
    out("commands:\n"
        "  help                     this list\n"
        "  status                   show device state\n"
        "  wifi <ssid> [password]   set WiFi credentials (use \"\" for an empty SSID; reboot to apply)\n"
        "  token [<value>|-]        show, set or clear (-) the API token (immediate)\n"
        "  hostname <name>          set hostname (reboot to apply)\n"
        "  keydelay <ms>            set default delay between key reports, 0-1000 (immediate)\n"
        "  reboot                   restart the device\n"
        "  factory-reset            erase all settings and restart\n");
}

static void cmd_status(void)
{
    espkb_config_t c;
    config_get(&c);
    wifi_status_t w;
    wifi_mgr_get_status(&w);
    engine_status_t e;
    engine_get_status(&e);
    uint8_t leds = usb_hid_leds();
    out("espkb %s, uptime %lld s, heap free %lu (min %lu)\n", ESPKB_FW_VERSION, (long long)(now_us() / 1000000),
        (unsigned long)esp_get_free_heap_size(), (unsigned long)esp_get_minimum_free_heap_size());
    out("usb: mounted=%d suspended=%d remote_wakeup=%d leds(num=%d caps=%d scroll=%d) kbd=%s mouse=%s\n",
        usb_hid_mounted(), usb_hid_suspended(), usb_hid_remote_wakeup_allowed(), leds & 1, (leds >> 1) & 1,
        (leds >> 2) & 1, usb_hid_boot_protocol(ITF_KBD) ? "boot" : "report",
        usb_hid_boot_protocol(ITF_MOUSE) ? "boot" : "report");
    out("hid: busy=%d queued=%d held=%d reports=%lu done=%lu failed=%lu\n", e.busy, e.queued, e.nkeys,
        (unsigned long)e.reports_sent, (unsigned long)e.jobs_done, (unsigned long)e.jobs_failed);
    out("wifi: sta=%s ssid=\"%s\" ip=%s rssi=%d ap=%s (%s, %d clients) hostname=%s\n",
        w.sta_connected ? "connected" : "disconnected", w.ssid, w.ip[0] ? w.ip : "-", w.rssi,
        w.ap_active ? "on" : "off", w.ap_ssid, w.ap_clients, w.hostname);
    out("config: hostname=%s wifi_ssid=\"%s\" token=%s key_delay_ms=%u\n", c.hostname, c.wifi_ssid,
        c.token[0] ? "set" : "none", c.key_delay_ms);
}

static void restart_soon(void)
{
    engine_release_all();
    vTaskDelay(pdMS_TO_TICKS(300));
    esp_restart();
}

static void run_command(char *line)
{
    char *argv[MAX_ARGS];
    char msg[112];
    int argc = tokenize(line, argv, MAX_ARGS);
    if (argc < 0) {
        out("error: too many arguments\n");
        return;
    }
    if (argc == 0) {
        return;
    }
    const char *cmd = argv[0];
    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "?") == 0) {
        cmd_help();
    } else if (strcmp(cmd, "status") == 0) {
        cmd_status();
    } else if (strcmp(cmd, "wifi") == 0) {
        if (argc < 2 || argc > 3) {
            out("usage: wifi <ssid> [password]\n");
        } else if (config_set_wifi(argv[1], argc == 3 ? argv[2] : "", msg, sizeof(msg))) {
            out("ok: WiFi set to \"%s\"; reboot to apply\n", argv[1]);
        } else {
            out("error: %s\n", msg);
        }
    } else if (strcmp(cmd, "token") == 0) {
        if (argc == 1) {
            espkb_config_t c;
            config_get(&c);
            if (c.token[0]) {
                out("token: %s\n", c.token);
            } else {
                out("token: none (API open)\n");
            }
        } else if (argc == 2) {
            const char *t = strcmp(argv[1], "-") == 0 ? "" : argv[1];
            if (config_set_token(t, msg, sizeof(msg))) {
                out(t[0] ? "ok: token set\n" : "ok: token cleared\n");
            } else {
                out("error: %s\n", msg);
            }
        } else {
            out("usage: token [<value>|-]\n");
        }
    } else if (strcmp(cmd, "hostname") == 0) {
        if (argc != 2) {
            out("usage: hostname <name>\n");
        } else if (config_set_hostname(argv[1], msg, sizeof(msg))) {
            out("ok: hostname set to %s; reboot to apply\n", argv[1]);
        } else {
            out("error: %s\n", msg);
        }
    } else if (strcmp(cmd, "keydelay") == 0) {
        char *end = NULL;
        long v = argc == 2 ? strtol(argv[1], &end, 10) : -1;
        if (argc != 2 || !end || *end || end == argv[1]) {
            out("usage: keydelay <ms>\n");
        } else if (config_set_key_delay(v, msg, sizeof(msg))) {
            out("ok: key delay %ld ms\n", v);
        } else {
            out("error: %s\n", msg);
        }
    } else if (strcmp(cmd, "reboot") == 0) {
        out("rebooting\n");
        restart_soon();
    } else if (strcmp(cmd, "factory-reset") == 0) {
        out("factory reset: erasing settings and rebooting\n");
        config_factory_reset();
        restart_soon();
    } else {
        out("unknown command \"%s\" (try help)\n", cmd);
    }
}

static void console_task(void *arg)
{
    (void)arg;
    char line[CON_LINE_MAX];
    size_t len = 0;
    bool overflow = false;
    out("\nespkb console ready, type 'help'\nespkb> ");
    for (;;) {
        /* uart_read_bytes() waits for the full length: block for one byte,
         * then take whatever else is already buffered. */
        uint8_t buf[64];
        int n = uart_read_bytes(CON_UART, buf, 1, portMAX_DELAY);
        if (n == 1) {
            size_t avail = 0;
            uart_get_buffered_data_len(CON_UART, &avail);
            if (avail > sizeof(buf) - 1) {
                avail = sizeof(buf) - 1;
            }
            if (avail > 0) {
                int m = uart_read_bytes(CON_UART, buf + 1, avail, 0);
                n += m > 0 ? m : 0;
            }
        }
        for (int i = 0; i < n; i++) {
            char c = (char)buf[i];
            if (c == '\r' || c == '\n') {
                if (len > 0 || overflow) {
                    out("\n");
                    line[len] = '\0';
                    if (overflow) {
                        out("error: line too long\n");
                    } else {
                        run_command(line);
                    }
                    out("espkb> ");
                }
                len = 0;
                overflow = false;
            } else if (c == 0x08 || c == 0x7f) {
                if (len > 0) {
                    len--;
                    out("\b \b");
                }
            } else if (c >= 0x20 && c < 0x7f) {
                if (len < CON_LINE_MAX - 1) {
                    line[len++] = c;
                    out("%c", c);
                } else {
                    overflow = true;
                }
            }
        }
    }
}

void console_start(void)
{
    fflush(stdout);
    esp_err_t err = uart_driver_install(CON_UART, 1024, 4096, 0, NULL, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "UART driver install failed: %s", esp_err_to_name(err));
        return;
    }
    uart_vfs_dev_use_driver(CON_UART);
    xTaskCreate(console_task, "console", 4096, NULL, 3, NULL);
}
