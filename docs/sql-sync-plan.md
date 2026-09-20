# SQL 结构与配置同步方案（云库 → B 库）

> 版本 v0.2（2026-09-20）
> 需求来源：[idea.md](idea.md)；上位架构沿用 [tech-design.md](tech-design.md)（service + IPC + CLI/UI）
> 状态：核心决策已定（§1.1），**待确认自动执行护栏阈值后开工**

## 0. 可行性结论

**可行。** 核心判断：

1. **问题模型对口**：增量 SQL 迁移依赖"完整有序的历史"，客户跨多版本后历史断链即失效；声明式同步只依赖"云库终态 + B 库现状"，历史丢失不影响结果。这是本方案对"丢 SQL"的根治点。
2. **技术上有成熟积木但无开箱即用的 MySQL 全家桶**：migra 只支持 PostgreSQL（排除）；MySQL 生态的 mysqldbcompare 已随 MySQL Utilities 归档。可行路径是 **SQLAlchemy 反射 + alembic 的 compare_metadata** 做结构比对引擎（覆盖常规差异 80–90%），加一层**自研归一化/补齐**处理 MySQL 特有噪音。工作量集中在比对保真，而非从零造轮子。
3. **DDL 不可事务回滚是固有约束**：用"Down 脚本预生成 + 表级备份 + 断点续跑"缓解，破坏性操作默认拦截人工确认。**不建议首版做无人值守全自动 apply**。
4. **残余风险转移**：从"CI 不丢 SQL"变成"管理员及时把 A 库变更同步到云库"——纪律风险仍在，可用云库校验和/变更审计缓解（§14）。

---

## 1. 需求澄清与待确认问题

### 1.1 已确认决策（2026-09-20）

| # | 决策 | 设计影响 |
|---|------|---------|
| Q1 | 云库与 B 库均为 MySQL 8.0 | 归一化层保留显示宽度剥离（alembic/反射输出仍可能带 `int(11)`） |
| Q2 | **允许自动执行（含破坏性操作）** | 无人工审查 → §1.2 自动执行护栏成为唯一安全网，强制内置 |
| Q3 | 配置表清单放云端元表 | `callfans_sync.tables`，本地缓存 + 校验和 |
| Q5 | **定时全自动 apply**（`SQLSYNC_INTERVAL_HOURS`，默认 6h） | 并入现有 service 调度；失败/护栏触发 → 告警 |
| Q6 | 记账用 state.json（不建 B 库元表） | 与 Q14 最小方案一致；state 丢失无害——diff 本身幂等，重跑即自愈 |
| Q9 | **直接替代 sql_diff 制品流**（不并存） | checker 不再处理 type=sql 仓库；runner 移除 SQL 执行器；"立即更新"顺序变为 **sqlsync → server → 前端**；`.env` 的 MYSQL_* 段转为 B 库连接 |

未答复项采用默认：Q4 = B 库最小权限账号 + preflight 权限探测；Q7 = 首版不含视图/存储过程/触发器；Q8 = TLS + CA 文件路径配置。

### 1.2 自动执行护栏（Q2+Q5 决策的强制性配套，待确认阈值）

全自动 + 破坏性意味着"云库错了 → 全客户自动受损"。以下闸门**不可关闭**，触发即中止本次 apply 并告警（写入日志与托盘通知，人工用 `sqlsync plan` 排查后可用 `--force` 显式越过）：

| 护栏 | 默认阈值 | 防的场景 |
|------|---------|---------|
| G1 备份强制 | 每次必做（不可关） | 一切恢复路径的底座 |
| G2 漂移规模 | 单次语句数 > 200 或涉及表 > 清单 30% | 云库被大改/连错库/云库被清空 |
| G3 云库完整性 | 云库可见配置表数 < 清单数，或快照校验和与上次差异 > 阈值 | 云库误清空、同步到半截 |
| G4 删除行数 | 单表 DELETE > 1000 行 | 云端误清配置数据殃及客户 |
| G5 空跑检测 | 云库任意配置表行数为 0 且 B 库同表非空 → 该表跳过并告警 | 云库导数据失败 |

`--force` 越闸门的操作全量记录审计。**阈值可配，护栏本身不可禁用。**

