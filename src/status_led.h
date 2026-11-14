/* Onboard status LED (WS2812 RGB on GPIO48, ESP32-S3 SuperMini).
 *
 *   no WiFi link (provisioning AP, or reconnecting)  blue slow blink
 *   connected                                        WS2812 off (red LED stays lit)
 *   firmware upload in progress (POST /api/ota)      white fast blink
 *
 * The red LED next to the USB-C connector shares GPIO48 with the WS2812 data
 * input; the pin is driven high between frames to keep it lit. Only blue and
 * white are used, so clones with RGB instead of GRB order look the same. */
#pragma once

/* Initialises the LED (dark) and starts the updater task. Safe to call once,
 * after wifi_mgr_start() and ota_init(). */
void status_led_start(void);
