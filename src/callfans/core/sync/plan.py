"""同步计划编排（M3）：结构 + 数据 → Up/Down 有序计划 + 影响评估 + 护栏 + 校验和。

Up 顺序：建/改表与列（结构）→ 数据 DML → 二级索引（减少 DML 期间索引维护）。
Down 顺序：Up 的严格逆序 + 每条逆操作。
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
from .reflect import reflect_metadata, row_count, visible_tables
from .schema_diff import SchemaChange, SchemaDiffResult, diff_schemas

_DML_ORDER = {"insert": 0, "update": 1, "delete": 2}


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

    @property
    def statement_count(self) -> int:
        return len(self.schema_changes) + sum(len(d.changes) for d in self.data_tables)

    @property
    def affected_tables(self) -> set[str]:
        tables = {c.table for c in self.schema_changes}
        tables |= {d.table for d in self.data_tables if d.changes}
        return tables

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
        structure = [c.ddl for c in self.schema_changes if not c.kind.endswith("_index")]
        dml: list[str] = []
        for d in self.data_tables:
            dml.extend(c.up_sql for c in d.changes)
        indexes = [c.ddl for c in self.schema_changes if c.kind.endswith("_index")]
        return structure + dml + indexes

    def down_statements(self) -> list[str]:
        ups = self.up_statements()
        inverses = ([c.down_ddl for c in self.schema_changes if not c.kind.endswith("_index")]
                    + [c.down_sql for d in self.data_tables for c in d.changes]
                    + [c.down_ddl for c in self.schema_changes if c.kind.endswith("_index")])
        return list(reversed(inverses))

    # ---- 报告 ----

    def report_text(self) -> str:
        lines = [f"sqlsync 计划 run={self.run_id}（{self.created_at}）"]
        if self.targets:
            lines.append(f"目标: 云库 {self.targets.get('cloud')} → B 库 {self.targets.get('local')}")
        if self.manifest_tables:
            shown = ", ".join(self.manifest_tables[:12]) + (
                f" 等 {len(self.manifest_tables)} 张" if len(self.manifest_tables) > 12 else "")
            lines.append(f"清单: {shown}")
        lines.append(f"语句总数: {self.statement_count}｜涉及表: {len(self.affected_tables)}")
        for d in self.data_tables:
            if d.changes or d.skipped_reason:
                note = d.skipped_reason or (
                    f"增 {d.inserts} / 改 {d.updates} / 删 {d.deletes}")
                lines.append(f"  数据 {d.table}: {note}")
        schema_summary = "；".join(
            f"{c.kind} {c.table}" for c in self.schema_changes
        ) or "结构无差异"
        lines.append(f"结构: {schema_summary}")
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


def _columns_of(md, name) -> list[str]:
    return [c.name for c in md.tables[name].columns]


def _fetch_all(engine, table: str, columns: list[str], pk: str, batch: int = 1000) -> list[dict]:
    """键集分页拉取全表（每批短查询，无长生命周期游标），返回 dict 行。

    流式游标 + 生成器跨 yield 持连接的模式在实测中出现连接被回收导致的
    InterfaceError(0,'')（2026-09-20 沙箱实测定论）；配置表行数受护栏
    上限约束，全量入内存可接受。
    """
    cols = ", ".join(f"`{c}`" for c in columns)
    rows: list[dict] = []
    last = None
    with engine.connect() as conn:
        while True:
            if last is None:
                q = text(f"SELECT {cols} FROM `{table}` ORDER BY `{pk}` LIMIT {batch}")
                chunk = [dict(r) for r in conn.execute(q).mappings().fetchall()]
            else:
                q = text(f"SELECT {cols} FROM `{table}` WHERE `{pk}` > :last "
                         f"ORDER BY `{pk}` LIMIT {batch}")
                chunk = [dict(r) for r in conn.execute(q, {"last": last}).mappings().fetchall()]
            rows.extend(chunk)
            if len(chunk) < batch:
                return rows
            last = chunk[-1][pk]


def build_plan(cfg: CloudSyncConfig, cloud_engine, local_engine,
               manifest: list[TableRule]) -> SyncPlan:
    run_id, created_at = _now_id()

    # G3：云库完整性（先于一切）
    violations: list[GuardViolation] = []
    cloud_visible = visible_tables(cloud_engine)
    violations += check_cloud_integrity(cloud_visible, manifest)

    cloud_md = reflect_metadata(cloud_engine, only=[r.name for r in manifest if r.name in cloud_visible])
    local_md = reflect_metadata(local_engine, only=[r.name for r in manifest])

    schema = diff_schemas(cloud_md, local_md, manifest)
    added_tables = {c.table for c in schema.changes if c.kind == "add_table"}
    dropped_tables = {c.table for c in schema.changes if c.kind == "drop_table"}

    data_tables: list[TableDataDiff] = []
    for rule in manifest:
        if rule.name not in cloud_visible:
            continue
        if rule.name in dropped_tables:
            data_tables.append(TableDataDiff(
                rule.name, rule.pk, skipped_reason="表将被删除，无数据操作"))
            continue
        if not rule.data:
            data_tables.append(TableDataDiff(
                rule.name, rule.pk, skipped_reason="仅同步结构"))
            continue
        if not rule.pk:
            if cfg.policy.no_pk_tables == "error":
                violations.append(GuardViolation(
                    "G0", f"配置表 {rule.name} 无主键且策略为 error", tables=[rule.name]))
            else:
                data_tables.append(TableDataDiff(
                    rule.name, rule.pk, skipped_reason="无主键，跳过数据同步（策略 skip_warn）"))
            continue

        cloud_cols = _columns_of(cloud_md, rule.name)
        local_cols = _columns_of(local_md, rule.name) if rule.name in local_md.tables else []
        c_rows = row_count(cloud_engine, rule.name)
        l_rows = row_count(local_engine, rule.name) if rule.name in local_md.tables else 0

        if rule.name in added_tables:
            # 新建表：种子数据
            rows = _fetch_all(cloud_engine, rule.name, cloud_cols, rule.pk)
            data_tables.append(TableDataDiff(
                rule.name, rule.pk, changes=seed_inserts(rule.name, rows),
                inserts=len(rows), cloud_rows=c_rows, local_rows=0))
            continue

        if c_rows > cfg.policy.max_rows_per_table:
            data_tables.append(TableDataDiff(
                rule.name, rule.pk, cloud_rows=c_rows, local_rows=l_rows,
                skipped_reason=f"行数 {c_rows} 超上限 {cfg.policy.max_rows_per_table}，仅同步结构"))
            continue
        # G5：云库空表而 B 库非空
        v5 = check_empty_cloud_table(rule.name, c_rows, l_rows)
        if v5:
            violations += v5
            data_tables.append(TableDataDiff(
                rule.name, rule.pk, cloud_rows=c_rows, local_rows=l_rows,
                skipped_reason="G5：云库表为空，跳过数据同步"))
            continue

        changes = diff_rows(
            rule.name, rule.pk,
            _fetch_all(cloud_engine, rule.name, cloud_cols, rule.pk),
            _fetch_all(local_engine, rule.name, local_cols, rule.pk),
            ignore=set(rule.ignore_columns),
        )
        tdd = TableDataDiff(rule.name, rule.pk, changes=changes,
                            inserts=sum(1 for c in changes if c.kind == "insert"),
                            updates=sum(1 for c in changes if c.kind == "update"),
                            deletes=sum(1 for c in changes if c.kind == "delete"),
                            cloud_rows=c_rows, local_rows=l_rows)
        # G4：单表删除行数
        violations += check_delete_rows(rule.name, tdd.deletes, cfg.policy)
        data_tables.append(tdd)

    # G2：漂移规模
    violations += check_plan_scale(
        len(schema.changes) + sum(len(d.changes) for d in data_tables),
        {c.table for c in schema.changes} | {d.table for d in data_tables if d.changes},
        manifest, cfg.policy,
    )

    # 云库校验和：结构指纹 + 行数（v1 口径，记录于 state 用于漂移追踪）
    checksum = _cloud_checksum(cloud_md, data_tables)

    def _target(t) -> str:
        return f"{t.host}:{t.port}/{t.database} ({t.user})"

    return SyncPlan(run_id, created_at, schema.changes, data_tables,
                    violations, checksum,
                    targets={"cloud": _target(cfg.cloud), "local": _target(cfg.local)},
                    manifest_tables=[r.name for r in manifest])


def _cloud_checksum(cloud_md, data_tables: list[TableDataDiff]) -> str:
    h = hashlib.sha256()
    for name in sorted(cloud_md.tables.keys()):
        h.update(name.encode())
        h.update(repr(table_fingerprint(cloud_md.tables[name])).encode())
    for d in sorted(data_tables, key=lambda x: x.table):
        h.update(f"{d.table}:{d.cloud_rows}".encode())
    return h.hexdigest()[:16]
