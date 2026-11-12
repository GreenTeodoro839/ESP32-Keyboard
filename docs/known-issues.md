# 已知问题

## 大量短连接后偶发 TCP 连接超时（TIME_WAIT 四元组冲突）

**状态**：已定位，未修复。默认硬件测试套件约每轮命中 1 次。不影响 HID 数据，客户端重试即可恢复。

**现象**：`pytest tests` 跑几千个短连接后，偶发一个请求在 **TCP connect/响应阶段超时**（客户端 5–10 s 超时），
报 `client_timeout: cannot reach http://<device>`；紧接着的测试/请求又完全正常。命中位置随机，
`test_rapid_mouse_moves`、`test_rapid_sequential_key_requests`、`test_rapid_status_polls`、
`test_reconnecting_websockets`、clean-state 收尾都出现过。

**证据**（tcpdump，端口 80）：

- 客户端复用一个源端口（如 `39256`）向设备发新 SYN。
- 设备回的不是 SYN-ACK，而是**旧连接的 challenge ACK**（旧连接的 seq/ack）。
- 客户端回 RST；设备仍然是同一个 challenge ACK，连接建立不起来，直到客户端超时。
- 抓包同时显示 ping 正常（RTT 约 2 ms），所以不是 WiFi 链路或丢包。

**根因**：

1. 设备先发 FIN（HTTP 响应后），进入 TIME_WAIT。lwIP 的时长是 `2 * CONFIG_LWIP_TCP_MSL`，
   当前 `TCP_MSL=60000`，约 **120 s**。
2. 客户端收到 FIN 后已完全关闭，本地没有 TIME_WAIT，内核可以立即复用这个源端口。
3. 新 SYN 与设备 TIME_WAIT 中的旧四元组相同。lwIP 的 `tcp_timewait_input()`
   （`components/lwip/lwip/src/core/tcp_in.c`）在 TIME_WAIT 中：
   - 按 RFC 1337 **忽略 RST**；
   - 对 SYN 只回 ACK/RST，不会用新 SYN 建立连接。
4. 于是该四元组在设备的 TIME_WAIT 结束前一直不可用。

连接量越大、端口复用越频繁，命中概率越高；纯 HTTP 4000 次压测没复现，需要“HID 作业 + 大量 HTTP”
那种默认套件的连接密度。

**复现**：

```bash
ESPKB_HOST=<device-ip> .venv/bin/python -m pytest tests/test_ota.py -q
for i in $(seq 1 8); do
  ESPKB_HOST=<device-ip> .venv/bin/python -m pytest tests/test_robustness.py -q
done
```

前几轮正常，通常 3–6 轮内出现一次 `client_timeout`。抓包命令：

```bash
sudo tcpdump -i <iface> -w /tmp/espkb.pcap -s 0 'host <device-ip> and tcp port 80'
```

然后在 pcap 里找“客户端源端口相同的新 SYN 得到旧 seq/ack 的 ACK”的流。

**影响**：真实客户端（网页、脚本）偶尔会看到一次连接超时，重试即可；设备本身没有重启、没有丢配置。
测试套件表现为偶发红，误报风险高。

**候选修复**（按优先级）：

1. **缩短设备 TIME_WAIT**：把 `CONFIG_LWIP_TCP_MSL` 调小（例如 5000–10000 ms），
   碰撞窗口从 120 s 降到 10–20 s。改动最小，但只是缓解。
2. **查清 `Connection: close` 后 FIN 的时机**：README 已说明客户端应按 `Content-Length` 读、
   不要等连接关闭；抓包中设备有时会先发 FIN。需要确认 httpd/`http_sync` 的关闭路径，
   避免设备侧产生长 TIME_WAIT。
3. **让客户端先关闭**（keep-alive + 客户端主动 close），把 TIME_WAIT 留在客户端；
   对 `tools/espkb.py` 和测试 harness 都适用，但改动面比 1 大。
4. **测试侧重试**：对连接超时的请求重试一次。不建议作为首选——会掩盖固件问题，
   可作为修复前的临时措施。

**已排除**：绝对鼠标（第 3 个 HID 接口）、USB 报告、WiFi 信号（ping 全程正常）、
设备内存（heap 平稳）、设备重启（uptime 连续）。

**当时的测试结果**（固件 1.2.0，sha `c59feaf66823bdd6`）：

- 默认套件 787 项：783 passed / 3 skipped / 1 偶发失败（即本文问题）。
- `tests/test_abs_mouse.py` + `tests/test_descriptors.py`：55 passed。
- `slow / reboot / ota` 标记：11 passed。
- 离线：210 passed。
- 未跑：`wifi` / `serial`（测试热点不存在、串口适配器未连接）。
