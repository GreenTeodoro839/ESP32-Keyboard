/* Onboard WS2812 RGB LED, data on GPIO48 (ESP32-S3 SuperMini).
 *
 * On this board a plain red LED (next to the USB-C connector) is wired in
 * parallel with the WS2812 data input on GPIO48, so it cannot be controlled
 * separately. The sequence that keeps both happy, verified on hardware:
 *
 *   1. copy encoder: one long low reset pulse (clean frame start)
 *   2. bytes encoder: the 24 colour bits, ending low so the WS2812 latches
 *   3. wait a couple of ms, then take the pin back from RMT and drive it high
 *      as plain GPIO -- the red LED stays lit while the WS2812 holds its colour
 *
 * The next frame recreates the RMT channel and starts with another reset.
 *
 *   no WiFi link (provisioning AP, or reconnecting)  blue slow blink
 *   connected                                        WS2812 off (red stays lit)
 *   firmware upload in progress (POST /api/ota)      white fast blink
 *
 * Only blue and white are used: both look the same whichever RGB/GRB order a
 * clone uses. GPIO48 is SPICLK_N on ESP32-S3 variants with octal PSRAM, so the
 * LED is compiled out for those targets (the current board is an FH4R2 with
 * quad PSRAM; nothing else in the firmware uses GPIO48). */
#include <string.h>
#include "driver/gpio.h"
#include "driver/rmt_encoder.h"
#include "driver/rmt_tx.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "soc/gpio_sig_map.h"
#include "ota.h"
#include "status_led.h"
#include "wifi_mgr.h"

static const char *TAG = "status_led";

#define LED_GPIO          GPIO_NUM_48
#define LED_RES_HZ        (10 * 1000 * 1000)   /* 0.1 us per RMT tick */
#define LED_TASK_STACK    3072
#define LED_POLL_MS       50

#define BLINK_SLOW_MS     500                 /* no link */
#define BLINK_FAST_MS     100                 /* OTA upload */

/* 500 us: enough for WS2812B (50 us) and WS2812B-V5 (280 us). */
#define RESET_TICKS       5000
#define LATCH_MS          2

/* Refresh the latched colour even when it did not change, so a frame lost to
 * noise cannot leave the WS2812 showing a stale colour. */
#define REFRESH_US        (2000000)

/* Dim enough not to glare, bright enough to see across a desk. */
#define LEVEL             40

static const uint8_t BLUE[3]     = {0, 0, LEVEL};        /* G, R, B (wire order) */
static const uint8_t WHITE[3]    = {LEVEL, LEVEL, LEVEL};
static const uint8_t DARK[3]     = {0, 0, 0};

#if !defined(CONFIG_SPIRAM_MODE_OCT)

static rmt_channel_handle_t s_chan;
static rmt_encoder_handle_t s_enc_reset;   /* copy encoder: one reset symbol */
static rmt_encoder_handle_t s_enc_bytes;   /* bytes encoder: 24 colour bits */

static void rmt_teardown(void)
{
    if (s_chan) {
        rmt_disable(s_chan);
    }
    if (s_enc_reset) {
        rmt_del_encoder(s_enc_reset);
        s_enc_reset = NULL;
    }
    if (s_enc_bytes) {
        rmt_del_encoder(s_enc_bytes);
        s_enc_bytes = NULL;
    }
    if (s_chan) {
        rmt_del_channel(s_chan);
        s_chan = NULL;
    }
}

static bool rmt_setup(void)
{
    if (s_chan) {
        return true;
    }
    const rmt_tx_channel_config_t tx_cfg = {
        .gpio_num = LED_GPIO,
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = LED_RES_HZ,
        .mem_block_symbols = 48,
        .trans_queue_depth = 2,
        .flags.init_level = 0,
    };
    if (rmt_new_tx_channel(&tx_cfg, &s_chan) != ESP_OK) {
        return false;
    }
    const rmt_copy_encoder_config_t reset_cfg = {};
    if (rmt_new_copy_encoder(&reset_cfg, &s_enc_reset) != ESP_OK) {
        rmt_teardown();
        return false;
    }
    const rmt_bytes_encoder_config_t bytes_cfg = {
        .bit0 = {.level0 = 1, .duration0 = 3, .level1 = 0, .duration1 = 9},  /* 0.3 + 0.9 us */
        .bit1 = {.level0 = 1, .duration0 = 9, .level1 = 0, .duration1 = 3},  /* 0.9 + 0.3 us */
        .flags.msb_first = 1,
    };
    if (rmt_new_bytes_encoder(&bytes_cfg, &s_enc_bytes) != ESP_OK) {
        rmt_teardown();
        return false;
    }
    if (rmt_enable(s_chan) != ESP_OK) {
        rmt_teardown();
        return false;
    }
    return true;
}

