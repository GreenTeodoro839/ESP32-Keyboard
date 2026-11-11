# ESPKB 接口规范（HTTP + WebSocket）

ESPKB 是一个 ESP32-S3 固件：通过 WiFi 接收指令，在原生 USB 口上表现为一个标准 USB 键盘 + 相对鼠标。
本文是固件、网页、命令行工具和测试共同遵守的契约。接口版本：`1`。

- 地址：`http://<hostname>.local/`（mDNS，默认 `espkb.local`）或设备 IP，端口 80。
- 给脚本 / AI agent 的最短用法：

```bash
H=http://espkb.local; J='Content-Type: application/json'
curl -s $H/api/status                                         # 设备状态
curl -s -H "$J" -d '{"text":"echo hello\n"}'    $H/api/type    # 打字（\n = 回车）
curl -s -H "$J" -d '{"key":"ctrl+alt+t"}'       $H/api/key     # 组合键
curl -s -H "$J" -d '{"move":[120,-40]}'         $H/api/mouse   # 相对移动
curl -s -H "$J" -d '{"click":"left"}'           $H/api/mouse   # 点击
curl -s -H "$J" -d '{"steps":[{"key":"gui+r"},{"delay":400},{"text":"notepad\n"}]}' $H/api/seq
curl -s -X POST $H/api/release                                # 急停：中止队列并松开所有键
```

## 1. 通用约定

- 请求体为 JSON（`Content-Type: application/json`），上限 **65536 字节**。响应一律为 JSON（UTF-8）。
- 成功：HTTP 200，`{"ok":true, ...}`。
- 失败：`{"ok":false,"error":"<code>","message":"<给人看的说明>"}`，可带附加字段（如 `index`）。

| HTTP | `error` | 含义 |
|---|---|---|
| 400 | `bad_request` | JSON 解析失败、缺字段、字段类型或取值范围错误 |
| 400 | `bad_step` | 某个 step 非法（未知键名、不支持的字符、未知按钮……）。附 `index`（step 下标，从 0 起）；不支持的字符另附 `offset`（该字符在 text 中的字节偏移） |
| 401 | `unauthorized` | 需要 token 而未提供或不正确 |
| 403 | `forbidden` | 来源检查不通过（见 §2） |
| 404 | `not_found` | 路径不存在 |
| 405 | `method_not_allowed` | 方法不对 |
| 409 | `aborted` | 任务被 `/api/release` 中止 |
| 413 | `too_large` | 请求体或 step 数量超限 |
| 503 | `busy` | 任务队列已满 |
| 503 | `usb_not_ready` | USB 未被主机枚举（没插、主机关机） |
| 503 | `usb_suspended` | 主机已挂起且未允许远程唤醒 |
| 504 | `timeout` | 主机在 1000 ms 内没有取走某个报告 |

**校验是原子的**：一个请求里任何 step 非法，整个请求以 400 拒绝，一个键都不会发出。

## 2. 鉴权与来源检查

- 配置了 token（见 §7）时，所有 `/api/*` 和 `/ws` 都要求 token，三种携带方式任选：
  `Authorization: Bearer <token>` 头、`X-Token: <token>` 头、URL 查询参数 `?token=<token>`（WebSocket 只能用这种）。
- 未配置 token 时接口开放。`GET /`（网页本身）永远不需要 token。
- **来源检查（始终生效）**：请求带 `Origin` 头且其 host[:port] 与 `Host` 头不一致时，返回 403 `forbidden`。
  这样恶意网页无法跨站驱动设备；curl、脚本不带 `Origin`，不受影响。设备不返回任何 CORS 头。
- 未配置 token 时，额外要求 `Host` 头是设备自己的地址：当前 STA IP、AP IP `192.168.4.1`、`<hostname>.local` 或 `<hostname>`（可带 `:80`），否则 403。
- `GET` 端点只读，所有会产生动作或修改配置的端点都是 `POST`。

## 3. Step：动作的统一表示

