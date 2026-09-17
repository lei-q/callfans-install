"""更新编排（§6）：preflight → SQL → server → 前端（Q11），逐项串行、单项失败不阻断，
结果逐条写 update_history.jsonl 并通过 on_event 推进度。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...config import Config
from ...paths import history_file
from .. import history
from ..models import TYPE_FRONTEND, TYPE_SERVER, TYPE_SQL, PendingItem, UpdatePlan
from .frontend_updater import FrontendUpdater
from .preflight import PreflightError, preflight
from .server_updater import ServerUpdater
from .sql_updater import SqlUpdater

log = logging.getLogger(__name__)

TYPE_ORDER = {TYPE_SQL: 0, TYPE_SERVER: 1, TYPE_FRONTEND: 2}


class UpdateRunner:
    def __init__(self, cfg: Config, state=None, docker=None, puller=None,
                 on_event=None, mysql_connect=None, updaters: dict | None = None,
                 history_path=None, run_preflight: bool = True):
        from .docker_cli import DockerCLI

        self.cfg = cfg
        self.state = state
        self.docker = docker or DockerCLI()
        self.puller = puller
        self.on_event = on_event or (lambda *a, **k: None)
        self.mysql_connect = mysql_connect
        self._updaters = updaters  # 测试注入 {type: 实例}
        self.history_path = history_path or history_file()
        self.run_preflight = run_preflight

    def _updater_for(self, type_: str):
        if self._updaters is not None:
            return self._updaters[type_]
        if type_ == TYPE_SQL:
            return SqlUpdater(self.cfg, self.state, self.puller, self.on_event, self.mysql_connect)
        if type_ == TYPE_SERVER:
            return ServerUpdater(self.cfg, self.state, self.docker, self.on_event)
        if type_ == TYPE_FRONTEND:
            return FrontendUpdater(self.cfg, self.state, self.puller, self.on_event)
        raise ValueError(f"未知制品类型: {type_}")

    def run(self, plan: UpdatePlan) -> dict:
        items = sorted(plan.pending, key=lambda p: (TYPE_ORDER.get(p.type, 99), p.name))
        report: dict = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "preflight_error": None,
            "items": [],
            "summary": {"success": 0, "failed": 0, "rolled_back": 0},
        }
        if not items:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            return report
        if self.run_preflight:
            try:
                preflight(self.cfg, plan, docker=self.docker, mysql_connect=self.mysql_connect)
            except PreflightError as e:
                report["preflight_error"] = str(e)
                log.error("preflight 失败，未执行任何更新: %s", e)
                self.on_event("update_done", report)
                return report

        self.on_event("update_begin", {
            "total": len(items),
            "items": [{"name": i.name, "type": i.type} for i in items],
        })
        if TYPE_SERVER in {p.type for p in items}:
            try:  # tag 变量不手写：缺失的按本地当前版本补齐（失败不阻断更新）
                from .server_updater import backfill_tag_vars

                filled = backfill_tag_vars(self.cfg)
                if filled:
                    self.on_event("update_progress", {
                        "item": "(compose)", "stage": "tag_backfill", "vars": filled,
                    })
            except Exception:
                log.exception("tag 变量回填失败（不影响更新）")
        for item in items:
            self.on_event("update_progress", {"item": item.name, "type": item.type, "stage": "start"})
            try:
                record = self._updater_for(item.type).update(item)
            except Exception as e:  # 执行器自身异常兜底，不让单项炸掉整批
                log.exception("%s 更新执行器异常", item.name)
                record = {
                    "name": item.name, "type": item.type, "old": item.old, "new": item.new,
                    "result": "failed", "error": f"{type(e).__name__}: {e}",
                }
            result = record.get("result") or "failed"
            report["items"].append(record)
            report["summary"][result] = report["summary"].get(result, 0) + 1
            try:
                history.append_record(self.history_path, record)
            except OSError as e:
                log.error("更新历史写入失败: %s", e)
            self.on_event("update_progress", {"item": item.name, "stage": "done", "record": record})

        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.on_event("update_done", report)
        log.info("更新完成: %s", report["summary"])
        return report