| # | 问题 | 影响点 |
|---|------|--------|
| Q1 | 云库与 B 库的 MySQL 版本范围（5.7 / 8.0 / 混合）？ | information_schema 显示差异、类型字符串归一化规则 |
| Q2 | 破坏性操作默认策略：`DROP TABLE/COLUMN`、配置行 `DELETE` 是否允许自动执行？ | 建议：默认拦截仅生成脚本，`--allow-destructive` 显式放行 |
| Q3 | 配置表清单维护在哪：云端元表 / 随包配置文件？ | 建议：**云端元表** `callfans_sync.tables`，集中管理随连接分发，本地仅缓存 |
| Q4 | B 库账号实际权限边界（ALTER/CREATE/INDEX/INSERT/UPDATE/DELETE + information_schema 可读）？ | 执行器能力探测与报错文案 |
| Q5 | 执行模式：默认"生成计划人工确认"，还是定时全自动 apply？同步周期（并入现有 6h 检查或独立）？ | 决定 UI/CLI 交互与告警设计 |
| Q6 | 版本/校验和记账位置：B 库元表（推荐 `callfans_sync.meta`）+ 本地 state 缓存？ | 幂等跳过与审计 |
| Q7 | 首版范围是否限定"表结构 + 索引 + 配置表数据"，**不含**视图/存储过程/触发器/外键？ | 建议：外键纳入比对、routine/view 列 backlog |
| Q8 | 客户端 → 云库的网络形态（公网 TLS / 专线 / VPN）与证书管理方式？ | 连接配置与安全设计 |
| Q9 | 与现有 sql_diff 制品更新的关系：过渡期并存，成熟后替代？ | 集成方案与 UI 展示 |

### 非阻塞（默认假设）

| # | 假设 |
|---|------|
| A1 | 配置表必须有主键/唯一键；无键表默认**跳过并告警**（策略可配为：报错 / 跳过 / 仅结构不含数据） |
| A2 | 忽略规则支持：表、列、索引三级 |
| A3 | 数据同步方向严格单向（云 → B），绝不反向 |
| A4 | 单客户单 B 库，无多 B 库并发同步场景（多库即多份配置多次运行） |

---

## 2. 总体架构与数据流

```
┌──────────────┐   人工同步（管理员）    ┌──────────────┐
│  A 库（生产） │ ───────────────────► │  云库（标准） │  只读账号 + TLS
│  全量业务数据 │    结构+配置表变更      │  结构+配置    │
└──────────────┘                       └──────┬───────┘
                                              │ ① 反射读取（期望状态 E）
                                              ▼
                                   ┌────────────────────────┐
                                   │ callfans sync 引擎      │
                                   │ ② 反射读取 B 库（现状 C）│
                                   │ ③ diff(E, C) → 变更集   │
                                   │ ④ 生成 Up/Down + 报告    │
                                   │ ⑤ dry-run 审查 / 备份    │
                                   │ ⑥ 执行 + 断点 + 审计      │
                                   └──────────┬─────────────┘
                                              │ TLS，最小权限账号
                                              ▼
                                       ┌──────────────┐
                                       │ B 库（客户）   │
                                       └──────────────┘
```

数据流：`reflect(E) + reflect(C) → normalize → diff → plan(Up/Down/报告) → 审查 → backup → apply(断点) → 校验 → 记账`。

## 3. 技术选型对比与建议

### 3.1 数据库连接

| 候选 | 结论 |
|------|------|
| **PyMySQL（现有依赖，MIT）** | ✅ 保留：执行器与数据比对直连，`ssl={"ca": ...}` 支持 TLS |
| **SQLAlchemy Core（MIT，活跃维护，py3.7+）** | ✅ 新增：仅用于反射与差异比较（`MetaData.reflect`），不用其 ORM |
| mysql-connector-python | ✗ 与 PyMySQL 重复，Oracle 维护节奏受制 |

### 3.2 结构比对

| 候选 | 维护状态 | 结论 |
|------|---------|------|
| migra / sqlalchemy-diff | 活跃 | ✗ **仅支持 PostgreSQL** |
| MySQL Utilities（mysqldbcompare） | 2020 归档 | ✗ 依赖旧 Python，弃维 |
| Liquibase diff | 活跃 | ✗ 引入 JVM 与 XML 生态，重 |
| **alembic `autogenerate.compare_metadata`**（SQLAlchemy 反射） | 活跃（Alembic BSD） | ✅ **采用为比对引擎**：程序化调用（无需 Alembic 迁移目录），产出变更 op 列表，再编译为 MySQL DDL |
| 纯自研 INFORMATION_SCHEMA 比对 | — | 作为**归一化/补齐层**（处理 alembic 盲区：显示宽度、零填充、字符集归一、注释、索引细粒度变更） |

