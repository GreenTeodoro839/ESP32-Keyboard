#include <ctype.h>
#include <string.h>
#include "espkb.h"
#include "keymap.h"

static const keyname_t s_names[] = {
    {"enter", 40}, {"return", 40},
    {"esc", 41}, {"escape", 41},
    {"backspace", 42}, {"bksp", 42},
    {"tab", 43},
    {"space", 44}, {"spacebar", 44},
    {"minus", 45}, {"-", 45},
    {"equal", 46}, {"=", 46},
    {"lbracket", 47}, {"[", 47},
    {"rbracket", 48}, {"]", 48},
    {"backslash", 49}, {"\\", 49},
    {"semicolon", 51}, {";", 51},
    {"quote", 52}, {"'", 52},
    {"grave", 53}, {"`", 53},
    {"comma", 54}, {",", 54},
    {"period", 55}, {".", 55}, {"dot", 55},
    {"slash", 56}, {"/", 56},
    {"capslock", 57},
    {"f1", 58}, {"f2", 59}, {"f3", 60}, {"f4", 61}, {"f5", 62}, {"f6", 63},
    {"f7", 64}, {"f8", 65}, {"f9", 66}, {"f10", 67}, {"f11", 68}, {"f12", 69},
    {"f13", 104}, {"f14", 105}, {"f15", 106}, {"f16", 107}, {"f17", 108}, {"f18", 109},
    {"f19", 110}, {"f20", 111}, {"f21", 112}, {"f22", 113}, {"f23", 114}, {"f24", 115},
    {"printscreen", 70}, {"prtsc", 70},
    {"scrolllock", 71},
    {"pause", 72},
    {"insert", 73}, {"ins", 73},
    {"home", 74},
    {"pageup", 75}, {"pgup", 75},
    {"delete", 76}, {"del", 76},
    {"end", 77},
    {"pagedown", 78}, {"pgdn", 78},
    {"right", 79}, {"left", 80}, {"down", 81}, {"up", 82},
    {"numlock", 83},
    {"kp_divide", 84}, {"kp_multiply", 85}, {"kp_minus", 86}, {"kp_plus", 87}, {"kp_enter", 88},
    {"kp_1", 89}, {"kp_2", 90}, {"kp_3", 91}, {"kp_4", 92}, {"kp_5", 93},
    {"kp_6", 94}, {"kp_7", 95}, {"kp_8", 96}, {"kp_9", 97}, {"kp_0", 98},
    {"kp_dot", 99},
    {"menu", 101}, {"app", 101},
    {"ctrl", 224}, {"lctrl", 224}, {"control", 224},
    {"shift", 225}, {"lshift", 225},
    {"alt", 226}, {"lalt", 226}, {"option", 226},
    {"gui", 227}, {"lgui", 227}, {"win", 227}, {"meta", 227}, {"super", 227}, {"cmd", 227},
    {"rctrl", 228},
    {"rshift", 229},
    {"ralt", 230}, {"altgr", 230},
    {"rgui", 231}, {"rwin", 231},
};

static const char *const s_buttons[MOUSE_BUTTON_COUNT] = {"left", "right", "middle", "back", "forward"};

/* US layout: usage code (0 = unsupported) and shift flag for 0x00..0x7F. */
#define S 0x80
static const uint8_t s_ascii[128] = {
    [0x08] = 42,              /* \b backspace */
    [0x09] = 43,              /* \t tab */
    [0x0A] = 40,              /* \n enter */
    [0x0D] = 40,              /* \r enter */
    [0x1B] = 41,              /* esc */
    [' '] = 44,
    ['!'] = 30 | S, ['"'] = 52 | S, ['#'] = 32 | S, ['$'] = 33 | S, ['%'] = 34 | S,
    ['&'] = 36 | S, ['\''] = 52, ['('] = 38 | S, [')'] = 39 | S, ['*'] = 37 | S,
    ['+'] = 46 | S, [','] = 54, ['-'] = 45, ['.'] = 55, ['/'] = 56,
    ['0'] = 39, ['1'] = 30, ['2'] = 31, ['3'] = 32, ['4'] = 33,
    ['5'] = 34, ['6'] = 35, ['7'] = 36, ['8'] = 37, ['9'] = 38,
    [':'] = 51 | S, [';'] = 51, ['<'] = 54 | S, ['='] = 46, ['>'] = 55 | S, ['?'] = 56 | S,
    ['@'] = 31 | S,
    ['['] = 47, ['\\'] = 49, [']'] = 48, ['^'] = 35 | S, ['_'] = 45 | S, ['`'] = 53,
    ['{'] = 47 | S, ['|'] = 49 | S, ['}'] = 48 | S, ['~'] = 53 | S,
};
#undef S

static int parse_hex_usage(const char *s, size_t len)
{
    if (len < 3 || len > 4 || s[0] != '0' || (s[1] != 'x' && s[1] != 'X')) {
        return -1;
    }
    int v = 0;
    for (size_t i = 2; i < len; i++) {
        char c = (char)tolower((unsigned char)s[i]);
        if (c >= '0' && c <= '9') {
            v = v * 16 + (c - '0');
        } else if (c >= 'a' && c <= 'f') {
            v = v * 16 + (c - 'a' + 10);
        } else {
            return -1;
        }
    }
    return v;
}

int keymap_lookup(const char *name, size_t len)
{
    if (len == 0) {
        return -1;
    }
    if (len == 1) {
        char c = (char)tolower((unsigned char)name[0]);
        if (c >= 'a' && c <= 'z') {
            return 4 + (c - 'a');
        }
        if (c >= '1' && c <= '9') {
            return 30 + (c - '1');
        }
        if (c == '0') {
            return 39;
        }
    }
    int hex = parse_hex_usage(name, len);
    if (hex >= 0) {
        return hex;
    }
    for (size_t i = 0; i < sizeof(s_names) / sizeof(s_names[0]); i++) {
        if (strn_ieq(name, len, s_names[i].name)) {
            return s_names[i].usage;
        }
    }
    return -1;
}

bool keymap_char(uint8_t ch, uint8_t *usage, bool *shift)
{
    if (ch >= 'a' && ch <= 'z') {
        *usage = 4 + (ch - 'a');
        *shift = false;
        return true;
    }
    if (ch >= 'A' && ch <= 'Z') {
        *usage = 4 + (ch - 'A');
        *shift = true;
        return true;
    }
    if (ch >= 128 || s_ascii[ch] == 0) {
        return false;
    }
    *usage = s_ascii[ch] & 0x7F;
    *shift = (s_ascii[ch] & 0x80) != 0;
    return true;
}

bool keymap_is_letter(uint8_t ch)
{
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
}

int keymap_button(const char *name)
{
    for (int i = 0; i < MOUSE_BUTTON_COUNT; i++) {
        if (str_ieq(name, s_buttons[i])) {
            return i;
        }
    }
    return -1;
}

const char *keymap_button_name(int index)
{
    return (index >= 0 && index < MOUSE_BUTTON_COUNT) ? s_buttons[index] : "?";
}

const keyname_t *keymap_names(size_t *count)
{
    *count = sizeof(s_names) / sizeof(s_names[0]);
    return s_names;
}