HTTP 和 WebSocket 共用同一套 step。每个 step 是一个 JSON 对象，由它包含的"动词"字段决定类型（**有且只有一个**动词）：

| step | 作用 | 可选字段 |
|---|---|---|
| `{"text":"ls -l\n"}` | 逐字符敲出文本（见 §3.1） | `delay_ms` |
| `{"key":"ctrl+alt+del"}` | 敲一次键或组合键：按顺序逐个按下，保持 `hold_ms`，再逆序松开 | `action`、`hold_ms`、`repeat`(1–1000，默认 1)、`delay_ms`、`ttl_ms` |
| `{"key":"shift","action":"down"}` | 按住（`"down"`）/ 松开（`"up"`）；`action` 缺省为 `"tap"` | `ttl_ms` |
| `{"move":[dx,dy]}` | 鼠标相对移动，dx 向右为正、dy 向下为正，各自范围 ±32767；自动拆成多个报告并走直线 | `step`(每个报告的最大位移 1–127，默认 127)、`delay_ms`(报告间隔，默认 0) |
| `{"click":"left"}` | 点击 | `count`(1–100，默认 1)、`hold_ms`(默认 20)、`interval_ms`(连击间隔，默认 60) |
| `{"button":"left","action":"down"}` | 按住 / 松开鼠标键（`action` 必填：`down`/`up`） | `ttl_ms` |
| `{"scroll":3}` 或 `{"scroll":[v,h]}` | 滚轮：v>0 向上滚、v<0 向下滚；h>0 向右。各自范围 ±32767 | — |
| `{"delay":250}` | 等待（毫秒，0–60000） | — |
| `{"release":true}` | 松开所有键和鼠标键（作为队列中的一步；急停请用 `/api/release`） | — |

- `key` 的取值：键名字符串（§8，大小写不敏感）、用 `+` 连接的组合（`"ctrl+shift+esc"`），或 HID 键盘用法码整数（1–255，如 `40` 表示回车）/ 字符串 `"0x28"`。
- 鼠标键名：`left`、`right`、`middle`、`back`、`forward`。
- `delay_ms`：本 step 内相邻两个键盘报告之间的间隔，缺省用配置项 `key_delay_ms`（默认 5）。BIOS、慢速远程桌面等场景可调大。
- 同时按住的非修饰键最多 6 个，超过则该请求以 `bad_step` 拒绝（按下第 7 个键的那个 step）。
- 未知字段被忽略；字段类型错误、动词数量不是 1 → `bad_step`。

### 3.1 文本

- 按 **US 键盘布局**把字符翻译成按键，目标机须为 US 布局且处于英文输入状态。
- 支持：可打印 ASCII（0x20–0x7E）、`\n`（回车；`\r\n` 只算一次回车，单独的 `\r` 也算回车）、`\t`（Tab）、`\b`（0x08，退格）、0x1B（Esc）。
  其它字符（含任何非 ASCII / 中文）→ 整个请求 `bad_step`，不会敲出任何内容。
- 每个字符 = 一个"按下"报告 + 一个"松开"报告。
- 如果主机报告 CapsLock 灯亮，固件自动对字母取反 Shift，保证敲出的大小写与文本一致。

### 3.2 执行模型

- 一个 HTTP 请求或一条 WebSocket 消息 = 一个**任务**（一组 step）。所有来源的任务进入**同一个 FIFO 队列**，严格按到达顺序逐个执行，任务内的 step 顺序执行。队列容量 32 个任务，满则 `busy`。
- 每个 HID 报告都等主机取走后才发下一个，不会丢、不会合并（唯一例外：队列中相邻且尚未执行的纯 `move` 任务可以合并位移）。
- 任务开始时 USB 未就绪 → `usb_not_ready`，不执行任何 step。执行中出错（USB 断开、`timeout`）→ 任务失败，同时内部按键状态清零。
- 主机处于挂起状态时：若主机允许远程唤醒，固件先发远程唤醒并等待恢复（最多 3 s）再执行；否则 `usb_suspended`。

## 4. HTTP 端点

