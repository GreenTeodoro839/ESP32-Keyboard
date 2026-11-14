/* Onboard status LED (WS2812 RGB on GPIO48, ESP32-S3 SuperMini).
 *
 *   no WiFi link (provisioning AP, or reconnecting)  blue slow blink
 *   connected                                        WS2812 off (red LED stays lit)
 *   firmware upload in progress (POST /api/ota)      white fast blink
 *
 * The red LED next to the USB-C connector shares GPIO48 with the WS2812 data
 * input; the pin is driven high between frames to keep it lit. Only blue and
 * white are used, so clones with RGB instead of GRB order look the same.
 *
 * Quiet mode (short BOOT press, persisted as led_quiet in the config): the
 * pin idles low instead, so the red LED and the boot flash are off and a
 * connected board shows no light at all. */
#pragma once

/* Initialises the LED (dark) and starts the updater task. Safe to call once,
 * after wifi_mgr_start() and ota_init(). */
void status_led_start(void);
