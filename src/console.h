/* Serial console on UART0 (API.md §7). Offers no command that emits HID events. */
#pragma once

/* Installs the UART0 driver (so logging goes through a buffered driver) and
 * starts the console task. */
void console_start(void);
