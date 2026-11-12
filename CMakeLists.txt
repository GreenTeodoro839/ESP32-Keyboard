cmake_minimum_required(VERSION 3.16.0)

# The firmware version lives in src/espkb.h; it is also stored in the image's
# application descriptor, which the OTA upload (web page, tools/espkb.py) shows.
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/src/espkb.h" espkb_ver_line REGEX "#define[ \t]+ESPKB_FW_VERSION[ \t]")
string(REGEX REPLACE ".*\"([^\"]+)\".*" "\\1" PROJECT_VER "${espkb_ver_line}")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espkb)