所有动作端点默认**同步**：任务执行完才返回，返回 `{"ok":true,"job":<id>,"elapsed_ms":<n>}`。
加查询参数 `?async=1` 则入队后立即返回 `{"ok":true,"job":<id>,"queued":true}`。
长任务执行期间，其它请求（尤其 `/api/status`、`/api/release`）必须照常得到及时响应。

| 方法 路径 | 请求体 | 说明 |
|---|---|---|
| `POST /api/type` | `{"text":"...","delay_ms":5}`，或 `Content-Type: text/plain` 的原始文本 | 打字 |
| `POST /api/key` | 一个 `key` step | 敲键 / 按住 / 松开 |
| `POST /api/mouse` | 一个 `move` / `click` / `button` / `scroll` step | 鼠标 |
| `POST /api/seq` | `{"steps":[step, ...]}` 或直接一个 step 数组；最多 512 个 step | 一次提交多步，按序执行 |
| `POST /api/release` | 无 | **急停**：中止正在执行的任务，清空队列（这些任务以 `aborted` 结束），松开所有键和鼠标键。返回 `{"ok":true,"aborted":<被中止的任务数>}`。不排队，立即生效 |
| `POST /api/wake` | 无 | 主机挂起时发 USB 远程唤醒。主机未挂起也返回 ok |
| `GET /api/status` | — | 见 §4.1 |
| `GET /api/keys` | — | `{"ok":true,"keys":{"enter":40,...},"buttons":["left","right","middle","back","forward"]}` |
| `GET /api` | — | 自描述：`{"ok":true,"name":"espkb","api":1,"version":"...","endpoints":[{"method":"POST","path":"/api/type","desc":"..."}...],"steps":[...每种 step 的一行说明]}`，供 agent 自行发现用法 |
| `GET /api/config` | — | 见 §7（密码和 token 不回显） |
| `POST /api/config` | 要修改的配置项 | 部分更新。返回 `{"ok":true,"reboot_required":<bool>}` |
| `GET /api/wifi/scan` | — | `{"ok":true,"aps":[{"ssid":"..","rssi":-50,"channel":6,"secure":true}]}`，可能耗时 3 s 左右 |
| `POST /api/reboot` | 无 | 返回 ok 后约 500 ms 重启 |
| `GET /` | — | 网页（gzip），无需 token |

`/api/type`、`/api/key`、`/api/mouse` 都只是"单 step 任务"的入口；`/api/type` 的 text/plain 形式等价于 `{"text":<body>}`。

### 4.1 `GET /api/status`

```json
{
  "ok": true,
  "device": {"name":"espkb","api":1,"version":"1.0.0","uptime_ms":123456,
             "heap_free":250000,"heap_min":230000,"reset_reason":"poweron"},
  "usb":  {"mounted":true,"suspended":false,"remote_wakeup":false,
           "leds":{"num":true,"caps":false,"scroll":false},
           "kbd_protocol":"report","mouse_protocol":"report"},
  "hid":  {"busy":false,"queued":0,"keys":[225,4],"buttons":[],
           "reports_sent":1024,"jobs_done":88,"jobs_failed":0},
  "wifi": {"sta_connected":true,"ssid":"MyWiFi","ip":"192.168.1.50","rssi":-48,
           "ap_active":false,"ap_ssid":"ESPKB-5D8C","ap_ip":"192.168.4.1","hostname":"espkb"},
  "ws_clients": 1,
  "auth_required": false
}
```

- `hid.keys`：当前用 `down` 按住的键（HID 用法码，含修饰键 224–231）；`hid.buttons`：用 `down` 按住的鼠标键名。
  敲键、打字、点击过程中的瞬时按下不计入。
- `hid.busy`：有任务正在执行；`hid.queued`：排队中的任务数。
- `usb.*_protocol`：`"report"` 或 `"boot"`。

## 5. WebSocket：`/ws`

实时通道，网页用它，agent 也可以用。只用**文本帧**，每帧一个 JSON，单帧上限 8192 字节，最多 4 个连接。
鉴权用 `?token=`；来源检查同 §2。

