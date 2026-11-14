# ESPKB：WiFi 控制的 USB 键盘 + 鼠标

ESPKB 是一块 ESP32-S3 SuperMini（ESP32-S3FH4R2：4 MB Flash + 2 MB PSRAM）上的固件：它通过 WiFi 接收指令（HTTP + WebSocket），在 USB 口上表现为一个标准 USB 键盘 + 鼠标（相对移动和绝对定位，VID:PID `303a:4004`）。
适合给不方便插键盘鼠标的机器打字、按组合键、移动/点击鼠标，可以用网页操作，也可以由脚本或 AI agent 调用。

完整接口说明见 [`docs/API.md`](docs/API.md)，本文只讲怎么用。

## 接线

SuperMini 只有一个 USB-C 口，直连芯片的原生 USB：

| 口 | 接到哪里 | 用途 |
|---|---|---|
| USB-C（原生 USB，无串口芯片） | **被控电脑** | 键盘 + 鼠标（HID） |
| | 你的开发电脑 | 烧录、供电 |

烧录时接到开发电脑，日常使用时接到被控电脑。被控电脑上无需安装任何驱动。

板上没有 CH343 之类的 USB-UART 芯片，而且 USB Serial/JTAG 与 HID 共用 D+/D-，固件运行后这个口只能当键盘鼠标用。
串口日志和控制台在 UART0（GPIO43/44）上，需要外接 USB-UART 适配器才能用（这两个脚是否引出取决于具体板子）。

板载蓝色 WS2812 指示灯（GPIO48）显示网络状态：未连上 WiFi（配网热点或重连中）蓝色慢闪，连上后熄灭，
固件更新期间白色快闪。USB-C 旁边那颗红色电源指示灯与 WS2812 共用 GPIO48（并联），固件在数据帧之间把
引脚拉高让它常亮；那颗约 1 Hz 闪烁的蓝色小灯是充电指示，不受固件控制。

## 编译与烧录

需要 PlatformIO（ESP-IDF 6.1，由 PlatformIO 自动安装）：

```bash
pio run              # 编译；web/index.html 会被自动 gzip 打包进固件
pio run -t upload    # 通过 USB-C（USB Serial/JTAG）烧录
```

烧录前用 USB-C 线把设备接到开发电脑。固件运行后该口是 HID，esptool 的自动复位可能失效；
如果提示无法进入下载模式，按住 BOOT 再点一下 RST，然后重新执行 `pio run -t upload`。

需要固定烧录口时，新建 `platformio_local.ini`（已被 git 忽略）：

```ini
[env:esp32-s3-devkitc-1]
upload_port  = /dev/ttyACM0          ; 或 /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_...
monitor_port = /dev/ttyACM0
```

硬件参数按 ESP32-S3 SuperMini（S3FH4R2：4 MB QIO Flash + 2 MB Quad PSRAM）写在 `platformio.ini` 和
`sdkconfig.defaults` 里；换成 N16R8 等其它型号时，要同时改 Flash 大小、PSRAM 模式（Octal/Quad）和
`partitions.csv`。

