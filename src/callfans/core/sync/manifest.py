"""云端元表（Q3）：配置表清单读取 + 本地缓存兜底。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from .config import TableRule

_MANIFEST_SQL = (
    "SELECT name, data_sync, pk, ignore_columns FROM callfans_sync.tables ORDER BY name"
)


class ManifestError(RuntimeError):
    pass


def fetch_manifest(cloud_engine) -> list[TableRule]:
    """从云库 callfans_sync.tables 读取表清单。"""
    try:
        with cloud_engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(text(_MANIFEST_SQL)).mappings()]
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
