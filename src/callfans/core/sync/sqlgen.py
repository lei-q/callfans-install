"""SQL 字面量渲染（dump / 数据比对共用）。"""

from __future__ import annotations

from datetime import date, datetime


def sql_literal(v) -> str:
    """保守转义的 SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    if isinstance(v, (datetime, date)):
        return f"'{v.isoformat(sep=' ')}'"
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    return f"'{s}'"
