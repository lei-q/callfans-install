"""执行前备份（G1，不可关闭）：受影响表 bak 表 + 本地 SQL dump 双保险。

- bak 表：`CREATE TABLE _cf_bak_<run8>_<table> LIKE <table>` + 全量 INSERT
  （run8 为 run_id 前 8 位，总名长不超 MySQL 64 字符限制）
- 本地 dump：`<backup_root>/<run_id>/<table>.sql`（SHOW CREATE TABLE + 批量 INSERT）
- 恢复：优先 RENAME bak 回原表（最快）；bak 丢失时回放本地 dump
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .sqlgen import sql_literal as _literal  # 兼容既有测试引用

_IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_BATCH = 500


class BackupError(RuntimeError):
    pass


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise BackupError(f"非法表名: {name!r}")
    return name


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


@dataclass
class BackupResult:
    run_id: str
    tables: dict[str, str]  # 原表 → bak 表
    dump_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return bool(self.tables)


class BackupManager:
    def __init__(self, conn_factory, backup_root: Path):
        """conn_factory: () -> pymysql 连接（服务器级，不选库）。"""
        self._conn_factory = conn_factory
        self._root = Path(backup_root)

    # ---------- 备份 ----------

    def backup(self, tables: list[tuple[str, str]] | list[str],
               run_id: str | None = None) -> BackupResult:
        """tables: [(db, table)] 或兼容旧式 ["table"]（不带库）。"""
        targets = [t if isinstance(t, tuple) else ("", t) for t in tables]
        run_id = run_id or new_run_id()
        result = BackupResult(run_id=run_id, tables={})
        dump_dir = self._root / run_id
        dump_dir.mkdir(parents=True, exist_ok=True)
        conn = self._conn_factory()
        try:
            for db, table in targets:
                t = _ident(table)
                d = _ident(db) if db else ""
                fq = f"`{d}`.`{t}`" if d else f"`{t}`"
                bak = f"_cf_bak_{run_id[:8]}_{t}"[:64]
                bak_fq = f"`{d}`.`{bak}`" if d else f"`{bak}`"
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {bak_fq}")
                    cur.execute(f"CREATE TABLE {bak_fq} LIKE {fq}")
                    cur.execute(f"INSERT INTO {bak_fq} SELECT * FROM {fq}")
                self._dump_table(conn, db, t, dump_dir / f"{(d + '_') if d else ''}{t}.sql")
                result.tables[t] = bak
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        result.dump_dir = dump_dir
        return result

    def _dump_table(self, conn, db: str, table: str, path: Path) -> None:
        fq = f"`{db}`.`{table}`" if db else f"`{table}`"
        rows: list[tuple] = []
        create_stmt = ""
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE {fq}")
            row = cur.fetchone()
            create_stmt = row[1] if row else ""
            cur.execute(f"SELECT * FROM {fq}")
            rows = cur.fetchall()
        lines = [f"-- backup of {fq}", create_stmt or "-- (no create stmt)", ""]
        for i in range(0, len(rows), _BATCH):
            batch = rows[i:i + _BATCH]
            values = ", ".join(
                "(" + ", ".join(_literal(v) for v in row) + ")" for row in batch
            )
            lines.append(f"INSERT INTO {fq} VALUES {values};")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------- 恢复 ----------

    def restore(self, table: str, result: BackupResult, db: str = "") -> None:
        """优先用 bak 表 RENAME 恢复；无 bak 则报错（走本地 dump 回放）。"""
        t = _ident(table)
        d = _ident(db) if db else ""
        fq = f"`{d}`.`{t}`" if d else f"`{t}`"
        conn = self._conn_factory()
        try:
            bak = result.tables.get(t)
            with conn.cursor() as cur:
                if bak:
                    bak_fq = f"`{d}`.`{bak}`" if d else f"`{bak}`"
                    cur.execute(f"DROP TABLE IF EXISTS {fq}")
                    cur.execute(f"RENAME TABLE {bak_fq} TO {fq}")
                else:
                    raise BackupError(f"无 bak 表可恢复 {fq}（run {result.run_id}）")
            conn.commit()
        finally:
            conn.close()
