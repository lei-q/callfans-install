# callfans 镜像更新器 — 实施方案

> 版本 v0.3（2026-09-17）
> 上位文档：[tech-design.md](tech-design.md)（双进程架构、IPC、打包分发沿用该文档，本文只定义业务实现）
> 状态：**全部决策已定（§1.1），可进入开发**；§1.2 默认假设未推翻即生效

## 0. 范围界定

**做**：
- Harbor ↔ 本地对比，筛出领先版本（三种制品）
- 定时（6h）+ 手动检查，新版本提示
- 三种制品的"立即更新"执行器（docker 含回滚）
- changelog 展示、更新历史记录

**不做**（防蔓延，如需另行立项）：
- 自动执行更新（永远人工触发）
- 失败自动重试（只做一次回滚）
- 多 compose 文件 / 多环境；增量或断点续传下载
- Ubuntu 图形界面（当前需求 UI 仅 Windows；Ubuntu 走 headless 服务 + CLI）

## 1. 决策记录

### 1.1 已确认（2026-09-17）

| # | 决策 |
|---|------|
| Q1 | tag 形如 `20260912132921-123a066`（时间戳-commitId；**2026-09-17 放宽：commitId 可选**，兼容真实仓库的纯时间戳 tag 如 `20260910161528`）；**比较器用 Harbor push_time**；自动排除 `latest`、`dev`（做成 .env 可配 `TAG_EXCLUDE`）；不匹配该格式的 tag 跳过并告警；同 tag 但 digest 变化仍判为领先（保护性，防 CI 重推） |
| Q2 | 前端/SQL 本地版本由应用自维护 `state.json` 记账（详见 §7 与正文说明）；加固方案见 Q14 |
| Q3 | 类型标识 `com.callfans.type`，取值 **server / frontend / sql**：前端/SQL 在 manifest annotation；server（标准 docker 镜像）在 image Label（经 registry API 读 config，见 Q4）。无 annotation 的制品默认按 server 处理 |
| Q4 | docker Label 读取走 registry API（manifest → config blob，不下载镜像层） |
| Q5 | compose 用 `image: <repo>:${VAR}` 形式。**注入机制细化为：更新器把新 tag 持久写入 compose 同目录 .env 的对应变量**（不传临时进程环境变量），原因见 §6.1 |
| Q13 | `com.callfans.type` 的 Label/annotation 值定为 `server`（docker 镜像）、`frontend`、`sql` |
| Q14 | 版本记账采用**最小方案：仅 state.json**；不建 MySQL 记账表、不写前端版本标记文件（加固方案留档 §7.3，未采纳） |

### 1.2 默认假设（未推翻即生效）

| # | 问题 | 默认假设 |
|---|------|---------|
| Q6 | 立即更新是"一键全更"还是可勾选 | 一键全更 |
| Q7 | Ubuntu 端形态 | 服务跑定时检查，更新由 CLI `callfans update` 触发，无 UI |
| Q8 | Harbor 凭据 | robot 账号 basic auth；只管理 .env 配置 project 下的制品 |
| Q9 | ORAS 依赖形态 | oras-py（pip）；能力不足则降级 registry v2 API 直下 blob |
| Q10 | SQL 是否幂等、是否含 DELIMITER | 假设幂等、无 DELIMITER |
| Q11 | 执行顺序、单项失败是否继续 | SQL → server（docker 镜像）→ 前端；单项失败不阻断，汇总报告 |
| Q12 | alias 为空取 repo 名去掉 project 前缀 | 是 |

## 2. 架构落位（复用上位文档双进程架构）

```
┌─ callfans-ui（仅 Windows）─────────┐       ┌─ callfans-service（两平台）───────────────┐
│ 托盘 + 窗口                        │ IPC   │  scheduler   6h 定时 → checker            │
│ [检查更新] → POST /api/v1/check    │◄─────►│  checker     HarborClient + LocalState    │
│ [立即更新] → POST /api/v1/update   │ HTTP  │               + Comparator → UpdatePlan   │
│ 列表：old → new + changelog        │ + WS  │  updater     Sql/Docker/Frontend 三执行器 │
│ 托盘气泡（定时检查命中新版本）        │       │  history     update_history.jsonl        │
└───────────────────────────────────┘       │  CLI：pending / check / update / status    │
                                            └───────────────────────────────────────────┘
外部依赖：docker CLI（含 compose 插件）、Harbor API + registry v2 API、oras-py、pymysql、python-dotenv
```

