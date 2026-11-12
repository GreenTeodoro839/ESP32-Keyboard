/* WebSocket endpoint /ws (API.md §5): one engine owner per connection,
 * per-connection outbox flushed from the HTTP server task, status pushes. */
#pragma once

#include "esp_http_server.h"

void ws_init(httpd_handle_t hd);
esp_err_t ws_register(httpd_handle_t hd);
/* Called by the server's close callback for every closed socket. Clears the
 * WebSocket state and returns true if the callback must NOT close the fd:
 * the fd is handed to the reaper task, which waits for the client's FIN first
 * (passive close, no device-side TIME_WAIT). Returns false for non-WebSocket
 * sockets and when the close must happen immediately. */
bool ws_on_close(int fd);
int ws_client_count(void);
