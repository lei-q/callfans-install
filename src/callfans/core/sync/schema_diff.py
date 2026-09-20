"""结构比对引擎（M2）：指纹级确定性 diff → MySQL DDL。

主路径：normalize.table_fingerprint 归一化比较（已消显示宽度等噪音），
DDL 生成走 SQLAlchemy MySQL 方言编译器；alembic compare_metadata 保留为
可选交叉校验（cross_check），不在主生成路径（决策 2026-09-20，见方案 §3.2 注）。

范围（Q7 默认）：表、列、二级索引、主键；外键/视图/存储过程列 backlog。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable, DropIndex, DropTable

from .config import TableRule
from .normalize import column_fingerprint, index_fingerprint, table_fingerprint

_DIALECT = mysql.dialect()

KIND_LABEL = {
    "add_table": "新建表",
    "drop_table": "删除表",
    "add_column": "新增列",
    "drop_column": "删除列",
    "modify_column": "修改列",
    "add_index": "新增索引",
    "drop_index": "删除索引",
    "change_pk": "变更主键",
}


@dataclass
class SchemaChange:
    kind: str
    table: str
    ddl: str
    destructive: bool = False
    detail: str = ""
    down_ddl: str = ""  # 逆操作（drop_table/drop_column 数据恢复依赖备份）


@dataclass
class SchemaDiffResult:
    changes: list[SchemaChange] = field(default_factory=list)

    @property
    def statement_count(self) -> int:
        return len(self.changes)

    @property
    def affected_tables(self) -> set[str]:
        return {c.table for c in self.changes}

    @property
    def destructive(self) -> list[SchemaChange]:
        return [c for c in self.changes if c.destructive]

    def summary(self) -> str:
        if not self.changes:
            return "结构无差异"
        return "；".join(
            f"{KIND_LABEL.get(c.kind, c.kind)} {c.table}"
            + (f"（{c.detail}）" if c.detail else "")
            for c in self.changes
        )


def _quote(name: str) -> str:
    return f"`{name}`"


def _col_ddl(col) -> str:
    return str(CreateColumn(col).compile(dialect=_DIALECT))


def _force_quote(md) -> None:
    """强制全量反引号引用（默认仅在撞保留字时引用，`value` 这类名字有风险）。

    引用开关在 quoted_name 对象上（Table.name / Column.name / Index.name），
    不在 Table/Column 自身的 quote 属性（2026-09-20 实测定论）。
    """
    for t in md.tables.values():
        try:
            t.name.quote = True
        except AttributeError:
            pass
        for c in t.columns:
            try:
                c.name.quote = True
            except AttributeError:
                pass
        for i in t.indexes:
            try:
                i.name.quote = True
            except AttributeError:
                pass


def diff_schemas(cloud_md, local_md, manifest: list[TableRule],
                 ignore_indexes: list[str] | None = None) -> SchemaDiffResult:
    """云库（期望）vs B 库（现状）→ 有序变更列表。"""
    _force_quote(cloud_md)
    _force_quote(local_md)
    result = SchemaDiffResult()
    ignore_idx = set(ignore_indexes or [])
    for rule in manifest:
        cloud_t = cloud_md.tables.get(rule.name)
        local_t = local_md.tables.get(rule.name)
        if cloud_t is None:
            continue  # 云库缺表由 G3 在更早阶段拦截
        if local_t is None:
            create_ddl = str(CreateTable(cloud_t).compile(dialect=_DIALECT))
            result.changes.append(SchemaChange(
                "add_table", rule.name, create_ddl,
                detail="含主键；二级索引随后单独建",
                down_ddl=f"DROP TABLE IF EXISTS {_quote(rule.name)};",
            ))
            for idx in sorted(cloud_t.indexes, key=lambda i: i.name or ""):
                result.changes.append(_index_change("add_index", rule.name, idx))
            continue
        _diff_table(result, rule, cloud_t, local_t, ignore_idx)
    # B 库多出的清单表 → 删除（破坏性；不在清单里的表不碰）
    manifest_names = {r.name for r in manifest}
    for name in sorted(manifest_names & set(local_md.tables.keys()) - set(cloud_md.tables.keys())):
        result.changes.append(SchemaChange(
            "drop_table", name,
            str(DropTable(local_md.tables[name]).compile(dialect=_DIALECT)),
            destructive=True,
            down_ddl=f"-- {name} 表数据恢复依赖备份回灌",
        ))
    return result


def _diff_table(result: SchemaDiffResult, rule: TableRule, cloud_t, local_t,
                ignore_idx: set[str]) -> None:
    ignored = set(rule.ignore_columns)
    # ---- 列 ----
    cloud_cols = {c.name: c for c in cloud_t.columns if c.name not in ignored}
    local_cols = {c.name: c for c in local_t.columns if c.name not in ignored}
    for name, col in cloud_cols.items():
        if name not in local_cols:
            result.changes.append(SchemaChange(
                "add_column", rule.name,
                f"ALTER TABLE {_quote(rule.name)} ADD COLUMN {_col_ddl(col)}",
                down_ddl=f"ALTER TABLE {_quote(rule.name)} DROP COLUMN {_quote(name)};",
            ))
        elif column_fingerprint(col) != column_fingerprint(local_cols[name]):
            _, c_type, _, c_def, _ = column_fingerprint(col)
            _, l_type, _, l_def, _ = column_fingerprint(local_cols[name])
            detail = []
            if c_type != l_type:
                detail.append(f"{l_type}→{c_type}")
            if col.nullable != local_cols[name].nullable:
                detail.append("可空性" if col.nullable else "非空")
            if c_def != l_def:
                detail.append(f"默认 {c_def!r}")
            result.changes.append(SchemaChange(
                "modify_column", rule.name,
                f"ALTER TABLE {_quote(rule.name)} MODIFY COLUMN {_col_ddl(col)}",
                detail="，".join(detail) or "指纹差异",
                down_ddl=f"ALTER TABLE {_quote(rule.name)} MODIFY COLUMN "
                         f"{_col_ddl(local_cols[name])};",
            ))
    for name in sorted(set(local_cols) - set(cloud_cols)):
        result.changes.append(SchemaChange(
            "drop_column", rule.name,
            f"ALTER TABLE {_quote(rule.name)} DROP COLUMN {_quote(name)}",
            destructive=True,
            detail="列数据恢复依赖备份",
            down_ddl=f"ALTER TABLE {_quote(rule.name)} ADD COLUMN "
                     f"{_col_ddl(local_cols[name])};",
        ))
    # ---- 二级索引 ----（含忽略列的索引跳过；ignore_idx 名单跳过）
    def _usable(side_t):
        out = {}
        for idx in side_t.indexes:
            cols = [c.name for c in idx.columns]
            if any(c in ignored for c in cols):
                continue
            if idx.name and any(idx.name.startswith(p.rstrip("*")) and p.endswith("*") or idx.name == p
                                for p in ignore_idx):
                continue
            out[index_fingerprint(idx)] = idx
        return out

    cloud_idx = _usable(cloud_t)
    local_idx = _usable(local_t)
    for fp, idx in cloud_idx.items():
        if fp not in local_idx:
            result.changes.append(_index_change("add_index", rule.name, idx))
    for fp, idx in local_idx.items():
        if fp not in cloud_idx:
            result.changes.append(_index_change("drop_index", rule.name, idx, destructive=True))
    # ---- 主键 ----
    cloud_pk = tuple(c.name for c in cloud_t.primary_key.columns)
    local_pk = tuple(c.name for c in local_t.primary_key.columns)
    if cloud_pk != local_pk:
        parts = []
        if local_pk:
            parts.append("DROP PRIMARY KEY")
        if cloud_pk:
            parts.append("ADD PRIMARY KEY (" + ", ".join(_quote(c) for c in cloud_pk) + ")")
        down_parts = []
        if cloud_pk:
            down_parts.append("DROP PRIMARY KEY")
        if local_pk:
            down_parts.append("ADD PRIMARY KEY (" + ", ".join(_quote(c) for c in local_pk) + ")")
        if parts:
            result.changes.append(SchemaChange(
                "change_pk", rule.name,
                f"ALTER TABLE {_quote(rule.name)} " + ", ".join(parts),
                destructive=True,
                detail=f"{local_pk or '无'}→{cloud_pk or '无'}",
                down_ddl=f"ALTER TABLE {_quote(rule.name)} " + ", ".join(down_parts) + ";",
            ))


def _index_change(kind: str, table: str, idx, destructive: bool = False) -> SchemaChange:
    if kind == "add_index":
        ddl = str(CreateIndex(idx).compile(dialect=_DIALECT))
        down = str(DropIndex(idx).compile(dialect=_DIALECT))
    else:
        ddl = str(DropIndex(idx).compile(dialect=_DIALECT))
        down = str(CreateIndex(idx).compile(dialect=_DIALECT))
    cols = ", ".join(c.name for c in idx.columns)
    return SchemaChange(kind, table, ddl + ";", destructive=destructive, detail=cols,
                        down_ddl=down + ";")
