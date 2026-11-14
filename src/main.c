/* ESPKB: ESP32-S3 WiFi-controlled USB keyboard + mouse (see docs/API.md).
 *
 * Tasks:
 *   hid_engine  - the only task that sends HID reports; runs jobs from one FIFO
 *   TinyUSB     - USB device stack (callbacks feed the engine via event bits)
 *   httpd       - HTTP server and WebSocket frames (never waits for jobs)
 *   http_resp   - answers synchronous HTTP requests when their job finishes
 *   ws_push     - WebSocket status pushes
 *   wifi_mgr    - STA reconnect, fallback AP, scans
 *   ota         - only while a firmware image is being uploaded (POST /api/ota)
 *   console     - UART0 command line
 *   button      - BOOT button: short press toggles LED quiet mode, 5 s hold = factory reset
 */
#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "config.h"
#include "console.h"
#include "espkb.h"
#include "hid_engine.h"
#include "http_api.h"
#include "ota.h"
#include "status_led.h"
#include "wifi_mgr.h"

static const char *TAG = "main";

#define BOOT_BUTTON       GPIO_NUM_0
#define FACTORY_RESET_MS  5000
#define SHORT_PRESS_MAX_MS 1000   /* released before this = short press */

static void button_task(void *arg)
{
    (void)arg;
    const gpio_config_t io = {
        .pin_bit_mask = 1ULL << BOOT_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    int held_ms = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(50));
        if (gpio_get_level(BOOT_BUTTON) == 0) {
            held_ms += 50;
            if (held_ms == 1000) {
                ESP_LOGW(TAG, "BOOT button held: keep holding for factory reset");
            }
            if (held_ms >= FACTORY_RESET_MS) {
                ESP_LOGW(TAG, "factory reset by BOOT button");
                engine_release_all();
                config_factory_reset();
                vTaskDelay(pdMS_TO_TICKS(300));
                esp_restart();
            }
        } else {
            if (held_ms > 0 && held_ms < SHORT_PRESS_MAX_MS) {
                char msg[64];
                bool quiet = !config_led_quiet();
                if (config_set_led_quiet(quiet, msg, sizeof(msg))) {
                    ESP_LOGI(TAG, "quiet mode %s (short BOOT press)", quiet ? "on" : "off");
                } else {
                    ESP_LOGW(TAG, "toggling quiet mode failed: %s", msg);
                }
            }
            held_ms = 0;
        }
    }
}

static void init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS partition unusable (%s), erasing", esp_err_to_name(err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

void app_main(void)
{
    /* The HTTP server logs every dropped client at warning level; under load
     * that floods the 115200 baud console and stalls the logging tasks. */
    static const char *const quiet[] = {"httpd", "httpd_txrx", "httpd_parse", "httpd_uri", "httpd_sess", "httpd_ws"};
    for (size_t i = 0; i < sizeof(quiet) / sizeof(quiet[0]); i++) {
        esp_log_level_set(quiet[i], ESP_LOG_ERROR);
    }

    cJSON_Hooks hooks = {.malloc_fn = espkb_malloc, .free_fn = espkb_free};
    cJSON_InitHooks(&hooks);

    init_nvs();
    config_init();
    console_start();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    engine_init();      /* USB device + HID engine */
    wifi_mgr_start();
    ota_init();         /* after WiFi: a freshly updated image is confirmed once the network is up */
    status_led_start(); /* after WiFi/OTA: it shows their state */
    http_api_start();
    xTaskCreate(button_task, "button", 3072, NULL, 2, NULL);

    ESP_LOGI(TAG, "espkb %s ready", ESPKB_FW_VERSION);
}
