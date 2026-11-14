# All .c files in src/ (including the generated web_page_gen.c) form the main component.
file(GLOB app_sources ${CMAKE_CURRENT_SOURCE_DIR}/*.c)

idf_component_register(SRCS ${app_sources}
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_gpio esp_driver_uart esp_driver_rmt esp_http_server esp_wifi
                                     esp_netif esp_event esp_timer nvs_flash lwip esp_app_format
                                     app_update esp_partition)
