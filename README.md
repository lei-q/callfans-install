# callfans 镜像更新器

Harbor ↔ 本地版本对比与更新（server 容器 / 前端静态文件 / SQL 三类制品）。
设计文档：[docs/tech-design.md](docs/tech-design.md)、[docs/implementation-plan.md](docs/implementation-plan.md)。

## 快速开始

```bash
cp .env.example .env       # 填 Harbor 地址/账号、compose 路径等
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

callfans serve             # 启动后台服务（定时检查 + IPC API）
callfans check             # 立即检查一次（服务未跑则本地直连）
callfans pending           # 查看最近一次检查结果
callfans update            # 立即更新全部待更新项（SQL → server → 前端）
callfans status            # 服务状态
callfans-ui                # 托盘 + 窗口（需 gui extra，通过服务 IPC 操作）
```

## 开发

```bash
pytest                     # 单元测试（无需 Harbor/docker）
```

## 构建与安装（M4）

| 平台 | 命令（在装好 `.[dev,gui]` + `pyinstaller` 的环境执行） | 产物 |
|------|------|------|
| Linux（须 Ubuntu 20.04，glibc 2.31 基线） | `installer/build.sh` | `dist/callfans-service_*.deb`（无 GUI）+ `dist/callfans_*.deb`（GUI） |
| Windows | `installer/build.ps1`（Inno Setup 可选） | `dist/callfans-setup-x64.exe` |

- CI：`.github/workflows/release.yml`（Linux 在 ubuntu:20.04 容器内构建；打 `v*` tag 自动发 Release）
- **deb 系统级**：`callfans-service` 以 systemd 服务运行（root），配置 `/etc/callfans/.env`，runtime 共享文件 `/run/callfans/runtime.json`；桌面用户加入 `callfans` 组即可用 GUI 包的托盘
- **Linux 单用户（无 root）**：`callfans service install --env .env`（systemd 用户级 + UI 自启），headless 再执行 `loginctl enable-linger $USER`
- **Windows 模式 A**：安装器注册 HKCU Run 自启 `callfans-ui`，UI 检测到服务未运行时自动拉起（`ui/bootstrap.py`）

## 状态

- [x] M1 core（harbor/compare/checker）+ service + CLI，检查链路（已对真实 Harbor 验证）
- [x] M2 三种更新执行器 + 回滚 + 更新历史 + POST /update
- [x] M3 托盘 UI（`callfans-ui`，需 `pip install ".[gui]"`；代码完成，待 Windows 真机验证）
- [x] M4 打包（spec×2 + Inno + deb×2 + CI；spec 已本机构建验证，正式产物需 Linux/Windows 环境出）
