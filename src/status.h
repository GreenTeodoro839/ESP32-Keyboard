/* Builds the device status object of API.md §4.1 (without "ok"). */
#pragma once

#include <stdbool.h>
#include "cJSON.h"

/* with_ok adds "ok":true as the first member (HTTP response form). */
cJSON *status_build(bool with_ok);
