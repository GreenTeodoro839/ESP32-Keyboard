/* Key names (API.md §8), US-layout character mapping (§3.1) and mouse button names. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HID_MOD_FIRST 224   /* usage codes 224..231 are the modifier bits 0..7 */
#define HID_MOD_LAST  231
#define HID_KEY_LSHIFT_BIT 0x02

#define MOUSE_BUTTON_COUNT 5

typedef struct {
    const char *name;
    uint8_t usage;
} keyname_t;

/* Looks up a key name (case-insensitive, not NUL-terminated). Returns the HID
 * usage code, or -1 if unknown. Accepts names, aliases, single characters and
 * "0x.." hex usage codes. */
int keymap_lookup(const char *name, size_t len);

/* Maps a text character (already excluding the \r\n pairing logic) to a usage
 * code and a shift flag. Returns false for unsupported characters. */
bool keymap_char(uint8_t ch, uint8_t *usage, bool *shift);

/* True for a..z and A..Z (CapsLock compensation applies only to letters). */
bool keymap_is_letter(uint8_t ch);

/* Mouse button name -> bit index 0..4, or -1. */
int keymap_button(const char *name);
const char *keymap_button_name(int index);

/* Table of named keys (excluding letters and digits, which are generated). */
const keyname_t *keymap_names(size_t *count);
