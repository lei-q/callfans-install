# callfans 桌面托盘应用技术方案

> 版本：v0.1（2026-09-16）
> 目标平台：Windows 10 及以上 / Ubuntu 20.04 及以上（含无桌面 headless 环境）

## 1. 需求与约束

| # | 需求 | 说明 |
|---|------|------|
| R1 | 托盘应用 | 常驻系统托盘，图标 + 菜单 + 交互窗口（设置、状态展示等） |
| R2 | 后台服务 | 独立于 UI 运行的常驻服务，承载实际业务逻辑，UI 崩溃/重启不影响服务 |
| R3 | Windows 10+ | 安装即用，支持开机自启 |
| R4 | Ubuntu 20.04+ 桌面 | 同上，兼容 GNOME（X11/Wayland）|
| R5 | Ubuntu 无桌面可安装运行 | 没有 GUI 环境时，服务可独立安装运行，并提供 CLI 管理手段 |
| R6 | 单一 Python 代码库 | 一套代码多平台构建，避免分叉维护 |

## 2. 总体架构

**核心决策：UI 与服务拆成两个进程**，通过本机 IPC 通信。这是满足 R2（服务独立于 UI）和 R5（headless 无 UI 运行）的前提。

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  callfans-ui（桌面才有）      │        │  callfans-service            │
│  ┌───────────────────────┐  │        │  ┌────────────────────────┐  │
│  │ PySide6 托盘 + 窗口    │  │        │  │ core    业务逻辑（纯py） │  │
│  │ QSystemTrayIcon       │  │        │  │ worker  任务调度/长连接   │  │
│  └───────────────────────┘  │        │  └────────────────────────┘  │
└──────────────┬──────────────┘        └──────────────┬───────────────┘
               │  HTTP(控制) + WebSocket(事件推送)      │
               │  仅绑定 127.0.0.1，随机端口 + token 鉴权 │
               └────────────────────►──────────────────┘
                                          ▲
               ┌──────────────────────────┘
┌──────────────┴──────────────┐
│  callfans CLI（headless 核心）│  status / start / stop / logs / config
└─────────────────────────────┘
```

三个入口，共享同一 IPC 协议：

| 入口 | 桌面环境 | headless | 职责 |
|------|---------|----------|------|
| `callfans-service` | ✅ | ✅ | 后台服务进程 |
| `callfans-ui` | ✅ | ❌（不安装） | 托盘 + 交互窗口 |
| `callfans`（CLI） | ✅ | ✅ | 安装/启停/日志/配置管理 |

关键点：

- **core 层零 GUI 依赖**：业务逻辑放在 `core/`，不 import 任何 Qt，headless 包由此自然成立。
- **服务与 UI 生命周期解耦**：UI 退出服务继续跑；服务被 supervisor 拉起，不依赖 UI 存活。
- **IPC 选本机 HTTP + WS** 而非 Unix socket / 命名管道：跨平台一致、可用 `curl` 直接调试（对 headless 运维很重要）、库支持成熟。安全靠"仅 127.0.0.1 + 随机端口 + token 文件"保证，见 §9。

## 3. 技术选型

### 3.1 语言与运行时

- **Python 3.11**（开发与 CI 基准）。
- **分发包自带解释器**（PyInstaller 打包），不依赖目标机 Python —— 规避 Ubuntu 20.04 系统 Python 仅 3.8 的问题（新版 PySide6 已不支持 3.8）。
- 仅当走"pip 源码安装"这条路时，Ubuntu 20.04 需通过 deadsnakes PPA 装 3.11，文档中说明即可，不作为主路径。

### 3.2 GUI 框架（对比后定论：PySide6）

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **PySide6 (Qt6)** | 官方维护、LGPL 免商用、QSystemTrayIcon 成熟、完整窗口控件、X11/Wayland 均走 StatusNotifier | 包体大（打包后 ~100-150MB） | ✅ 采用 |
| pystray + Pillow | 极轻，纯托盘 | 无窗口控件（还得配 Tkinter）、Wayland 下问题多、菜单能力弱 | 备选（若最终只要托盘不要窗口） |
| Tkinter | 标准库自带 | 无原生托盘、观感差 | 不采用 |
| wxPython | 中庸 | 打包坑多、社区弱于 Qt | 不采用 |

托盘在 Linux 上的可用性说明：Ubuntu 自带的 GNOME 已预装并启用 AppIndicator 扩展，Qt 的 StatusNotifier 托盘直接可用；非 Ubuntu 的原生 GNOME 需用户自行装扩展——文档中注明即可，不在本方案范围。

### 3.3 其余组件

| 组件 | 选型 | 理由 |
|------|------|------|
| IPC 服务框架 | FastAPI + Uvicorn（异步） | REST + WS 一把抓、类型化、curl 可调试 |
| CLI | Typer | 与 FastAPI 同作者，类型风格统一 |
| 配置 | TOML（`tomllib`） | 3.11 内置解析；人机均可读可改 |
| 标准路径 | platformdirs | Windows `%APPDATA%` / Linux `~/.config` 等自动适配 |
| 日志 | logging + RotatingFileHandler，JSON 行格式 | 便于排查与采集 |
| 打包 | PyInstaller（onedir） | 交叉需求简单、生态最熟 |
| 安装器 | Windows: Inno Setup；Ubuntu: .deb（dpkg-deb 构建） | 见 §7 |

## 4. 代码结构

```
callfans/
├── src/callfans/
│   ├── core/            # 业务逻辑，禁止 import Qt / 任何 GUI
│   │   ├── tasks.py     # 后台任务调度
│   │   └── state.py     # 运行状态模型
│   ├── service/
│   │   ├── app.py       # FastAPI 应用装配
│   │   ├── api.py       # REST/WS 端点（§5）
│   │   ├── auth.py      # token 生成与校验
│   │   └── runtime.py   # 端口/token 文件的落盘位置管理
│   ├── ui/              # PySide6，仅桌面包包含
│   │   ├── tray.py      # QSystemTrayIcon + 菜单
│   │   ├── main_window.py
│   │   └── client.py    # IPC 客户端（httpx + websockets）
│   ├── cli.py           # Typer 入口
│   └── paths.py         # platformdirs 封装
├── installer/
│   ├── gui.spec         # PyInstaller spec：UI + 服务
│   ├── service.spec     # PyInstaller spec：仅服务（excludes PySide6）
│   ├── inno/callfans.iss
│   └── deb/             # debian 控制文件、systemd unit、.desktop 模板
└── tests/
```

两个 PyInstaller spec 是 headless 支持的关键：`service.spec` 用 `excludes=['PySide6']` 排除 Qt，headless 包不含任何 GUI 动态库，体积约为 GUI 包的 1/3。

## 5. IPC 协议

- 服务启动时：绑定 `127.0.0.1:<随机端口>`，生成随机 token，写入运行时文件：
  - Windows: `%LOCALAPPDATA%\callfans\runtime.json`
  - Linux: `${XDG_RUNTIME_DIR:-/run/user/<uid>}/callfans/runtime.json`（0600）
  - 内容：`{"port": 53124, "token": "...", "pid": 1234}`
- 所有请求带 `Authorization: Bearer <token>`；token 不对返回 401。

端点（v1）：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/status` | 服务/任务状态（UI 图标状态、CLI status 共用） |
| POST | `/api/v1/tasks/{id}/start|stop` | 任务控制 |
| GET/PUT | `/api/v1/config` | 读取/修改配置（修改后热生效项即时应用） |
| GET | `/api/v1/logs/tail?n=200` | 拉取尾部日志 |
| WS | `/api/v1/events` | 事件推送：状态变更、任务进度、错误告警（UI 实时刷新、断线自动重连） |

