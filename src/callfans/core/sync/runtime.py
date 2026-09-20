"""sqlsync 运行时门面：CLI 与 service 共用的编排入口。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...paths import state_file
from ..local import StateStore
from .backup import BackupManager
from .config import CloudSyncConfig, DbTarget
from .executor import SqlSyncExecutor, SyncReport
from .manifest import get_manifest
from .plan import SyncPlan, build_plan
from .reflect import engine_for

log = logging.getLogger(__name__)


def _artifacts_root() -> Path:
    root = state_file().parent / "sqlsync"
    root.mkdir(parents=True, exist_ok=True)
    return root


def pymysql_factory(target: DbTarget):
    """BackupManager 用的 pymysql 连接工厂（B 库）。"""
    def factory():
        import pymysql

        kwargs = {
            "host": target.host, "port": target.port,
            "user": target.user, "password": target.password,
            "database": target.database, "charset": "utf8mb4",
        }
        if target.ca:
            kwargs["ssl"] = {"ca": target.ca}
        return pymysql.connect(**kwargs)
    return factory


class SqlSyncRuntime:
    def __init__(self, cfg: CloudSyncConfig, cloud_engine=None, local_engine=None,
                 state: StateStore | None = None, on_event=None,
                 artifacts_root: Path | None = None, backup=None):
        self.cfg = cfg
        self._cloud = cloud_engine
        self._local = local_engine
        self.state = state or StateStore(state_file())
        self.on_event = on_event or (lambda *a, **k: None)
        self._artifacts = Path(artifacts_root) if artifacts_root else _artifacts_root()
        self._backup = backup  # 测试可注入；缺省按 B 库配置构造

    @property
    def cloud(self):
        if self._cloud is None:
            self._cloud = engine_for(self.cfg.cloud)
        return self._cloud

    @property
    def local(self):
        if self._local is None:
            self._local = engine_for(self.cfg.local)
        return self._local

    # ---------- 查询 ----------

    def manifest(self):
        return get_manifest(self.cloud, self._artifacts / "tables_cache.json")

    def build(self) -> SyncPlan:
        return build_plan(self.cfg, self.cloud, self.local, self.manifest())

    def status(self) -> dict:
        from sqlalchemy import text

        def _ping(engine, label):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return "OK"
            except Exception as e:
                return f"不可达: {type(e).__name__}"

        last = self.state.get("sqlsync") or {}
        try:
            plan = self.build()
            drift = {
                "statements": plan.statement_count,
                "tables": len(plan.affected_tables),
                "fatal_guard": plan.fatal,
                "checksum_cloud": plan.checksum_cloud,
            }
        except Exception as e:
            drift = {"error": f"{type(e).__name__}: {e}"}
        return {
            "cloud": _ping(self.cloud, "cloud"),
            "local": _ping(self.local, "local"),
            "last_run": last or None,
            "drift": drift,
        }

    # ---------- 执行 ----------

    def _save_artifacts(self, plan: SyncPlan) -> Path:
        run_dir = self._artifacts / plan.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "up.json").write_text(
            json.dumps(plan.up_statements(), ensure_ascii=False, indent=1), encoding="utf-8")
        (run_dir / "down.json").write_text(
            json.dumps(plan.down_statements(), ensure_ascii=False, indent=1), encoding="utf-8")
        (run_dir / "report.txt").write_text(plan.report_text(), encoding="utf-8")
        return run_dir

    def _executor(self) -> SqlSyncExecutor:
        if self._backup is not None:
            backup = self._backup
        else:
            backup_root = self._artifacts / "backup"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = BackupManager(pymysql_factory(self.cfg.local), backup_root)
        return SqlSyncExecutor(
            self.cfg, self.local, backup,
            state=self.state, on_event=self.on_event,
        )

    def apply(self, force: bool = False) -> SyncReport:
        plan = self.build()
        self._save_artifacts(plan)  # 先落盘，失败也可按 run_id 回滚
        return self._executor().apply(plan, force=force)

    def rollback(self, run_id: str) -> SyncReport:
        down_file = self._artifacts / run_id / "down.json"
        if not down_file.exists():
            raise FileNotFoundError(f"未找到 run {run_id} 的 Down 脚本（{down_file}）")
        statements = [s for s in json.loads(down_file.read_text(encoding="utf-8"))
                      if s.strip() and not s.strip().startswith("--")]
        executor = self._executor()
        self.on_event("sqlsync_progress", {"stage": "rollback", "total": len(statements)})
        report = SyncReport(run_id=run_id, plan_id=run_id, status="rolled_back",
                            total=len(statements))
        for i, stmt in enumerate(statements, 1):
            from sqlalchemy import text as _text

            with self.local.begin() as conn:
                conn.execute(_text(stmt))
            report.executed = i
        from datetime import datetime, timezone

        report.finished_at = datetime.now(timezone.utc).isoformat()
        # 复用执行器审计
        empty_plan = SyncPlan(run_id=run_id, created_at=report.finished_at,
                              schema_changes=[], data_tables=[],
                              guard_violations=[], checksum_cloud="")
        executor._record(empty_plan, report)
        return report
