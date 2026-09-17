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

## 状态

- [x] M1 core（harbor/compare/checker）+ service + CLI，检查链路（已对真实 Harbor 验证）
- [x] M2 三种更新执行器 + 回滚 + 更新历史 + POST /update
- [x] M3 托盘 UI（`callfans-ui`，需 `pip install ".[gui]"`；代码完成，待 Windows 真机验证）
- [ ] M4 打包安装器（Inno Setup / .deb）