/* Give the pin back to the GPIO block and hold it high: the plain red LED on
 * the same pin lights up, while the WS2812 (already latched) ignores the
 * steady level. After RMT has owned the pin the GPIO matrix still points at
 * the peripheral, hence the explicit output signal selection. */
static void led_idle_high(void)
{
    rmt_teardown();
    const gpio_config_t io = {
        .pin_bit_mask = 1ULL << LED_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&io) == ESP_OK) {
        esp_rom_gpio_connect_out_signal(LED_GPIO, SIG_GPIO_OUT_IDX, false, false);
        gpio_set_level(LED_GPIO, 1);
    }
}

/* WS2812 sends G,R,B, most significant bit first. */
static void led_write(const uint8_t grb[3])
{
    if (!rmt_setup()) {
        led_idle_high();
        return;
    }
    rmt_transmit_config_t tx = {
        .loop_count = 0,
        .flags.eot_level = 0,   /* stay low: this is where the WS2812 latches */
    };
    const rmt_symbol_word_t reset = {
        .level0 = 0, .duration0 = RESET_TICKS,
        .level1 = 0, .duration1 = 1,
    };
    esp_err_t err = rmt_transmit(s_chan, s_enc_reset, &reset, sizeof(reset), &tx);
    if (err == ESP_OK) {
        err = rmt_tx_wait_all_done(s_chan, 20);
    }
    if (err == ESP_OK) {
        err = rmt_transmit(s_chan, s_enc_bytes, grb, 3, &tx);
    }
    if (err == ESP_OK) {
        err = rmt_tx_wait_all_done(s_chan, 20);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "RMT transmit failed: %s", esp_err_to_name(err));
    }
    vTaskDelay(pdMS_TO_TICKS(LATCH_MS));
    led_idle_high();
}

typedef enum { MODE_OFF, MODE_NO_LINK, MODE_OTA } led_mode_t;

static void led_task(void *arg)
{
    (void)arg;
    led_mode_t mode = MODE_OFF;
    bool phase_on = false;
    int64_t next_toggle_us = 0;
    int64_t last_write_us = 0;
    const uint8_t *painted = DARK;

    for (;;) {
        bool sta_connected = false, ap_active = false;
        int64_t now = esp_timer_get_time();
        wifi_mgr_get_link_state(&sta_connected, &ap_active);
        (void)ap_active;   /* both the AP and the reconnect wait show as "no link" */

        led_mode_t next = ota_in_progress() ? MODE_OTA
                        : !sta_connected      ? MODE_NO_LINK
                                              : MODE_OFF;
        if (next != mode) {
            mode = next;
            phase_on = false;
            next_toggle_us = 0;
        }

        const uint8_t *want = DARK;
        if (mode != MODE_OFF) {
            if (now >= next_toggle_us) {
                phase_on = !phase_on;
                next_toggle_us = now + (int64_t)(mode == MODE_OTA ? BLINK_FAST_MS : BLINK_SLOW_MS) * 1000;
            }
            if (phase_on) {
                want = mode == MODE_OTA ? WHITE : BLUE;
            }
        }

        if (want != painted || now - last_write_us >= REFRESH_US) {
            led_write(want);
            painted = want;
            last_write_us = now;
        }
        vTaskDelay(pdMS_TO_TICKS(LED_POLL_MS));
    }
}

void status_led_start(void)
{
    if (!rmt_setup()) {
        ESP_LOGW(TAG, "RMT init failed; leaving GPIO%d high", LED_GPIO);
        led_idle_high();
        return;
    }
    led_write(DARK);
    /* One visible flash proves the WS2812 is being addressed, even when the
     * board boots connected. */
    led_write(BLUE);
    vTaskDelay(pdMS_TO_TICKS(200));
    led_write(DARK);

    if (xTaskCreate(led_task, "status_led", LED_TASK_STACK, NULL, 2, NULL) != pdPASS) {
        ESP_LOGW(TAG, "task creation failed");
        led_write(DARK);
    }
}

#else /* octal PSRAM: GPIO48 is the PSRAM clock line, leave it alone. */

void status_led_start(void)
{
    ESP_LOGW(TAG, "status LED disabled: GPIO%d belongs to octal PSRAM on this target", LED_GPIO);
}

#endif