**已知保真缺口与对策**（选型的核心风险面）：

| 缺口 | 对策 |
|------|------|
| `int(11)` 显示宽度噪音（8.0.19+ 默认省略） | 归一化层剥离显示宽度再比对 |
| server_default / 注释变更检出不稳定 | 关键列默认值差异降级为"提示项"人工审查，不自动生成 |
| 索引变更（含 ASC/DESC、前缀长度）细粒度识别不全 | 自研 information_schema.statistics 比对补齐 |
| 字符集/排序规则差异 | 表选项级比对，生成 CONVERT 提示（默认仅告警不自动转） |
| **生成的 DDL 必须审查** | dry-run 输出完整 SQL + 影响评估，人工确认是流程硬环节 |

### 3.3 数据比对

| 候选 | 结论 |
|------|------|
| tablediff-cli 等现成工具 | ✗ 不成熟、引入额外运行时 |
| **自研 PK 分块比对** | ✅ 按主键/唯一键范围切块（`WHERE pk > ? ORDER BY pk LIMIT n`），逐块比哈希再细比行；内存有界，支持大配置表 |

## 4. 模块划分与目录结构（集成进现有程序）

```
src/callfans/core/sync/
├── config.py        # SyncConfig：连接、表清单、忽略规则、策略（.env + YAML/云端元表）
├── reflect.py       # 连接管理与元数据反射（云库只读 / B 库最小权限；TLS）
├── normalize.py     # 类型/默认值/索引的 MySQL 归一化（diff 保真的关键层）
├── schema_diff.py   # 结构比对：alembic compare_metadata + statistics 补齐
├── data_diff.py     # 配置表数据比对：PK 分块 + 行级 INSERT/UPDATE/DELETE 生成
├── plan.py          # SyncPlan：Up/Down SQL 有序编排 + 影响评估 + 校验和
├── backup.py        # 执行前备份（受影响表 → bak 表 + 本地 dump）
├── executor.py      # 断点执行、幂等、失败恢复
├── audit.py         # callfans_sync 元表读写 + update_history 落盘
└── report.py        # 人读报告（markdown/text）
service/api.py       # +POST /sqlsync/plan|apply、GET /sqlsync/status（阶段二再接 UI）
cli.py               # +`callfans sqlsync status|plan|apply|rollback`
installer/           # 打包无新增系统依赖（纯 Python）
```

**与现有程序的关系：集成，不重构。** 复用：配置加载（.env 扩展段）、IPC 服务与调度、preflight/日志/更新历史、打包发布链。新增 `core/sync` 独立域与 `sqlsync` 命令域；现有 sql_diff 制品流保持运行（Q9 过渡策略），互不干扰。

## 5. 配置设计示例

`.env` 追加（凭据沿用现有惯例，不落代码库）：

```ini
# --- sql sync ---
CLOUD_DB_HOST=cloud-mysql.example.com
CLOUD_DB_PORT=3306
CLOUD_DB_USER=callfans_ro
CLOUD_DB_PASSWORD=***
CLOUD_DB_NAME=callfans_standard
CLOUD_DB_CA=/path/to/ca.pem        # TLS
LOCAL_DB_HOST=127.0.0.1            # B 库（沿用 MYSQL_* 或独立段，待 Q4）
SQLSYNC_INTERVAL_HOURS=6
SQLSYNC_MODE=plan                  # plan(仅生成) | apply(需 Q2 策略允许)
```

表清单与策略（云端元表 `callfans_sync.tables`，本地缓存 `sync_tables.json`）：

```yaml
# 云端元表导出形态（示例）
tables:
  - name: sys_config            # 配置表：结构 + 数据
    data: true
    pk: id
    ignore_columns: [updated_at]
  - name: sys_dict
    data: true
    pk: code
  - name: mobile_system_notify  # 结构性表：仅结构
    data: false
ignore:
  tables: [tmp_*]
  indexes: [idx_ignored_*]
policy:
  destructive: block             # block(默认) | confirm | allow
  no_pk_tables: skip_warn        # error | skip_warn
  max_rows_per_table: 1000000    # 超限仅结构不比数据，报告提示
```

## 6. 核心流程

