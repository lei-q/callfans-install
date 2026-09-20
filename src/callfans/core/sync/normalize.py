"""MySQL 元数据归一化（M1）：diff 保真的关键层。

处理反射/alembic 输出的 MySQL 噪音（Q1：均 8.0，但信息来源仍可能带差异）：
- 整数族显示宽度剥离：int(11) == int，bigint(20) == bigint（unsigned/zerofill 保留为显著差异）
- 类型串小写归一、多余空白折叠
- datetime(6)/decimal(10,2)/varchar(32) 等精度保留（显著）
- 列指纹：名称 + 归一类型 + 可空 + 默认值语义（None 与 '' 区分，'NULL' 与 None 等价）
- 索引指纹：列序 + 前缀长度；主键单列时与 UNIQUE 语义区分
"""

from __future__ import annotations

import re

_INT_RE = re.compile(r"^(tinyint|smallint|mediumint|int|integer|bigint)\b(.*)$", re.I)
_WIDTH_RE = re.compile(r"^\s*\(\s*\d+\s*\)\s*")
_WS_RE = re.compile(r"\s+")


def normalize_type(sql_type: str) -> str:
    """归一化类型串：整数族统一名称并剥显示宽度（有无括号都要处理），其余小写归一。"""
    t = _WS_RE.sub(" ", (sql_type or "").strip()).lower()
    m = _INT_RE.match(t)
    if m:
        family = m.group(1).lower()
        family = "int" if family == "integer" else family
        rest = _WIDTH_RE.sub("", m.group(2))  # 剥 (11) 显示宽度，保留 unsigned/zerofill
        return _WS_RE.sub(" ", f"{family} {rest}").strip()
    return t


def normalize_default(default) -> str | None:
    """默认值语义归一：None/'NULL' 等价；字符串去引号后比较；数字去尾零。"""
    if default is None:
        return None
    s = str(default).strip()
    if s == "" or s.upper() == "NULL":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except ValueError:
        return s.lower()


def mysql_type_str(col_type) -> str:
    """按 MySQL 方言编译类型串（str(type) 会丢 unsigned 等方言细节）。"""
    from sqlalchemy.dialects import mysql

    try:
        return str(col_type.compile(mysql.dialect()))
    except Exception:
        return str(col_type)


def column_fingerprint(col) -> tuple:
    """SQLAlchemy Column → 可比较指纹（含列注释——2026-09-20 实测盲区补齐）。"""
    return (
        col.name.lower(),
        normalize_type(mysql_type_str(col.type)),
        bool(col.nullable),
        normalize_default(
            str(col.server_default.arg) if col.server_default is not None
            and hasattr(col.server_default, "arg") else None
        ),
        bool(col.autoincrement is True),
        (col.comment or "").strip() or None,
    )


def index_fingerprint(idx) -> tuple:
    """SQLAlchemy Index/PrimaryKeyConstraint → (列名列表小写, unique)。"""
    try:
        cols = tuple(c.name.lower() for c in idx.columns)
    except AttributeError:  # PrimaryKeyConstraint.columns 兼容
        cols = tuple(str(c).lower() for c in idx.columns)
    unique = bool(getattr(idx, "unique", True))
    return cols, unique


def table_fingerprint(table) -> dict:
    """SQLAlchemy Table → 规范指纹（结构比对的输入）。"""
    columns = {name: column_fingerprint(c) for name, c in table.columns.items()}
    indexes = sorted(
        (cols, unique) for cols, unique in (index_fingerprint(i) for i in table.indexes)
    )
    pk = tuple(c.name.lower() for c in table.primary_key.columns)
    return {"columns": columns, "indexes": indexes, "pk": pk}