- core 层零 GUI 依赖，headless 自然成立。
- docker 操作统一 CLI 子进程（`docker` / `docker compose`）：compose 只有 CLI 能力，统一通道避免 SDK 与 CLI 行为不一致；结构化输出用 `--format json`。
- 检查与更新互斥：更新进行中跳过定时检查；手动检查后重置 6h 计时。
- "立即更新"执行前先自动重新检查一次，以最新结果执行。

## 3. 配置（.env）

新增 python-dotenv 依赖。路径：应用工作目录下 `.env`（与 compose 自己的 env 文件互不干扰）。

```ini
# --- Harbor ---
HARBOR_API_URL=https://harbor.example.com     # 代码内拼 /api/v2.0（管理面）与 /v2（registry 面）
HARBOR_PROJECT=callfans
HARBOR_USERNAME=robot$callfans
HARBOR_PASSWORD=***

# --- 检查 ---
CHECK_INTERVAL_HOURS=6
TAG_EXCLUDE=latest,dev                        # 排除的 tag（Q1）

# --- docker 镜像更新 ---
COMPOSE_FILE=/opt/callfans/docker-compose.yml

# --- 前端静态文件 ---
FRONTEND_OUTPUT_DIR=/opt/callfans/webroot

# --- SQL ---
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=callfans
MYSQL_PASSWORD=***
MYSQL_DATABASE=callfans

# --- 更新执行 ---
HEALTH_WAIT_SECONDS=60      # 新容器运行成功观察窗口
DOCKER_STOP_TIMEOUT=15
```

## 4. 数据模型（检查输出 UpdatePlan）

```json
{
  "checked_at": "2026-09-17T10:00:00+08:00",
  "pending": [
    {"name": "callfans/api", "type": "server", "old": "20260901102030-abc1234",
     "new": "20260912132921-123a066", "new_pushed_at": "2026-09-12T13:35:02Z",
     "changelog": "修复 ……", "digest_new": "sha256:…"},
    {"name": "callfans/web-admin", "type": "frontend", "old": "20260901102030-abc1234",
     "new": "20260912132921-123a066", "changelog": "……", "alias": "admin"},
    {"name": "callfans/db", "type": "sql", "old": "20260901102030-abc1234",
     "new": ["20260910150000-def5678", "20260912132921-123a066"],
     "changelog": {"20260910150000-def5678": "…", "20260912132921-123a066": "…"}}
  ]
}
```

> 术语：制品类型 `type ∈ {server, frontend, sql}`，server 即需求里的"正常 docker 镜像"（下文机制描述中 docker 一词指操作手段）。

- server 当前版本：`docker images` 实时读取（本地真值）；目标取 push_time 最新的一个（跳过中间版本）。
- frontend / sql 当前版本：`state.json`（§7）。
- changelog 与类型读取：
  - frontend / sql：Harbor artifact 列表接口带 manifest annotations，直接读 `com.callfans.changelog`、`com.callfans.alias`、`com.callfans.type`，免拉取；
  - server：registry API 两步读 config 的 Labels（`com.callfans.changelog`、`com.callfans.type`，值为 server），免拉取（Q4）。
- sql 允许积压多个版本，按 push_time 升序全列（迁移语义）；server/frontend 只列最新目标。

## 5. 版本比较器（push_time 策略）

1. **tag 过滤**：去掉 `TAG_EXCLUDE`（默认 latest、dev）中的 tag；不匹配 `^\d{14}-[0-9a-f]{7,}$` 的 tag 跳过并在日志告警。
2. **基准版本（old）**：
   - server：本地 `docker images` 中该 repo 的 tag → 在 Harbor tag 列表里查它的 push_time；本地 tag 已不在 Harbor（被清理）则退化为解析 tag 时间戳前缀；再不行视为最旧（所有远端 tag 均领先）。
   - frontend / sql：state.json 记录的 tag → 同样查 push_time。