**客户端 → 设备**

| 消息 | 说明 |
|---|---|
| 一个 step 对象，如 `{"key":4,"action":"down"}`、`{"move":[3,-2]}` | 作为一个任务入队 |
| `{"steps":[...]}` 或 step 数组 | 多步任务 |
| 以上任意对象形式加 `"id":<数字或字符串>` | 任务结束后设备回 `{"ack":<id>,"ok":true}` 或 `{"ack":<id>,"ok":false,"error":"..","message":".."}` |
| `{"ping":<任意值>}` | 设备回 `{"pong":<原值>,"t":<uptime_ms>}`；同时算作保活 |
| `{"release":true}` | 立即急停，等同 `POST /api/release`（不排队） |

**设备 → 客户端**

| 消息 | 时机 |
|---|---|
| `{"status":{...}}` | 内容同 §4.1（去掉 `ok`）。连接建立时、状态变化时（USB 挂载/挂起、LED、WiFi、按住的键变化）立即推送，另外每 2 s 推送一次 |
| `{"ack":...}` / `{"pong":...}` | 见上 |
| `{"error":"<code>","message":".."}` | 消息非法或任务失败而该消息没带 `id` 时。同类错误 1 s 内最多报一次 |

USB 未就绪时，WS 发来的任务直接丢弃并报 `usb_not_ready`，不排队。

## 6. 防卡键机制

按键"按下"后如果控制端消失，目标机会无限连发。设备用三层机制保证不会卡键：

1. **所有者**：每个被按住的键/鼠标键都记着是谁按的——某条 WS 连接，或"HTTP"（所有 HTTP 请求算同一个所有者）。
2. **保活超时**：
   - WS 连接按住的键：该连接 **1000 ms** 内没有发来任何消息（step 或 ping）→ 松开它按住的全部键。客户端按住键期间应每 250 ms 发一次 ping。
   - HTTP 按住的键：**5000 ms** 内没有新的 HTTP 动作请求 → 全部松开。可在 `down` 的 step 上用 `ttl_ms`（1–600000）指定更长或更短的期限；之后每个 HTTP 动作请求都把期限重置为"现在 + 该值"。
3. **连接断开**：WS 连接关闭时，它按住的键立即松开。

此外 `tap`、`click`、`text` 自身保证成对按下/松开；任务失败、被中止、USB 重新枚举时内部状态清零并尽力发出全松开报告。

## 7. 配置

存于 NVS。`GET /api/config` 返回：

```json
{"ok":true,"hostname":"espkb","wifi_ssid":"MyWiFi","wifi_password_set":true,
 "ap_ssid":"ESPKB-5D8C","ap_password_set":true,"token_set":false,"key_delay_ms":5}
```

`POST /api/config` 可写字段：

| 字段 | 约束 | 默认 | 生效 |
|---|---|---|---|
| `hostname` | 1–32 位 `[a-z0-9-]`，不以 `-` 开头结尾 | `espkb` | 重启后 |
| `wifi_ssid` | 0–32 字节；空串 = 不连路由器 | 空 | 重启后 |
| `wifi_password` | 0 或 8–63 字节 | 空 | 重启后 |
| `ap_password` | 8–63 字节 | `espkb1234` | 重启后 |
| `token` | 0–64 位可打印 ASCII（无空格）；空串 = 关闭鉴权 | 空 | 立即 |
| `key_delay_ms` | 0–1000 | 5 | 立即 |

取值非法 → 400 `bad_request`，任何字段都不保存。

**WiFi 行为**：有 `wifi_ssid` 时作为 STA 连接并无限重连。未配置，或连续 20 s 连不上时，同时开启配网热点
`ESPKB-XXXX`（XXXX = MAC 后两字节，WPA2，密码 `ap_password`，设备地址 `192.168.4.1`）；STA 连上且热点无客户端后热点关闭。

**串口控制台**（调试口 UART0，115200）：`help`、`status`、`wifi <ssid> [password]`、`token [<value>|-]`（`-` 清除）、
`hostname <name>`、`keydelay <ms>`、`reboot`、`factory-reset`。控制台不提供任何会发出 HID 事件的命令。