串口烧录不会清除设备上的配置（WiFi、令牌等），以后的版本可以直接通过 WiFi 更新，见下面的[固件更新](#固件更新ota)。
不要用 `pio run -t erase`：它会擦除整片 Flash，包括配置。

### 从 1.0.0 升级

1.1.0 改了分区表（两个固件分区，用于 OTA），所以**第一次**必须用 USB 线烧录：`pio run -t upload`。
配置分区的位置没有变，WiFi 等设置会保留，烧录后设备照常联网。之后的更新就可以走 WiFi 了。
如果项目目录里有 1.0.0（或旧 N16R8 配置）生成的 `sdkconfig.esp32-s3-devkitc-1`，先把它删掉再编译
（它是自动生成的文件，会按 `sdkconfig.defaults` 重新生成），否则新增的配置项不生效，编译会报错提示这一点。
从 N16R8 换到 SuperMini 时同样要在删除后重新编译，否则 Flash/PSRAM 设置仍是旧板子的。

## 首次配网

设备还没有 WiFi 配置时（或连续 20 s 连不上路由器时），会开启配网热点 `ESPKB-XXXX`（密码 `espkb1234`），
连上后打开 `http://192.168.4.1/`，在“设置”里填写路由器 SSID 和密码并重启设备。

如果你把 UART0（GPIO43/44）接到了外部 USB-UART 适配器，也可以用串口控制台（115200 波特率）配置：

```bash
.venv/bin/python tools/serialcon.py cmd 'wifi "我的WiFi" 我的密码'   # 只保存，重启后生效
.venv/bin/python tools/serialcon.py cmd reboot
.venv/bin/python tools/serialcon.py cmd status                       # 查看拿到的 IP
```

控制台命令：`help`、`status`、`wifi`、`token`、`hostname`、`keydelay`、`reboot`、`factory-reset`。
运行中按住 BOOT 键 5 秒会恢复出厂设置。

连上路由器后，用 `http://espkb.local/`（mDNS）或设备 IP 访问。

## 日常使用

### 网页

浏览器打开 `http://espkb.local/`：点击“键鼠捕获”区域后，键盘和鼠标操作会转发到被控电脑；
也可以用屏幕按键、组合键、触控板，或在“发送文本”里粘贴文字发送。顶部“急停”会中止所有任务并松开所有键。

> 注意：网页的键鼠控制部分目前只在模拟设备（`web/dev/mock_server.py`）上测试过，尚未在真实的被控电脑上验证。
> 在真实设备上用浏览器（无头 Chrome）验证过的是：页面加载、状态显示、设置对话框和固件更新。

### curl

```bash
H=http://espkb.local; J='Content-Type: application/json'
curl -s $H/api/status                                              # 设备状态
curl -s -H "$J" -d '{"text":"echo hello\n"}' $H/api/type           # 打字（\n = 回车）
curl -s -H "$J" -d '{"key":"ctrl+alt+t"}'    $H/api/key            # 组合键
curl -s -H "$J" -d '{"move":[120,-40]}'      $H/api/mouse          # 鼠标相对移动
curl -s -H "$J" -d '{"move_to":[960,540],"screen":[1920,1080]}' $H/api/mouse   # 鼠标绝对定位（像素）
curl -s -H "$J" -d '{"click":"left"}'        $H/api/mouse          # 左键单击
curl -s -H "$J" -d '{"steps":[{"key":"gui+r"},{"delay":400},{"text":"notepad\n"}]}' $H/api/seq   # 多步
curl -s -X POST $H/api/release                                     # 急停
```

### 命令行工具 `tools/espkb.py`

单文件、只依赖 Python 标准库，可以复制到任何地方使用：

```bash
export ESPKB_HOST=espkb.local          # 或设备 IP
python3 tools/espkb.py status
python3 tools/espkb.py type 'echo hello' --enter
python3 tools/espkb.py key ctrl+alt+t
python3 tools/espkb.py move 120 -40 ; python3 tools/espkb.py click
python3 tools/espkb.py moveto 960 540 --screen 1920x1080   # 绝对定位；不带 --screen 时坐标为 0–32767
python3 tools/espkb.py seq '[{"key":"gui+r"},{"delay":400},{"text":"notepad\n"}]'
python3 tools/espkb.py release         # 急停
python3 tools/espkb.py --help          # 全部命令和 step 格式
```

### 固件更新（OTA）

编译出新固件（`pio run`）后不用再接串口，通过 WiFi 就能更新，**WiFi、令牌、主机名等配置都会保留**：

- **网页**：“设置” → “固件更新”，选择 `.pio/build/esp32-s3-devkitc-1/firmware.bin`，点“上传并更新”。
  页面显示进度，设备校验固件后自动重启，页面重新连上后报告结果。整个过程约半分钟。
- **命令行**：`python3 tools/espkb.py ota .pio/build/esp32-s3-devkitc-1/firmware.bin`（会等设备重启并确认它运行的是新固件；
  `espkb.py ota` 不带文件则显示当前固件信息）。
- **curl**：`curl -H 'Content-Type: application/octet-stream' --data-binary @.pio/build/esp32-s3-devkitc-1/firmware.bin $H/api/ota`

设备有两个固件分区，新固件写在没在运行的那个里，写完校验通过才切换，所以上传中断、文件损坏、传错文件都不会影响正在运行的固件。
新固件启动后如果崩溃，或者 2 分钟内连不上网络（也开不了配网热点），设备会自动回到更新前的固件。
更新期间不要断电。设置了访问令牌时，更新同样需要令牌。细节见 [`docs/API.md`](docs/API.md) §11。

### 给脚本 / AI agent 的要点

- `GET /api` 返回接口自描述（端点、step 格式），可用于自动发现用法。
- 动作接口默认**同步**：任务在被控电脑上执行完才返回 `{"ok":true,"job":..,"elapsed_ms":..}`；加 `?async=1` 立即返回。
- 出错时返回 `{"ok":false,"error":"<code>","message":".."}` 和对应 HTTP 状态码；校验是原子的，一个 step 非法则整个请求不执行。
- 文本按 **US 键盘布局**输入，只支持 ASCII；被控电脑需是美式布局、英文输入状态。
- 按住（`"action":"down"`）的键在控制端消失后会自动松开（HTTP 5 s，WebSocket 1 s 无消息或断开）。
- 任何时候 `POST /api/release` 都会立即中止所有任务并松开所有键和鼠标键。

## 安全

- **请设置访问令牌**：网页“设置”里的“访问令牌”，或 `espkb.py config set token=<令牌>`，或串口 `token <令牌>`。
  设置后所有 `/api/*` 和 `/ws` 都需要令牌（`Authorization: Bearer <令牌>`）。未设置时，同一网络里的任何人都能控制被控电脑。
- 只在可信的局域网中使用，**不要把设备暴露到互联网**（不要做端口转发）。HTTP 是明文的。

## 已知限制

- 只支持 US 键盘布局的 ASCII 文本，不能直接输入中文。
- 板载 PCB 天线，WiFi 距离和穿墙能力弱于带外置天线的开发板；USB-C 口焊在小板上，机械强度有限。
- 只有一个 USB-C 口，烧录和 HID 不能同时用；没有板载串口芯片，日志/串口控制台需要外接 USB-UART 适配器。
- 绝对定位（`move_to`）用的是第 3 个 USB 接口，BIOS 等只认 boot 协议的环境里不起作用；多显示器时对应哪块屏幕由被控系统决定。
  从 1.1.0 升级上来后 USB 设备描述变了（多了一个接口），被控电脑会把它当作新设备重新识别一次。
- 在大量 WebSocket 客户端频繁断开重连的压力下，偶尔会有新建的 HTTP/WebSocket 连接被立即断开（ESP-IDF HTTP 服务器的延迟关闭机制相关），客户端重试即可。
- 一次同步发送接近 64 KB 的文本（约 2 分钟）时，连接可能在等待结果期间被重置；长文本请分段发送。
- **尚未验证**：网页在真实设备上的表现；在真实被控电脑（Windows / macOS / Linux 桌面、BIOS）上的实际效果（例外：绝对定位在
  Linux X11 桌面 1920×1080 上实测过，像素级准确）；主机挂起后的远程唤醒；BOOT 键恢复出厂；通过配网热点完成配网。目前的验证都是用 libusb 假主机在 USB 报告层面做的。
- 设备不会处理客户端的 `Connection: close`：读响应时请按 `Content-Length`，不要等连接关闭（curl、Python requests 等常用客户端都没有问题）。

## 运行测试

```bash
python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt   # 首次
.venv/bin/python -m pytest tests/offline   # 不需要硬件
.venv/bin/python -m pytest tests           # 默认硬件测试（需要设备在线）
.venv/bin/python -m pytest tests -m ""     # 全部，包括 slow / wifi / reboot / serial / ota
.venv/bin/python -m pytest tests/test_ota.py -m ota   # 固件更新测试：真的刷写并重启设备（约 2 分钟），不需要 USB 测试台
```

设备地址来自环境变量 `ESPKB_HOST` 或 `tests/device.env`（`ESPKB_HOST=...`）；测试台参数在 `tests/testbed.env`（从 `tests/testbed.env.example` 复制后修改）。详见 [`tests/README.md`](tests/README.md)。
测试时设备的原生 USB 口要插在运行测试的电脑上，测试框架（`tools/hidhost.py`）通过 libusb 充当 USB 主机，逐个检查设备发出的报告。

### 测试用的 udev 隔离规则

硬件测试时设备的 HID 口插在运行测试的电脑上，设备发出的按键会直接打进这台电脑的桌面。
测试前必须先装隔离规则，让内核的 `usbhid` 驱动不接管 `303a:4004`：

```bash
sudo cp tests/udev/99-espkb-test-isolation.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules      # 然后重新插拔设备
```

装了这条规则的电脑上，ESPKB 不能当作键盘鼠标使用。测试完删除规则：

```bash
sudo rm /etc/udev/rules.d/99-espkb-test-isolation.rules
sudo udevadm control --reload-rules
```

没有这条规则时不要运行硬件测试（测试框架检测到 `usbhid` 已绑定时会拒绝运行）。
`wifi` 类测试还需要一个由 NetworkManager 管理、名为 `espkb-test` 的测试热点，见 [`tests/README.md`](tests/README.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
