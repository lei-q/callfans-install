"""SQL 执行器（§6.3）：oras 拉取 → 归档解包（zip/tar.gz/gz，2026-09-18 实测为打包制品）
→ 按 py 序执行全部 .sql（每文件独立事务）→ state 记账。

- 积压多版本按 push_time 升序逐个执行（runner 保证传入顺序，本执行器按列表顺序）
- 单文件单事务；失败回滚当前文件；含 DDL 时 DDL 隐式提交无法回滚 → 记录断点人工介入（Q10）
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ...config import Config
from ..local import StateStore
from ..models import PendingItem
from .archive import extract_archive, is_archive
from .artifact_puller import ArtifactPuller

log = logging.getLogger(__name__)


class SqlError(RuntimeError):
    pass


def mysql_connect_from_cfg(cfg: Config):
    """按 .env 配置建 MySQL 连接（utf8mb4、手动事务）。

    database 可选不指定——sql 文件内通过 USE 语句自行选库（2026-09-18 决策）。
    """
    import pymysql

    kwargs: dict = {
        "host": cfg.mysql_host,
        "port": cfg.mysql_port,
        "user": cfg.mysql_user,
        "password": cfg.mysql_password,
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if cfg.mysql_database:
        kwargs["database"] = cfg.mysql_database
    return pymysql.connect(**kwargs)


def split_sql(text: str) -> list[str]:
    """按分号切分 SQL 语句；正确处理单双引号、反引号、转义与三类注释。"""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    in_single = in_double = in_backtick = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # 注释
        if not (in_single or in_double or in_backtick):
            if ch == "-" and nxt == "-":
                end = text.find("\n", i)
                if end == -1:
                    break  # 注释直达文件尾
                buf.append("\n")
                i = end + 1
                continue
            if ch == "#":
                i = text.find("\n", i)
                if i == -1:
                    break
                buf.append("\n")
                i += 1
                continue
            if ch == "/" and nxt == "*":
                end = text.find("*/", i + 2)
                if end == -1:
                    break
                i = end + 2
                buf.append(" ")
                continue
        buf.append(ch)
        # 引号状态
        if ch == "'" and not in_double and not in_backtick:
            # '' 转义
            if in_single and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            in_single = not in_single
        elif ch == '"' and not in_single and not in_backtick:
            if in_double and nxt == '"':
                buf.append(nxt)
                i += 2
                continue
            in_double = not in_double
        elif ch == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
        elif ch == ";" and not (in_single or in_double or in_backtick):
            stmt = "".join(buf[:-1]).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class SqlUpdater:
    def __init__(self, cfg: Config, state: StateStore | None = None,
                 puller: ArtifactPuller | None = None, on_event=None, mysql_connect=None):
        self.cfg = cfg
        self.state = state
        self.puller = puller
        self.on_event = on_event or (lambda *a, **k: None)
        self._connect = mysql_connect

    def _default_connect(self):
        return mysql_connect_from_cfg(self.cfg)

    def update(self, item: PendingItem) -> dict:
        record = {
            "name": item.name, "type": "sql", "old": item.old, "new": item.new,
            "result": "failed", "error": None, "executed": [],
        }
        tags = item.new if isinstance(item.new, list) else [item.new]
        missing = [k for k in ("mysql_host", "mysql_user", "mysql_password")
                   if not getattr(self.cfg, k)]  # mysql_database 可选（sql 内 USE 指定）
        if missing:
            record["error"] = f"MySQL 配置缺失: {', '.join(missing)}"
            return record
        puller = self.puller or ArtifactPuller(
            self.cfg.harbor_api_url, self.cfg.harbor_username, self.cfg.harbor_password
        )
        connect = self._connect or self._default_connect
        executed: list[str] = []
        for tag in tags:
            self.on_event("update_progress", {"item": item.name, "stage": "sql_pull", "tag": tag})
            tmpdir = Path(tempfile.mkdtemp(prefix="callfans-sql-"))
            try:
                files = puller.pull(item.name, tag, tmpdir)
                sql_files = self._collect_sql_files(files, tmpdir / "extracted")
                self.on_event("update_progress", {
                    "item": item.name, "stage": "sql_exec", "tag": tag,
                    "files": [f.name for f in sql_files],
                })
                conn = connect()
                try:
                    for sql_file in sql_files:
                        statements = split_sql(sql_file.read_text(encoding="utf-8"))
                        if not statements:
                            raise SqlError(f"{tag}/{sql_file.name}: SQL 文件为空")
                        with conn.cursor() as cursor:
                            for idx, stmt in enumerate(statements, 1):
                                try:
                                    cursor.execute(stmt)
                                except Exception as e:
                                    conn.rollback()
                                    raise SqlError(
                                        f"{tag}/{sql_file.name}: 第 {idx}/{len(statements)} 条语句失败: {e}"
                                        "（若含 DDL，已执行部分无法回滚，请人工确认）"
                                    ) from e
                        conn.commit()
                finally:
                    conn.close()
                executed.append(tag)
                self._record(item.name, tag)
            except Exception as e:
                record["executed"] = executed
                record["error"] = f"{type(e).__name__}: {e}"
                return record
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        record.update(result="success", executed=executed)
        return record

    @staticmethod
    def _collect_sql_files(files: list[Path], workdir: Path) -> list[Path]:
        """裸 .sql 直接用；归档（zip/tar.gz/gz）解包后取全部 .sql，按路径升序依次执行。"""
        archives = [f for f in files if is_archive(f)]
        if archives:
            extract_archive(archives[0], workdir)
            sqls = sorted(
                workdir.rglob("*.sql"),
                key=lambda p: str(p.relative_to(workdir)).lower(),
            )
            if not sqls:
                raise SqlError(f"压缩包内未找到 .sql 文件: {[f.name for f in files]}")
            return sqls
        plain = sorted((f for f in files if f.suffix.lower() == ".sql"),
                       key=lambda p: p.name.lower())
        if plain:
            return plain
        if len(files) == 1:
            return [files[0]]  # 无后缀单文件兜底
        raise SqlError(f"制品中未找到可执行的 .sql: {[f.name for f in files]}")

    def _record(self, repo: str, tag: str) -> None:
        """每个版本执行成功立即记账（防中断后重跑）。"""
        if self.state is None:
            return
        data = self.state.get("sql") or {}
        entry = data.setdefault(repo, {"applied": []})
        if not any(a.get("tag") == tag for a in entry["applied"]):
            entry["applied"].append({
                "tag": tag,
                "digest": None,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            })
        self.state.set("sql", data)
        self.state.save()
