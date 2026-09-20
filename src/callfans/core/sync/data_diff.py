"""配置表数据比对（M3）：PK 归并流式比对，内存有界（每侧一次一行）。

行流按 pk 升序；归并三分支：云有本地无 → INSERT；本地有云无 → DELETE；
都有 → 比较非忽略列 → UPDATE（全列覆盖，忽略列除外）。
每条变更同时生成逆操作（Down）：UPDATE 的 Down 用本地旧值回写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .sqlgen import sql_literal


@dataclass
class DataChange:
    table: str
    kind: str            # insert / update / delete
    up_sql: str
    down_sql: str
    pk: object = None


@dataclass
class TableDataDiff:
    table: str
    rule_pk: str | None
    changes: list[DataChange] = field(default_factory=list)
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    cloud_rows: int = 0
    local_rows: int = 0
    skipped_reason: str | None = None
    guard_notes: list[str] = field(default_factory=list)

    @property
    def row_changes(self) -> int:
        return self.inserts + self.updates + self.deletes


def _q(name: str) -> str:
    return f"`{name}`"


def diff_rows(table: str, columns: list[str], pk_col: str,
              cloud_rows: Iterable[tuple], local_rows: Iterable[tuple],
              ignore: set[str] | None = None) -> list[DataChange]:
    """纯函数：两条按 pk 升序的行流 → 变更列表（可单测，不碰数据库）。"""
    ignored = set(ignore or ())
    cmp_cols = [c for c in columns if c not in ignored]
    pk_idx = columns.index(pk_col)
    changes: list[DataChange] = []

    def _pk_val(row) -> object:
        return row[pk_idx]

    def _cmp(a: tuple, b: tuple) -> bool:
        """行内容是否一致（比较列）。"""
        idx = [columns.index(c) for c in cmp_cols]
        return all(a[i] == b[i] for i in idx)

    def _insert(row) -> None:
        cols = ", ".join(_q(c) for c in columns)
        vals = ", ".join(sql_literal(v) for v in row)
        pkv = _pk_val(row)
        changes.append(DataChange(
            table, "insert",
            f"INSERT INTO {_q(table)} ({cols}) VALUES ({vals});",
            f"DELETE FROM {_q(table)} WHERE {_q(pk_col)} = {sql_literal(pkv)};",
            pk=pkv,
        ))

    def _delete(row) -> None:
        cols = ", ".join(_q(c) for c in columns)
        vals = ", ".join(sql_literal(v) for v in row)
        pkv = _pk_val(row)
        changes.append(DataChange(
            table, "delete",
            f"DELETE FROM {_q(table)} WHERE {_q(pk_col)} = {sql_literal(pkv)};",
            f"INSERT INTO {_q(table)} ({cols}) VALUES ({vals});",
            pk=pkv,
        ))

    def _update(cloud_row, local_row) -> None:
        sets = ", ".join(
            f"{_q(c)} = {sql_literal(cloud_row[columns.index(c)])}"
            for c in cmp_cols if c != pk_col
        )
        back = ", ".join(
            f"{_q(c)} = {sql_literal(local_row[columns.index(c)])}"
            for c in cmp_cols if c != pk_col
        )
        pkv = _pk_val(cloud_row)
        where = f"WHERE {_q(pk_col)} = {sql_literal(pkv)}"
        changes.append(DataChange(
            table, "update",
            f"UPDATE {_q(table)} SET {sets} {where};",
            f"UPDATE {_q(table)} SET {back} {where};",
            pk=pkv,
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
            cpk, lpk = _pk_val(c), _pk_val(l)
            if cpk == lpk:
                if not _cmp(c, l):
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


def seed_inserts(table: str, columns: list[str],
                 cloud_rows: Iterable[tuple]) -> list[DataChange]:
    """新建表的种子数据（全量 INSERT；Down 为整表清空，依赖备份兜底）。"""
    return [
        DataChange(
            table, "insert",
            f"INSERT INTO {_q(table)} ("
            + ", ".join(_q(c) for c in columns)
            + ") VALUES (" + ", ".join(sql_literal(v) for v in row) + ");",
            f"DELETE FROM {_q(table)};  -- Down 为整表清空，数据恢复依赖备份",
            pk=row[0],
        )
        for row in cloud_rows
    ]
