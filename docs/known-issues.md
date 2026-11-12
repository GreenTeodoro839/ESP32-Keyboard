# 已知问题

## 大量短连接后偶发 TCP 连接超时（TIME_WAIT 四元组冲突）

**状态**：已修复（固件 1.2.0，sha `96cf83a00b960d22`）。默认硬件套件和 8 轮短连接压测均通过。

**现象**：`pytest tests` 跑几千个短连接后，偶发一个请求在 **TCP connect/响应阶段超时**（客户端 5–10 s 超时），
报 `client_timeout: cannot reach http://<device>`；紧接着的测试/请求又完全正常。命中位置随机，
`test_rapid_mouse_moves`、`test_rapid_sequential_key_requests`、`test_rapid_status_polls`、
`test_reconnecting_websockets`、clean-state 收尾都出现过。

**证据**（tcpdump，端口 80）：

- 客户端复用一个源端口（如 `39256`）向设备发新 SYN。
- 设备回的不是 SYN-ACK，而是**旧连接的 challenge ACK**（旧连接的 seq/ack）。
- 客户端回 RST；设备仍然是同一个 challenge ACK，连接建立不起来，直到客户端超时。
- 抓包同时显示 ping 正常（RTT 约 2 ms），所以不是 WiFi 链路或丢包。
- 按流统计：普通 HTTP 响应几乎都是客户端先发 FIN（服务端 passive close，没有 TIME_WAIT）；
  **只有 WebSocket（`101` 升级）流是设备先发 FIN**。

**根因**：

1. WebSocket 关闭握手完成后，esp_http_server 在 `httpd_req_cleanup()` 里立刻
   `httpd_sess_trigger_close()`。设备因此成为 active closer，进入 TIME_WAIT。
2. lwIP 的 TIME_WAIT 时长是 `2 * CONFIG_LWIP_TCP_MSL`，原来 `TCP_MSL=60000`，约 **120 s**。
3. 客户端收到 FIN 后已完全关闭，本地没有 TIME_WAIT，内核可以立即复用这个源端口。
4. 新 SYN 与设备 TIME_WAIT 中的旧四元组相同。lwIP 的 `tcp_timewait_input()`
   （`components/lwip/lwip/src/core/tcp_in.c`）在 TIME_WAIT 中：
   - 按 RFC 1337 **忽略 RST**；
   - 对 SYN 只回 ACK/RST，不会用新 SYN 建立连接。
5. 于是该四元组在设备的 TIME_WAIT 结束前一直不可用。WS 重连越频繁，命中概率越高。

**修复**：

1. **让客户端先关 TCP**（`src/ws.c`）：关闭握手后不立刻 close，而是把 fd 交给一个独立的
   reaper 任务；reaper 最多等 2 s，先 drain 掉客户端可能重复发送的 CLOSE 帧，
   再用 `recv()` 等它的 FIN。客户端 FIN 到达后，设备的 close 就是 passive close
   （`CLOSE_WAIT -> LAST_ACK`），不产生 TIME_WAIT。
   等待不在 HTTP 服务任务里，所以不会拖慢其他连接或按键释放；等待有上限，
   客户端不配合时仍会关闭。每个正在收尾的 WS 占一个额外 socket，
   `CONFIG_LWIP_MAX_SOCKETS` 从 32 提到 36，`max_open_sockets` 保持 28。
2. **缩短兜底 TIME_WAIT**（`sdkconfig.defaults`）：`CONFIG_LWIP_TCP_MSL=5000`，
   任何剩余的设备主动关闭最多占用四元组 10 s，而不是 120 s。

**回归测试**：`tests/test_websocket.py::test_close_lets_client_close_tcp_first`。
它完成 WS 关闭握手后先断言设备**没有**发 FIN，再由客户端发 FIN，最后确认设备才关闭。
旧固件会在第一步失败。

**验证结果**：

- 抓包：修复后 WS 流是 client FIN → server FIN；设备不再先发 FIN。
- `tests/test_websocket.py`：44 passed / 1 skipped。
- `tests/test_stuck_keys.py`：14 passed / 1 deselected（含 WS 关闭后立即释放按键、
  关闭一个 WS 不影响其他 owner 的用例）。
- `tests/test_ota.py`（默认）：12 passed / 6 deselected。
- `tests/test_robustness.py` 连续 8 轮通过；改用 reaper 任务后又跑 3 轮，每轮 7 passed，
  **没有一次 `client_timeout`**（修复前按原复现步骤通常 3–6 轮内命中一次）。
- 默认硬件套件：**785 passed / 3 skipped / 22 deselected**（0 failed）。

**排查时的干扰项**：如果同一网络里有浏览器/脚本开着设备网页（`http://<device>`），
它会占一个 `/ws` 连接，使 `ws_clients == 0` 和“最多 4 个 WebSocket”测试失败。
跑测试前请关掉这些页面；`tests/README.md` 已注明。