**恢复出厂**：运行中按住 BOOT 键 5 s → 清空配置并重启。

## 8. 键名

大小写不敏感。括号内为别名。数字是 HID 键盘用法码。

- 字母 `a`–`z`（4–29），数字 `1`–`9`、`0`（30–39）
- `enter`(`return`) 40、`esc`(`escape`) 41、`backspace`(`bksp`) 42、`tab` 43、`space`(`spacebar`) 44
- `minus`(`-`) 45、`equal`(`=`) 46、`lbracket`(`[`) 47、`rbracket`(`]`) 48、`backslash`(`\`) 49、
  `semicolon`(`;`) 51、`quote`(`'`) 52、`grave`(`` ` ``) 53、`comma`(`,`) 54、`period`(`.`、`dot`) 55、`slash`(`/`) 56
- `capslock` 57、`f1`–`f12` 58–69、`f13`–`f24` 104–115
- `printscreen`(`prtsc`) 70、`scrolllock` 71、`pause` 72、`insert`(`ins`) 73、`home` 74、`pageup`(`pgup`) 75、
  `delete`(`del`) 76、`end` 77、`pagedown`(`pgdn`) 78、`right` 79、`left` 80、`down` 81、`up` 82
- `numlock` 83、`kp_divide` 84、`kp_multiply` 85、`kp_minus` 86、`kp_plus` 87、`kp_enter` 88、
  `kp_1`–`kp_9` 89–97、`kp_0` 98、`kp_dot` 99
- `menu`(`app`) 101
- 修饰键：`ctrl`(`lctrl`、`control`) 224、`shift`(`lshift`) 225、`alt`(`lalt`、`option`) 226、
  `gui`(`lgui`、`win`、`meta`、`super`、`cmd`) 227、`rctrl` 228、`rshift` 229、`ralt`(`altgr`) 230、`rgui`(`rwin`) 231

组合键里 `+` 是分隔符，所以没有名为 `+` 的键：要敲加号用 `{"text":"+"}` 或 `"shift+="`。

## 9. USB 设备特征

| 项 | 值 |
|---|---|
| VID:PID | `303a:4004`，全速，总线供电，支持远程唤醒 |
| 接口 0 | HID boot 键盘（class 3 / subclass 1 / protocol 1），中断 IN 端点 `0x81`，轮询间隔 1 ms |
| 接口 1 | HID boot 鼠标（class 3 / subclass 1 / protocol 2），中断 IN 端点 `0x82`，轮询间隔 1 ms |

- 键盘输入报告（8 字节，无 Report ID）：`[修饰键位图, 0, key1..key6]`。修饰键位图 bit0–7 = 用法码 224–231。
- 键盘输出报告（主机经 SET_REPORT 下发，1 字节）：bit0 NumLock、bit1 CapsLock、bit2 ScrollLock。
- 鼠标输入报告，report 协议（5 字节，无 Report ID）：`[按钮位图, dx, dy, wheel, pan]`，后四个为 int8。
  按钮位图 bit0–4 = left、right、middle、back、forward。boot 协议下只发前 3 字节。

## 10. 细则与边界行为

正文没有写到的细节，以本节为准。

**错误归类**
- step 内部的问题（字段类型、取值范围、未知键名、不支持的字符、动词数量不为 1）一律是 `bad_step`，带 `index`。
- 请求层面的问题是 `bad_request`：JSON 解析失败或尾部有多余内容、缺少必需字段（`text` / `key` / 鼠标动词 / `steps`）、请求体类型不对。给 `/api/seq` 传单个 step 对象（不是数组也没有 `steps`）也是 `bad_request`。
- 空的 `steps` 数组合法，是一个不产生输出的任务（可当作"等前面的任务都执行完"的栅栏）。
- 6 键上限在提交时按当时已按住的键检查；若执行时仍然超限，任务以 `bad_step` 失败并清空按键状态。
- 请求体恰好 65536 字节可以接受，超过才是 `too_large`；此时设备回复后关闭该连接。
- 404、405 等错误同样返回 JSON。失败的任务响应里带 `job`，执行期出错时带出错 step 的 `index`。

**step 取值范围与缺省值**
- `delay_ms` 0–1000；`hold_ms`、`interval_ms` 0–60000。敲键的 `hold_ms` 缺省等于生效的 `delay_ms`。
- 鼠标 `step` 是每个轴单个报告的最大位移。位移为 0 的 `move` / `scroll` 可以不产生报告。
- 文本使用左 Shift。数值用法码 224–231 进入修饰键位图而不是按键数组。
- 敲一个已经被 `down` 按住的键，结束后该键保持按住。已按住的修饰键对随后的文本同样生效。
- `release`（接口和 step）总是在两个接口上各发一个全松开报告，即使当时没有键被按住。
- boot 协议下鼠标没有滚轮字节，`scroll` 被跳过（任务仍然成功）。
- `elapsed_ms` 从入队算起。队列里的 `move` 任务合并是可选优化，目前未实现。

**防卡键补充**
- 某个所有者自己有任务在排队或执行时，它的保活计时暂停，任务结束后重新计时（否则按住 Shift 再打一段长文本，Shift 会在中途被松开）。
- `ttl_ms` 只对 HTTP 的 `down` 生效（键和鼠标键都适用），对 WebSocket 连接无效。HTTP 不再按住任何键后，期限恢复为 5000 ms。
- 能重置 HTTP 期限的是动作请求（`POST /api/type|key|mouse|seq`），`GET` 不算。
- 已关闭的 WebSocket 连接留在队列里的 `down` 会被跳过。

**WebSocket 补充**
- 任何帧都算保活。`ping` 在收到时立即应答，不排队，长任务执行期间也一样。
- 顶层 `{"release":true}`（不在 `steps` 里）是急停；带 `id` 时 ack 里附 `aborted` 数量。放在 `steps` 里的 `release` 是普通的排队步骤。
- 错误消息的限流按"每条连接、每种错误码"计，ack 不限流。
- 二进制帧、分片帧回 `bad_request`；超过 8192 字节的帧回错误后以 1009 关闭连接。
- 第 5 个连接在握手阶段被拒绝（HTTP 503）；token 错误是 401，来源检查不过是 403。浏览器拿不到握手失败的原因，可再请求一次 `GET /api/status` 来判断。
- `/ws` 也接受放在请求头里的 token（非浏览器客户端可用）。

**鉴权补充**
- `Host` 检查作用于 `/api/*` 和 `/ws`，不作用于 `GET /`。比较 `Origin` 与 `Host` 时忽略默认端口 `:80` / `:443`；`Origin: null` 视为不一致。
- USB 是否就绪在请求到达时就检查，未就绪立即返回 503，不入队。

**状态与配置补充**
- WiFi 未连接时 `wifi.ip` 为空串、`rssi` 为 0，`wifi.ssid` 是已配置的 SSID（空串表示尚未配网）。
- `usb.remote_wakeup` 要等主机挂起过一次才知道真实值。
- `ap_password_set` 为 true 表示保存了自定义热点密码；仍在用默认密码时为 false。
- `/api/wake` 在 USB 未枚举时返回 `usb_not_ready`；发出唤醒后立即返回，不等主机恢复。
- 串口控制台的 `wifi` 命令只保存配置，`reboot` 后生效；SSID 含空格时加引号。`token` 不带参数时打印当前 token。
- USB 字符串：厂商 `ESPKB`，产品 `ESPKB Keyboard Mouse`，序列号为 MAC。

**已知特性**
- 主机一段时间不取报告导致任务 `timeout` 后，留在端点里的那一个报告会在主机恢复读取时送达，紧跟着一个全松开报告。主机可能看到一次本应失败的按键，但不会卡键。
- 同步请求的客户端中途断开，任务仍会继续执行。同时等待结果的同步请求最多 20 个，超过返回 `busy`。