UI 对服务的控制**只走 IPC**，不做本机特权操作（启停由 supervisor 负责，见 §6）。

## 6. 平台适配（服务生命周期管理）

统一抽象一个 `supervisor` 概念：`install / uninstall / start / stop / status`，各平台实现不同。

### 6.1 Windows（默认：托盘进程托管 + 可选真服务）

| 模式 | 实现 | 适用 |
|------|------|------|
| **A. 用户会话托管（默认）** | UI 进程以子进程方式拉起服务，监控 + 崩溃自动重启；UI 自身通过注册表 `HKCU\...\Run`（或启动文件夹快捷方式）开机自启 | 普通用户，实现最简单，推荐默认 |
| B. 任务计划程序 | `schtasks` 注册开机任务（不要求登录） | 需要开机即跑但不想做真服务 |
| C. Windows 服务 | pywin32 `win32serviceutil` + PyInstaller 隐藏参数打包 | 需无人值守服务器场景；打包较繁琐，作为后续可选迭代 |

首版交付 A（+B 文档说明），C 列入 backlog。

### 6.2 Ubuntu 桌面

- **服务 = systemd 用户服务**：`~/.config/systemd/user/callfans.service`，`systemctl --user enable --now callfans`，无需 root。
- **UI = 自启 desktop 文件**：`~/.config/autostart/callfans-ui.desktop`。
- UI 不负责拉服务，只连 IPC；启停通过 `systemctl --user` 执行。

### 6.3 Ubuntu 无桌面（headless）

两种安装形态，同一二进制：

| 形态 | 安装方式 | 服务方式 |
|------|---------|---------|
| **单用户** | `callfans service install`（CLI 内置命令，写入用户 unit） | systemd 用户服务 + `loginctl enable-linger <user>`，无需保持登录 |
| **系统级（.deb）** | `sudo apt install callfans-service` | 系统 unit，`systemctl enable --now callfans`，随开机启动 |

管理全靠 CLI：

```bash
callfans status            # 读 runtime.json → 调 IPC
callfans logs -f           # journalctl -u callfans -f 或文件日志 tail
callfans config set k v
callfans service install / uninstall / start / stop
```

