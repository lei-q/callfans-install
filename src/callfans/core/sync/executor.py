"""同步执行器（M4）：护栏执法 + G1 强制备份 + 逐语句执行 + 审计。

断点/幂等说明：diff 式同步天然幂等——中断后重跑 plan+apply 自动收敛
（已执行语句在新计划中不再出现）。执行器记录逐条进度用于审计与失败
定位；失败后恢复二选一：rollback（Down + 备份）或直接重跑（收敛）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from ...paths import history_file as default_history_file
from .. import history as history_mod
from ..local import StateStore
from .backup import BackupManager, BackupResult, new_run_id
from .guards import summarize
from .plan import SyncPlan

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    run_id: str
    plan_id: str
    status: str            # success / aborted_by_guard / failed / rolled_back / backup_failed
    executed: int = 0
    total: int = 0
    error: str | None = None
    guard_summary: str | None = None
    backup_tables: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "plan_id": self.plan_id, "status": self.status,
            "executed": self.executed, "total": self.total, "error": self.error,
            "backup_tables": self.backup_tables,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlSyncExecutor:
    def __init__(self, cfg, local_engine, backup: BackupManager,
                 state: StateStore | None = None,
                 history_path=None, on_event=None):
        self.cfg = cfg
        self.local_engine = local_engine
        self.backup = backup
        self.state = state
        self.history_path = history_path or default_history_file()
        self.on_event = on_event or (lambda *a, **k: None)

    # ---------- apply ----------

    def apply(self, plan: SyncPlan, force: bool = False) -> SyncReport:
        report = SyncReport(
            run_id=new_run_id(), plan_id=plan.run_id,
            status="failed", total=plan.statement_count, started_at=_now(),
        )
        if plan.guard_violations:
            report.guard_summary = summarize(plan.guard_violations)
        if plan.fatal and not force:
            report.status = "aborted_by_guard"
            report.error = f"护栏拦截（--force 可越过，行为全量审计）:\n{report.guard_summary}"
            self._record(plan, report)
            return report
        if plan.statement_count == 0:
            report.status = "success"
            report.finished_at = _now()
            self._record(plan, report)
            return report

        # G1：备份失败绝不执行（不可关）
        self.on_event("sqlsync_progress", {"stage": "backup", "tables": sorted(plan.affected_tables)})
        try:
            backup_result = self.backup.backup(sorted(plan.affected_tables), run_id=report.run_id)
            report.backup_tables = backup_result.tables
        except Exception as e:
            report.status = "backup_failed"
            report.error = f"备份失败，未执行任何语句: {e}"
            report.finished_at = _now()
            self._record(plan, report)
            return report

        statements = plan.up_statements()
        report.total = len(statements)
        for i, stmt in enumerate(statements, 1):
            self.on_event("sqlsync_progress", {
                "stage": "execute", "index": i, "total": len(statements),
                "sql": stmt[:120],
            })
            try:
                with self.local_engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception as e:
                report.executed = i - 1
                report.error = (
                    f"第 {i}/{len(statements)} 条失败: {stmt[:120]} — {type(e).__name__}: {e}"
                    "（恢复：rollback 回滚，或直接重跑 plan+apply 自动收敛）"
                )
                report.finished_at = _now()
                self._record(plan, report)
                return report
        report.executed = len(statements)
        report.status = "success"
        report.finished_at = _now()
        self._record(plan, report)
        return report

    # ---------- rollback ----------

    def rollback(self, plan: SyncPlan, report: SyncReport) -> SyncReport:
        """执行 Down（跳过注释行；备份依赖项已在语句注释中标注）。"""
        down = [s for s in plan.down_statements()
                if s.strip() and not s.strip().startswith("--")]
        rb = SyncReport(
            run_id=new_run_id(), plan_id=plan.run_id,
            status="rolled_back", total=len(down), started_at=_now(),
            backup_tables=report.backup_tables,
        )
        for i, stmt in enumerate(down, 1):
            self.on_event("sqlsync_progress", {
                "stage": "rollback", "index": i, "total": len(down), "sql": stmt[:120],
            })
            try:
                with self.local_engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception as e:
                rb.executed = i - 1
                rb.error = f"回滚第 {i}/{len(down)} 条失败: {stmt[:120]} — {e}（可用备份表手工恢复）"
                rb.finished_at = _now()
                self._record(plan, rb)
                return rb
        rb.executed = len(down)
        rb.finished_at = _now()
        self._record(plan, rb)
        return rb

    # ---------- 审计 ----------

    def _record(self, plan: SyncPlan, report: SyncReport) -> None:
        try:
            history_mod.append_record(self.history_path, {
                "domain": "sqlsync", "status": report.status,
                "run_id": report.run_id, "plan_id": plan.run_id,
                "executed": report.executed, "total": report.total,
                "error": report.error, "checksum_cloud": plan.checksum_cloud,
                "backup_tables": report.backup_tables,
            })
        except OSError as e:
            log.error("sqlsync 历史写入失败: %s", e)
        if self.state is not None:
            try:
                self.state.set("sqlsync", {
                    "last_run_id": report.run_id,
                    "last_status": report.status,
                    "last_plan_id": plan.run_id,
                    "checksum_cloud": plan.checksum_cloud,
                    "executed": report.executed,
                    "total": report.total,
                    "error": report.error,
                    "finished_at": report.finished_at or _now(),
                })
                self.state.save()
            except OSError as e:
                log.error("state.json sqlsync 段写入失败: %s", e)
