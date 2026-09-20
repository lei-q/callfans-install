"""配置表数据比对（M3）：PK 归并比对 + Up/Down 生成。

行模型为 dict（列名 → 值），两侧列集可以不同：
- 比较：交集列 − 忽略列（新列的值在下一轮收敛，结构先行保证幂等收敛）
- 写入（INSERT/UPDATE SET）：云库全列 − 忽略列（执行时结构阶段已补齐列）
- DELETE 的 Down：本地全列（回滚时本地列集即执行前列集）
"""

from __future__ import annotations

from typing import Iterable

from .sqlgen import sql_literal


class DataChange:
    __slots__ = ("table", "kind", "up_sql", "down_sql", "pk", "db")

    def __init__(self, table: str, kind: str, up_sql: str, down_sql: str, pk=None, db=""):
        self.table = table
        self.kind = kind
        self.up_sql = up_sql
        self.down_sql = down_sql
        self.pk = pk
        self.db = db


class TableDataDiff:
    __slots__ = ("table", "rule_pk", "changes", "inserts", "updates", "deletes",
                 "cloud_rows", "local_rows", "skipped_reason", "db")

    def __init__(self, table, rule_pk=None, changes=None, inserts=0, updates=0,
                 deletes=0, cloud_rows=0, local_rows=0, skipped_reason=None, db=""):
        self.table = table
        self.rule_pk = rule_pk
        self.changes = changes or []
        self.inserts = inserts
        self.updates = updates
        self.deletes = deletes
        self.cloud_rows = cloud_rows
        self.local_rows = local_rows
        self.skipped_reason = skipped_reason
        self.db = db


def _q(name: str) -> str:
    return f"`{name}`"


def _tq(db: str, name: str) -> str:
    return f"`{db}`.`{name}`" if db else f"`{name}`"


def _cols_of(rows: Iterable[dict]) -> list[str]:
    for r in rows:
        return list(r.keys())
    return []


def diff_rows(table: str, pk_col: str, cloud_rows: list[dict], local_rows: list[dict],
              ignore: set[str] | None = None, db: str = "") -> list[DataChange]:
    """纯函数：两条按 pk 升序的 dict 行流 → 变更列表（不碰数据库）。"""
    ignored = set(ignore or ())
    cloud_cols = [c for c in _cols_of(cloud_rows) if c not in ignored]
    local_cols = [c for c in _cols_of(local_rows) if c not in ignored]
    local_set = set(local_cols)
    cmp_cols = [c for c in cloud_cols if c in local_set]  # 交集列（含 pk）
    write_cols = cloud_cols                               # 结构先行，执行时列已齐
    changes: list[DataChange] = []

    def _insert(row: dict) -> None:
        cols = ", ".join(_q(c) for c in write_cols)
        vals = ", ".join(sql_literal(row[c]) for c in write_cols)
        changes.append(DataChange(
            table, "insert",
            f"INSERT INTO {_tq(db, table)} ({cols}) VALUES ({vals});",
            f"DELETE FROM {_tq(db, table)} WHERE {_q(pk_col)} = {sql_literal(row[pk_col])};",
            pk=row[pk_col], db=db,
        ))

    def _delete(row: dict) -> None:
        cols = ", ".join(_q(c) for c in local_cols)
        vals = ", ".join(sql_literal(row[c]) for c in local_cols)
        changes.append(DataChange(
            table, "delete",
            f"DELETE FROM {_tq(db, table)} WHERE {_q(pk_col)} = {sql_literal(row[pk_col])};",
            f"INSERT INTO {_tq(db, table)} ({cols}) VALUES ({vals});",
            pk=row[pk_col], db=db,
        ))

    def _update(c_row: dict, l_row: dict) -> None:
        sets = ", ".join(
            f"{_q(c)} = {sql_literal(c_row[c])}" for c in write_cols if c != pk_col
        )
        back = ", ".join(
            f"{_q(c)} = {sql_literal(l_row[c])}" for c in local_cols if c != pk_col
        )
        where = f"WHERE {_q(pk_col)} = {sql_literal(c_row[pk_col])}"
        changes.append(DataChange(
            table, "update",
            f"UPDATE {_tq(db, table)} SET {sets} {where};",
            f"UPDATE {_tq(db, table)} SET {back} {where};",
            pk=c_row[pk_col], db=db,
        ))

    ci, li = iter(cloud_rows), iter(local_rows)
    c = next(ci, None)
    l = next(li, None)
    while c is not None or l is not None:
        if c is None:
            _delete(l)
            l = next(li, None)
        elif l is None:
            _insert(c)
            c = next(ci, None)
        else:
            cpk, lpk = c[pk_col], l[pk_col]
            if cpk == lpk:
                if any(c[col] != l[col] for col in cmp_cols if col != pk_col):
                    _update(c, l)
                c = next(ci, None)
                l = next(li, None)
            elif cpk < lpk:
                _insert(c)
                c = next(ci, None)
            else:
                _delete(l)
                l = next(li, None)
    return changes


def seed_inserts(table: str, cloud_rows: list[dict], db: str = "") -> list[DataChange]:
    """新建表的种子数据（全量 INSERT；Down 为整表清空，数据恢复依赖备份）。"""
    cols = _cols_of(cloud_rows)
    out = []
    for row in cloud_rows:
        col_sql = ", ".join(_q(c) for c in cols)
        vals = ", ".join(sql_literal(row[c]) for c in cols)
        out.append(DataChange(
            table, "insert",
            f"INSERT INTO {_tq(db, table)} ({col_sql}) VALUES ({vals});",
            f"DELETE FROM {_tq(db, table)};  -- Down 为整表清空，数据恢复依赖备份",
            pk=row.get(cols[0]) if cols else None, db=db,
        ))
    return out