> 注：.deb 拆为两个包：`callfans-service`（无 GUI，服务器用）与 `callfans`（依赖前者，加 GUI 二进制与 desktop 文件）。这样 headless 机器不会装上 100MB+ 的 Qt。

## 7. 构建与分发

### 7.1 构建基线（重要）

- Windows：在 Windows 10 上构建（向上兼容 10/11）。
- Linux：**在 Ubuntu 20.04 上构建**，保证 glibc ≥ 2.31 依赖，产物可在 20.04/22.04/24.04 运行。
- PyInstaller onedir（不是 onefile）：启动快、杀软误报率低、增量更新友好。

### 7.2 产物矩阵

| 产物 | 内容 | 分发 |
|------|------|------|
| `callfans-setup-x64.exe` | Inno Setup 安装包（UI+服务+Run 自启注册+卸载） | 官网/内部分发 |
| `callfans_<ver>_amd64.deb` | 服务包（systemd unit + service 二进制 + CLI） | apt 仓库 / 直接 dpkg |
| `callfans-gui_<ver>_amd64.deb` | 依赖服务包，加 UI 二进制 + desktop/autostart | 同上 |

### 7.3 CI（GitHub Actions）

- `ubuntu-20.04` runner：跑 lint + pytest（core/service 不依赖显示），构建 .deb。
- `ubuntu-20.04` + xvfb：UI 冒烟（QApplication 能起、托盘对象能建）。
- `windows-2022` runner：构建 exe 安装包。
- 产物统一上传，打 tag 发 Release。

## 8. 配置与日志

- 配置文件：Windows `%APPDATA%\callfans\config.toml`；Linux `/etc/callfans/config.toml`（系统级包）或 `~/.config/callfans/config.toml`。UI 与 CLI 均可读写，服务负责校验与热加载可热更项。
- 日志：`logs/callfans.log`（同根目录），10MB × 5 轮转；服务启动时记录 runtime.json 路径，方便定位。Linux 系统级包同时接 journald（stdout 转发即可）。

## 9. 安全

| 风险 | 措施 |
|------|------|
| 本机其他进程冒充控制端 | 仅绑定 127.0.0.1；token 文件 0600；每次安装/启动重新生成 |
| 低权限用户误装系统服务 | .deb 的系统级安装要求显式 sudo；用户级路径全程无 root |
| 供应链 | 依赖 lock（uv/pip-tools），CI 跑 `pip-audit` |
| Windows SmartScreen / 杁毒软件误报 | 代码签名（预算允许时接入 signtool），未签名前文档说明放行方式 |

不做（明确出范围）：远程管理、对外网暴露 IPC。若未来需要远程，另立方案走反向连接 + TLS，而非开放本机端口。

## 10. 升级策略（首版从简）

- 检测更新：服务定期 GET 版本接口，有新版通过托盘气泡/CLI 提示。
- 执行更新：下载安装包由用户确认后运行（Inno Setup / dpkg 覆盖安装），服务在升级前由安装脚本 `systemctl stop` / 任务计划停止。
- 自动静默更新、差量更新列入 backlog，不进首版。

## 11. 测试

- `core/`、`service/`：常规 pytest，无 GUI 依赖，CI 必跑。
- IPC 契约测试：起真实服务进程，断言端点行为与 token 鉴权。
- 平台冒烟：Win10 / Ubuntu 20.04（GNOME）虚拟机手工 checklist：安装 → 自启 → 托盘可见 → 重启 UI 服务不断 → headless 安装后 `callfans status` 正常。

## 12. 里程碑

| 阶段 | 内容 | 出口标准 |
|------|------|---------|
| M1 | core + service + CLI，headless 跑通（.deb + 用户 unit） | 无桌面 Ubuntu 上安装后 systemctl 起服务，CLI 全命令可用 |
| M2 | PySide6 托盘 + 设置窗口 + WS 实时状态 | Ubuntu/Windows 桌面托盘交互完整 |
| M3 | 安装器（Inno + deb gui 包）+ 自启注册 + CI 产物 | 两平台"装完即用"，卸载干净 |
| M4 | 升级提示、签名、Windows 服务模式（可选） | 按需排期 |

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 非 Ubuntu GNOME 无托盘扩展 | 图标不显示 | 启动时检测并引导装 AppIndicator 扩展；CLI 仍可管理 |
| PySide6 包体大 | 下载/磁盘开销 | onedir + 服务包不含 Qt；可接受即不优化 |
| pywin32 真服务打包坑多 | 拖慢交付 | 首版只做用户会话托管模式 |
| Ubuntu 20.04 系统 Python 3.8 | 源码安装受阻 | 主路径分发自带解释器的二进制；文档注明 deadsnakes |
| 杀软/SmartScreen 误报 | 用户装不上 | onedir + 签名（M4）+ 文档放行指引 |