3. **领先判定**：远端存在 tag ≠ 当前 tag 且 push_time 晚于基准 → 领先；同 tag 但 digest 与记录不同 → 也判领先（CI 重推保护）。
4. **目标选择**：server/frontend 取 push_time 最大者；sql 取全部领先项按 push_time 升序排列。

## 6. 更新流程（立即更新）

**preflight**（任一失败整体报错，不动任何东西）：docker daemon 可达、COMPOSE_FILE 存在且可解析、MySQL 可连、磁盘剩余空间、无并发更新锁。

执行顺序（Q11 默认）**SQL → server（docker 镜像）→ 前端**，逐项串行；单项失败记录后继续；结束写汇总（success / failed / rolled_back + 原因）到 update_history.jsonl 并 WS 推送进度。

### 6.1 server（docker 镜像）

回滚采用**旧容器保留改名法**（验证通过前绝不删除旧容器/旧镜像）——"重启旧容器"字面可达成，且不依赖 tag 可逆性：

1. `docker compose -f $COMPOSE_FILE config --format json` 解析 镜像→service 映射；再读 compose.yml 原文，解析每个 service image 里的 `${VAR}` 变量名，得到 service→变量名映射；
2. 记录旧容器 ID、旧 tag、旧镜像 digest；`docker stop -t $DOCKER_STOP_TIMEOUT` 旧容器 → `docker rename <svc> <svc>-old`（保留）；
3. `docker pull` 新镜像（WS 推送阶段进度）；
4. **把新 tag 写入 compose 同目录 .env 的对应变量**（持久化），然后 `docker compose up -d <service>` 创建全新容器；
5. **运行成功判定**：HEALTH_WAIT_SECONDS 窗口内容器持续 Running、无重启循环；镜像有 healthcheck 则要求 healthy；
6. 成功 → `docker rm <svc>-old`；`docker rmi` 旧镜像（仅当无其他容器/标签引用）；写 success 记录；
7. 失败 → 抓取退出码与日志尾部作为原因 → **回滚**：**把 compose .env 变量写回旧 tag**、`docker rm -f` 新容器、`docker rename <svc>-old <svc>`、`docker start <svc>`；写 rolled_back 记录。

> 为什么持久写 .env 而不是临时进程环境变量注入：compose 目录 .env 里若仍是旧 tag，任何人以后手动 `docker compose up -d` 都会用旧 tag 静默重建容器（等于无意回滚）。持久写入保证应用内外一致；回滚时同样写回，语义对称。

> 注：需求顺序是"先停旧容器再 pull"，停机窗口包含 pull 时长；如想缩短可改为"先 pull 后停"，默认按需求原文执行。

### 6.2 前端静态文件

1. oras-py 拉取 artifact 到临时目录（zip 文件）；
2. `zipfile` 解压到 `<FRONTEND_OUTPUT_DIR>/<alias>.new`（alias 为空取 repo 名，Q12）；
3. 原子替换：`<alias>` → `<alias>.bak`，`<alias>.new` → `<alias>`；成功删 `.bak`（失败可手工改回）；
4. 任何一步失败，正式目录未被触碰，写 failed 记录。

### 6.3 SQL

1. oras-py 拉取 sql 文件；积压多版本时按 push_time **升序逐个**执行；
2. pymysql 连接（charset=utf8mb4）；整文件单事务逐语句执行（简易切分：分号 + 注释 + 引号内分号跳过）；
3. 全 DML 时失败整体回滚；含 DDL 时 DDL 隐式提交无法整体回滚 → 失败即停并记录已执行到的语句位置（Q10 风险）；
4. 成功 → state.json 记账（tag + digest 加入已执行列表），写 success 记录。

## 7. 本地版本记账（state）

### 7.1 问题：为什么前端/SQL 需要"账本"

三类制品里只有 docker 有本地真值（本地镜像仓库）。前端 zip 解压完制品即丢弃、SQL 执行完什么都不剩——**本地没有任何现成地方能查到"当前装的是哪个版本"**，只能由应用在每次更新成功后自己记账。docker 镜像则直接实时读 `docker images`，无需记账。

### 7.2 state.json（最小方案）