1. **连接与版本检查**：双库连通、TLS 生效、版本与权限探测（B 库缺 ALTER → 明确报错清单）；云库读取表清单。
2. **结构比对**：反射两侧元数据 → 归一化 → `compare_metadata` + 索引补齐 → 变更 op（新增表/列、改类型、改可空、索引增删、表选项）。忽略规则过滤。
3. **数据比对**：仅 `data: true` 且有 PK 的表；PK 分块哈希初筛 → 差异行精比 → 新增行 INSERT（按云库行）、更新行 UPDATE（按 PK 全列覆盖）、删除行 DELETE（按 PK）。
4. **差异 SQL 生成（Up/Down）**：
   - Up：拓扑排序（被依赖表先建、外键最后加）；DDL 与 DML 分段，段间落"断点标记"注释；
   - Down：逆操作预生成（DROP COLUMN 的 Down 仅重建列定义并**标注数据不可恢复**；DELETE 的 Down 依赖备份回灌）；
   - 附影响评估：每表变更行数/语句数、风险级（info/warn/destructive）。
5. **审查与执行**：`plan` 命令产出 SQL+报告（文件+IPC 事件）；`apply` 前强制备份（受影响表 `CREATE TABLE _bak_<ts>` + mysqldump/`SELECT INTO OUTFILE` 本地副本），再按序执行，逐语句记审计与断点。
6. **回滚与失败恢复**：失败即停 → 报告已执行断点；恢复路径 = Down 脚本 + 备份表回灌；重跑天然幂等（diff 为空即跳过）。校验通过后写 `callfans_sync.meta`（云库快照校验和、执行时间、结果）。

伪代码（接口签名）：

```python
class SqlSync:
    def plan(self) -> SyncPlan: ...          # 只读，可随时重跑
    def apply(self, plan, *, allow_destructive=False) -> SyncReport: ...
    def rollback(self, run_id) -> SyncReport: ...

@dataclass
class SyncPlan:
    up: list[Statement]; down: list[Statement]
    impact: dict[str, TableImpact]; checksum_cloud: str
```

## 7. CLI / API 设计

```
callfans sqlsync status            # 双库连通/版本/上次同步结果/当前漂移概要（只比对不执行）
callfans sqlsync plan              # 生成并保存 Up/Down + 报告（默认输出到数据目录 sqlsync/<ts>/）
callfans sqlsync apply [--allow-destructive] [--plan-file ...]
callfans sqlsync rollback --run-id <id>
```

IPC（阶段二，接 UI 的"SQL 同步"页签）：`GET /api/v1/sqlsync/status`、`POST /api/v1/sqlsync/plan|apply`，进度经既有 WS 事件流（`sqlsync_progress`）。

## 8. 安全、权限、审计

- 云库账号只读（SELECT + information_schema）；B 库账号最小权限（目标库 ALTER/CREATE/INSERT/UPDATE/DELETE + information_schema 只读）。
- 全链路 TLS（PyMySQL `ssl` 参数，CA 路径进配置）；凭据只存 .env（沿用现状），日志与报告中脱敏。
- 审计：每次 run 记 `update_history.jsonl`（复用）+ B 库 `callfans_sync.run_log`（run_id、起止、语句数、结果、校验和）。
- 单向数据流由代码结构保证（云库连接只开只读事务；执行器仅持 B 库写句柄）。

## 9. 错误处理与日志

- 连接/权限错误：明确列出缺失项（哪个库、哪个权限）。
- 比对异常：单表失败不中断整次 plan，报告中列出失败表与原因。
- 执行失败：停在该语句，记录断点（已执行前缀），报告回滚指引；DDL 隐式提交特性在报告中显式提示（哪些不可回滚）。
- 超时与锁：DDL 语句设 `lock_wait_timeout` 会话变量；大表 DDL 提示风险（首版不自动做 online DDL 优化）。

## 10. 测试策略

- **单元**：normalize 规则矩阵（显示宽度/零填充/字符集）、PK 分块算法、Up/Down 生成逆一致性。
- **集成（沙箱 MySQL）**：CI 用 docker 起两个 MySQL（版本组合覆盖 Q1），构造已知漂移 → plan → 断言生成的 DDL/DML；apply 后再 diff 必须为空（**roundtrip=空 是核心验收**）。
- **回滚测试**：apply 半途杀进程 → 恢复路径可走通；Down+备份回灌后与执行前结构一致。
- **边界**：无 PK 配置表、忽略规则命中、超限大表、云库不可达、B 库权限不足、MySQL 5.7 vs 8.0 混合。
- 既有 121 个测试保持全绿（零回归）。

