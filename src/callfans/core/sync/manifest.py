"""云端元表（Q3）：配置表清单读取 + 本地缓存兜底。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from .config import TableRule

_MANIFEST_DB = "callfans_sync"
_MANIFEST_TABLE = "tables"
_MANIFEST_COLS = ("name", "data_sync", "pk", "ignore_columns")


class ManifestError(RuntimeError):
    pass


def fetch_manifest(cloud_engine) -> list[TableRule]:
    """从云库 callfans_sync.tables 读取表清单。

    兼容列不齐的旧元表：探测实际存在的列（name 必需），缺省列取默认值
    （2026-09-21 实测：客户云库元表少 ignore_columns 列导致整体不可读）。
    """
    try:
        with cloud_engine.connect() as conn:
            cols = [str(r[0]) for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{_MANIFEST_DB}' "
                f"AND table_name = '{_MANIFEST_TABLE}'"
            ))]
            if "name" not in cols:
                raise ManifestError(
                    "云端元表 callfans_sync.tables 不存在或缺 name 列，"
                    "需在云库执行建表 DDL（见 core/sync/config.py 模块注释）"
                )
            select_cols = ", ".join(f"`{c}`" for c in _MANIFEST_COLS if c in cols)
            rows = [dict(r) for r in conn.execute(text(
                f"SELECT {select_cols} FROM {_MANIFEST_DB}.{_MANIFEST_TABLE} ORDER BY name"
            )).mappings()]
    except ManifestError:
        raise
    except Exception as e:
        raise ManifestError(
            f"云端元表不可读: {e}\n"
            "需在云库执行建表 DDL（见 core/sync/config.py 模块注释）"
        ) from e
    rules = [TableRule.from_manifest_row(r) for r in rows]
    if not rules:
        raise ManifestError("云端元表为空（callfans_sync.tables 无配置行）")
    return rules


def save_cache(path: Path, rules: list[TableRule]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [{"name": r.name, "data": r.data, "pk": r.pk,
             "ignore_columns": r.ignore_columns} for r in rules]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_cache(path: Path) -> list[TableRule] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [TableRule(**row) for row in data]
    except (OSError, ValueError, TypeError):
        return None


def get_manifest(cloud_engine, cache_path: Path) -> list[TableRule]:
    """优先云端；不可达/未建表时回退本地缓存（云库故障也不挡 diff 之外的流程）。"""
    try:
        rules = fetch_manifest(cloud_engine)
        try:
            save_cache(cache_path, rules)
        except OSError:
            pass
        return rules
    except ManifestError:
        cached = load_cache(cache_path)
        if cached:
            return cached
        raise