```json
{
  "frontend": {
    "callfans/web-admin": {"tag": "20260912132921-123a066", "digest": "sha256:…", "updated_at": "…"}
  },
  "sql": {
    "callfans/db": {"applied": [{"tag": "20260901102030-abc1234", "digest": "sha256:…"}]}
  }
}
```

位置沿用 tech-design 的 platformdirs 约定。首次运行：文件不存在 → frontend/sql 全部视为待更新，一键更新到最新即建立基线。

**风险**：账本丢失（应用重装、目录被删）——前端退化无害（重新提示全量、重装一次即可）；SQL 危险：账本丢失后若把历史 SQL 全部重跑，非幂等语句会坏库。Q14 已选最小方案，此风险接受，对策见 §10。

### 7.3 加固方案（已评估，Q14 未采纳，留档备升级）

| 对象 | 措施 | 效果 |
|------|------|------|
| SQL | 在目标库建记账表 `callfans_schema_history`（tag、digest、executed_at、result），检查时优先以记账表为准，state.json 仅作缓存 | 账本跟数据库走，应用重装/换机不丢，等价 Flyway 的 schema_history 思路 |
| 前端 | 解压时在 `<alias>` 目录写 `.callfans-version.json` | 部署目录自带版本，state.json 丢失后可读回基线 |

> 若未来 state.json 丢失成为实际问题（尤其 SQL 场景），按本节升级即可，不影响其他模块。

## 8. 模块目录（落在上位文档结构上）

```
src/callfans/core/
├── harbor.py        # Harbor API + registry v2 客户端（分页、basic auth、manifest/config 读取）
├── local.py         # docker images 读取、state.json 读写
├── compare.py       # push_time 比较器 + tag 过滤（§5）
├── checker.py       # 检查编排 → UpdatePlan
├── updaters/
│   ├── server_updater.py   # type=server：stop/rename/pull/compose .env 注入/健康判定/回滚
│   ├── frontend_updater.py # oras-py 拉取 + unzip + 原子替换
│   └── sql_updater.py      # oras-py 拉取 + pymysql 执行 + state.json 记账
└── history.py       # update_history.jsonl
service/api.py       # 增加：POST /check、GET /pending、POST /update；WS：check_done / update_progress
ui/                  # 托盘+窗口：两按钮、列表(old/new/changelog)、进度区
cli.py               # 增加：pending / check / update 子命令（headless 用）
```

## 9. 测试要点

- compare.py 纯函数矩阵：latest/dev 过滤、非格式 tag 告警跳过、push_time 领先、同 tag 不同 digest、本地 tag 不在 Harbor（退化路径）、sql 多版本积压排序
- SQL 切分器：多语句、注释、字符串含分号、（如 Q10 需要）DELIMITER
- 集成（本地 docker + 真实或 mock Harbor）：6.1 全链路，必测"新容器秒退 → 回滚成功且旧容器恢复服务 + compose .env 写回旧 tag"
- UI：两按钮与 WS 事件联动、服务重启后 UI 断线重连

## 10. 风险与对策

| 风险 | 对策 |
|------|------|
| tag 无版本纪律（乱推 latest） | TAG_EXCLUDE + 格式校验，不匹配即跳过并告警 |
| `compose up` 未换用新镜像 | 步骤 4 后校验容器 image digest == 新镜像 digest，不符即判失败走回滚 |
| compose.yml 里 image 未用 `${VAR}` 写法 | preflight 校验并给出明确报错（提示改造 compose） |
| SQL DDL 半途失败不可回滚 | 记录断点位置，人工修复后重跑（幂等前提 Q10） |
| state.json 丢失（Q14 已选最小方案，接受此风险） | SQL 账本丢失场景：默认**不自动重跑**历史 SQL，提示人工确认基线后再执行；后续可按 §7.3 升级 |
| Windows 上 Docker Desktop 未启动 | preflight 检测并给出明确提示 |
| Harbor API 分页 / 限流 | page_size=100 循环取全；失败重试 1 次 |
| 前端解压中断 / 磁盘不足 | 临时目录 + 原子替换，正式目录不受影响 |
| oras-py 对 Harbor 认证/注解支持不足 | 降级方案：registry v2 API 直接下载 layer blob（oras pull 本质即此） |