## 11. 部署与运行

- 随现有安装包发布（纯 Python 依赖：+sqlalchemy、+alembic，无系统依赖）。
- 运行形态（Q5）：并入现有 service 调度，`SQLSYNC_INTERVAL_HOURS`（默认 6）自动 **plan+apply**；护栏触发或执行失败 → 告警并停，人工排查。
- 监控告警：护栏触发、漂移长期为零（疑似云库未更新）、连续失败 N 次告警。

## 12. 里程碑与任务分解

| # | 任务 | 输入 | 输出/验收 | 预估 |
|---|------|------|----------|------|
| M0 | **护栏与备份框架**（G1–G5 + bak 表 + 审计） | §1.2 阈值 | 护栏单测全绿（每条场景一测） | 2d |
| M1 | 反射与归一化层 | Q1（均 8.0） | normalize 单测全绿；两库元数据可稳定读取 | 2d |
| M2 | 结构比对引擎 | M1 | 构造的 20 个漂移用例 DDL 断言通过 | 3d |
| M3 | 数据比对与计划生成 | M2 | Up/Down+报告；roundtrip=空 集成测试 | 3d |
| M4 | 执行器/断点/恢复（对接 M0 护栏） | M3 | apply 中断恢复测试 | 2d |
| M5 | CLI + 配置 + 云端元表 + **替代 sql_diff**（checker 跳过 type=sql） | Q3/Q4/Q9 | `sqlsync` 命令可用；旧 SQL 流移除 | 2d |
| M6 | service 定时全自动 + UI（状态/告警/立即同步） | M5 | 定时链路 + 托盘告警 | 3d |

依赖：M0 与 M1 并行 → M2 → M3 → M4 → M5 → M6；每步以沙箱 MySQL 集成测试为验收闸门。

## 13. 验收标准

1. 对任意构造的客户漂移场景：`plan` 生成的 Up 执行后，再次比对**差异为零**（roundtrip）。
2. 破坏性操作在默认策略下被拦截并出现在报告"需确认"区。
3. apply 中断后可按指引恢复（Down/备份二选一）且恢复后 diff 状态明确。
4. 云库/B 库凭据与 TLS 符合 §8；日志无明文凭据。
5. 现有更新链路（Harbor 检查/镜像/前端）回归零影响。

## 14. 风险与缓解

| 风险 | 缓解 |
|------|------|
| diff 保真不足（假差异→错误 DDL） | 归一化层 + dry-run 人工审查硬环节 + roundtrip 测试矩阵 |
| DDL 不可回滚 | Down 预生成 + 备份表 + 断点 + 破坏性默认拦截 |
| 云库维护纪律（管理员漏同步 A 变更） | 云库快照校验和 + 变更审计表；漂移长期为零时告警提示"疑似云库未更新" |
| B 库权限不足/被收紧 | preflight 权限探测，缺项明确列名 |
| 大表 DDL 锁表影响业务 | 影响评估标注；执行窗口人工选择；online DDL 列 backlog |
| 配置表误删行（云端清数据殃及客户） | DELETE 归为破坏性，默认拦截；上限行数策略 |

## 15. 现状评估步骤与集成建议（对现有程序）

评估（半天）：核对现有配置加载、调度与 IPC 的可复用面；确认 sqlalchemy/alembic 与打包链兼容（纯 Python，PyInstaller 无额外 hiddenImports 风险，仍需构建验证）。

**建议：集成而非重构。** 复用现有 service/调度/IPC/打包；新增 `core/sync` 域与 `sqlsync` 命令域。**Q9（直接替代）落点**：

- checker：不再产生 `type=sql` 待更新项（Harbor 的 sql_diff 仓库忽略）
- runner：执行顺序改为 **sqlsync → server → 前端**，SQL 执行器（sql_updater 的制品分支）移除
- `.env`：`MYSQL_*` 段语义转为 B 库连接；新增 `CLOUD_DB_*` 段与 `SQLSYNC_*` 策略段
- state.json：保留 `frontend`/`server` 记账，新增 `sqlsync` 段（云库快照校验和、最近 run）；`sql` 段随旧流程废弃
- UI：待更新列表不再显示 sql 制品项；新增同步状态/告警展示（M6）
