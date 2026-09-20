"""同步计划编排（M3 + 2026-09-21 多库）：结构 + 数据 → Up/Down 有序计划。

多库模式（.env 无 CLOUD_DB_NAME 时启用）：
- 范围 = 云库服务器全部非系统库（排除 SQLSYNC_EXCLUDE_DBS 名单），
  B 库同名对应；云有 B 无 → 计划含 CREATE DATABASE
- 清单行 rule.db=None 适用所有库（表在该库存在才生效）；
  rule.db 显式指定 → 缺表触发 G3
单库模式（配了 CLOUD_DB_NAME）：仅同步该库，行为与旧版一致。

Up 顺序：建库/建表与列（结构）→ 数据 DML → 二级索引。Down 严格逆序。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from .config import CloudSyncConfig, TableRule
from .data_diff import DataChange, TableDataDiff, diff_rows, seed_inserts
from .guards import (
    GuardViolation, check_cloud_integrity, check_delete_rows, check_empty_cloud_table,
    check_plan_scale,
)
from .normalize import table_fingerprint
from .reflect import (
    list_databases, reflect_metadata, row_count, tables_in,
)
from .schema_diff import SchemaChange, SchemaDiffResult, diff_schemas


@dataclass
class SyncPlan:
    run_id: str
    created_at: str
    schema_changes: list[SchemaChange]
    data_tables: list[TableDataDiff]
    guard_violations: list[GuardViolation]
    checksum_cloud: str
    # 诊断信息：解析后的连接目标与清单表明细（定位"改错服务器/登记错库"）
    targets: dict = field(default_factory=dict)
    manifest_tables: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)

    @property
    def statement_count(self) -> int:
        return len(self.schema_changes) + sum(len(d.changes) for d in self.data_tables)

    @property
    def affected_tables(self) -> set[str]:
        tables = {c.fq for c in self.schema_changes}
        tables |= {(f"{d.db}.{d.table}" if d.db else d.table)
                   for d in self.data_tables if d.changes}
        return tables

    @property
    def backup_targets(self) -> list[tuple[str, str]]:
        """执行前需备份的 (db, table) 对。"""
        out = []
        for c in self.schema_changes:
            if c.kind not in ("create_database",):
                out.append((c.db, c.table))
        for d in self.data_tables:
            if d.changes:
                out.append((d.db, d.table))
        return sorted(set(out))

    @property
    def destructive(self) -> list[str]:
        return [c.ddl for c in self.schema_changes if c.destructive] + [
            c.up_sql for d in self.data_tables for c in d.changes if c.kind == "delete"
        ]

    @property
    def fatal(self) -> bool:
        return any(v.fatal for v in self.guard_violations)

    # ---- 有序语句 ----

    def up_statements(self) -> list[str]:
        creates_db = [c.ddl for c in self.schema_changes if c.kind == "create_database"]
        structure = [c.ddl for c in self.schema_changes
                     if not c.kind.endswith("_index") and c.kind != "create_database"]
        dml: list[str] = []
        for d in self.data_tables:
            dml.extend(c.up_sql for c in d.changes)
        indexes = [c.ddl for c in self.schema_changes if c.kind.endswith("_index")]
        return creates_db + structure + dml + indexes

    def down_statements(self) -> list[str]:
        inverses = (
            [c.down_ddl for c in self.schema_changes if c.kind == "create_database"]
            + [c.down_ddl for c in self.schema_changes
               if not c.kind.endswith("_index") and c.kind != "create_database"]
            + [c.down_sql for d in self.data_tables for c in d.changes]
            + [c.down_ddl for c in self.schema_changes if c.kind.endswith("_index")]
        )
        return list(reversed(inverses))

    # ---- 报告 ----

    def report_text(self) -> str:
        lines = [f"sqlsync 计划 run={self.run_id}（{self.created_at}）"]
        if self.targets:
            lines.append(f"目标: 云库 {self.targets.get('cloud')} → B 库 {self.targets.get('local')}")
        if self.databases:
            shown = ", ".join(self.databases[:8]) + (
                f" 等 {len(self.databases)} 个" if len(self.databases) > 8 else "")
            lines.append(f"库范围: {shown}")
        if self.manifest_tables:
            shown = ", ".join(self.manifest_tables[:12]) + (
                f" 等 {len(self.manifest_tables)} 张" if len(self.manifest_tables) > 12 else "")
            lines.append(f"清单: {shown}")
        lines.append(f"语句总数: {self.statement_count}｜涉及表: {len(self.affected_tables)}")
        for d in self.data_tables:
            if d.changes or d.skipped_reason:
                prefix = f"[{d.db}] " if d.db else ""
                note = d.skipped_reason or (
                    f"增 {d.inserts} / 改 {d.updates} / 删 {d.deletes}")
                lines.append(f"  数据 {prefix}{d.table}: {note}")
        for c in self.schema_changes:
            if c.kind == "create_database":
                lines.append(f"  建库: {c.table}")
        structure = [c for c in self.schema_changes
                     if c.kind not in ("create_database",) and not c.kind.endswith("_index")]
        indexes = [c for c in self.schema_changes if c.kind.endswith("_index")]
        if structure or indexes:
            def _loc(c):
                return f"[{c.db}] {c.table}" if c.db else c.table
            parts = [f"{_loc(c)}（{c.detail}）" if c.detail else _loc(c) for c in structure + indexes]
            lines.append("结构: " + "；".join(parts))
        else:
            lines.append("结构: 无差异")
        if self.destructive:
            lines.append(f"⚠ 破坏性语句 {len(self.destructive)} 条（drop/delete）")
        if self.guard_violations:
            lines.append("护栏:")
            for v in self.guard_violations:
                mark = "中止" if v.fatal else "跳过"
                lines.append(f"  [{v.guard}] {mark} — {v.message}")
        else:
            lines.append("护栏: 全部通过")
        lines.append(f"云库校验和: {self.checksum_cloud}")
        return "\n".join(lines)


def _now_id() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d%H%M%S"), now.isoformat()


def _columns_of(md, db, name) -> list[str]:
    from .reflect import lookup_table
    t = lookup_table(md, db, name)
    return [c.name for c in t.columns] if t is not None else []


def _fetch_all(engine, db: str, table: str, columns: list[str], pk: str,
               batch: int = 1000) -> list[dict]:
    """键集分页拉取全表（每批短查询），返回 dict 行。"""
    fq = f"`{db}`.`{table}`" if db else f"`{table}`"
    cols = ", ".join(f"`{c}`" for c in columns)
    rows: list[dict] = []
    last = None
    with engine.connect() as conn:
        while True:
            if last is None:
                q = text(f"SELECT {cols} FROM {fq} ORDER BY `{pk}` LIMIT {batch}")
                chunk = [dict(r) for r in conn.execute(q).mappings().fetchall()]
            else:
                q = text(f"SELECT {cols} FROM {fq} WHERE `{pk}` > :last "
                         f"ORDER BY `{pk}` LIMIT {batch}")
                chunk = [dict(r) for r in conn.execute(q, {"last": last}).mappings().fetchall()]
            rows.extend(chunk)
            if len(chunk) < batch:
                return rows
            last = chunk[-1][pk]


def build_plan(cfg: CloudSyncConfig, cloud_engine, local_engine,
               manifest: list[TableRule]) -> SyncPlan:
    run_id, created_at = _now_id()
    violations: list[GuardViolation] = []
    schema_changes: list[SchemaChange] = []
    data_tables: list[TableDataDiff] = []

    # 范围：单库（CLOUD_DB_NAME）或多库自动发现
    exclude = set(cfg.policy.exclude_dbs)
    if cfg.cloud.database:
        databases = [cfg.cloud.database]
    else:
        databases = list_databases(cloud_engine, exclude)
    local_dbs = set(list_databases(local_engine))

    for db in databases:
        rules = [r for r in manifest if r.db in (None, "") or r.db == db]
        if not rules:
            continue
        cloud_tables = tables_in(cloud_engine, db)
        # G3：显式绑定该库的清单表缺失 → 中止
        explicit_missing = sorted(r.name for r in rules
                                  if r.db == db and r.name not in cloud_tables)
        if explicit_missing:
            violations += check_cloud_integrity(
                set(), [TableRule(name=n, db=db) for n in explicit_missing])

        applicable = [r for r in rules if r.name in cloud_tables]
        if not applicable:
            continue

        local_has_db = db in local_dbs
        if not local_has_db:
            schema_changes.append(SchemaChange(
                "create_database", db,
                f"CREATE DATABASE `{db}` DEFAULT CHARACTER SET utf8mb4 "
                f"COLLATE utf8mb4_0900_ai_ci;",
                detail="B 库缺失，随计划创建",
                down_ddl=f"DROP DATABASE IF EXISTS `{db}`;  -- 危险：含数据，仅在空跑回滚时使用",
                db=db,
            ))
            cloud_md = reflect_metadata(
                cloud_engine, only=[r.name for r in applicable], schema=db)
            # B 库无此库：全部清单表为新建 + 种子数据
            schema_changes.extend(diff_result_for_new_db(cloud_md, applicable, db).changes)
            for rule in applicable:
                if rule.data and rule.pk:
                    cols = _columns_of(cloud_md, db, rule.name)
                    rows = _fetch_all(cloud_engine, db, rule.name, cols, rule.pk)
                    data_tables.append(TableDataDiff(
                        rule.name, rule.pk, db=db,
                        changes=seed_inserts(rule.name, rows, db=db),
                        inserts=len(rows), cloud_rows=len(rows), local_rows=0))
            continue

        cloud_md = reflect_metadata(cloud_engine, only=[r.name for r in applicable], schema=db)
        local_md = reflect_metadata(local_engine, only=[r.name for r in applicable], schema=db)
        diff = diff_schemas(cloud_md, local_md, applicable, db=db)
        schema_changes.extend(diff.changes)

        added = {c.table for c in diff.changes if c.kind == "add_table"}
        dropped = {c.table for c in diff.changes if c.kind == "drop_table"}
        for rule in applicable:
            data_tables.append(_diff_table_data(
                cfg, cloud_engine, local_engine, rule, db,
                cloud_md, local_md, added, dropped, violations))

    # G2：漂移规模
    total_stmts = len(schema_changes) + sum(len(d.changes) for d in data_tables)
    affected = ({c.fq for c in schema_changes}
                | {(f"{d.db}.{d.table}" if d.db else d.table)
                   for d in data_tables if d.changes})
    violations += check_plan_scale(total_stmts, affected, manifest, cfg.policy)

    def _target(t) -> str:
        db_part = t.database if t.database else "(多库自动)"
        return f"{t.host}:{t.port}/{db_part} ({t.user})"

    checksum = _checksum(cfg, databases, cloud_engine, data_tables)
    return SyncPlan(run_id, created_at, schema_changes, data_tables,
                    violations, checksum,
                    targets={"cloud": _target(cfg.cloud), "local": _target(cfg.local)},
                    manifest_tables=[f"[{r.db}] {r.name}" if r.db else r.name
                                     for r in manifest],
                    databases=databases)


def diff_result_for_new_db(cloud_md, applicable, db) -> SchemaDiffResult:
    """B 库整体缺失：全部清单表 → add_table（复用 diff_schemas 的空 local）。"""
    from sqlalchemy import MetaData
    empty = MetaData()
    result = diff_schemas(cloud_md, empty, applicable, db=db)
    return result


def _diff_table_data(cfg, cloud_engine, local_engine, rule, db,
                     cloud_md, local_md, added, dropped, violations) -> TableDataDiff:
    from .reflect import lookup_table

    base = dict(table=rule.name, rule_pk=rule.pk, db=db)
    if rule.name in dropped:
        return TableDataDiff(**base, skipped_reason="表将被删除，无数据操作")
    if not rule.data:
        return TableDataDiff(**base, skipped_reason="仅同步结构")
    if not rule.pk:
        if cfg.policy.no_pk_tables == "error":
            violations.append(GuardViolation(
                "G0", f"[{db}] 配置表 {rule.name} 无主键且策略为 error", tables=[rule.name]))
        else:
            return TableDataDiff(**base, skipped_reason="无主键，跳过数据同步（策略 skip_warn）")
        return TableDataDiff(**base)

    cloud_cols = _columns_of(cloud_md, db, rule.name)
    c_rows = row_count(cloud_engine, db, rule.name)
    local_t = lookup_table(local_md, db, rule.name)
    l_rows = row_count(local_engine, db, rule.name) if local_t is not None else 0

    if rule.name in added or local_t is None:
        rows = _fetch_all(cloud_engine, db, rule.name, cloud_cols, rule.pk)
        return TableDataDiff(**base, changes=seed_inserts(rule.name, rows, db=db),
                             inserts=len(rows), cloud_rows=c_rows, local_rows=0)

    if c_rows > cfg.policy.max_rows_per_table:
        return TableDataDiff(**base, cloud_rows=c_rows, local_rows=l_rows,
                             skipped_reason=f"行数 {c_rows} 超上限 "
                             f"{cfg.policy.max_rows_per_table}，仅同步结构")
    v5 = check_empty_cloud_table(f"[{db}] {rule.name}", c_rows, l_rows)
    if v5:
        violations.extend(v5)
        return TableDataDiff(**base, cloud_rows=c_rows, local_rows=l_rows,
                             skipped_reason="G5：云库表为空，跳过数据同步")

    local_cols = _columns_of(local_md, db, rule.name)
    changes = diff_rows(
        rule.name, rule.pk,
        _fetch_all(cloud_engine, db, rule.name, cloud_cols, rule.pk),
        _fetch_all(local_engine, db, rule.name, local_cols, rule.pk),
        ignore=set(rule.ignore_columns), db=db,
    )
    tdd = TableDataDiff(**base, changes=changes,
                        inserts=sum(1 for c in changes if c.kind == "insert"),
                        updates=sum(1 for c in changes if c.kind == "update"),
                        deletes=sum(1 for c in changes if c.kind == "delete"),
                        cloud_rows=c_rows, local_rows=l_rows)
    violations += check_delete_rows(f"[{db}] {rule.name}", tdd.deletes, cfg.policy)
    return tdd


def _checksum(cfg, databases, cloud_engine, data_tables) -> str:
    h = hashlib.sha256()
    for db in databases:
        h.update(db.encode())
        for name in sorted(tables_in(cloud_engine, db)):
            h.update(f"{db}.{name}".encode())
    for d in sorted(data_tables, key=lambda x: f"{x.db}.{x.table}"):
        h.update(f"{d.db}:{d.table}:{d.cloud_rows}".encode())
    return h.hexdigest()[:16]
