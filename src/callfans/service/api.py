"""IPC API（仅绑定 127.0.0.1，Bearer token 鉴权）。

端点见 docs/implementation-plan.md §5/§8：
- POST /api/v1/check        立即检查
- GET  /api/v1/pending      最近一次检查结果
- GET  /api/v1/status       服务状态
- GET  /api/v1/logs/tail    尾部日志
- WS   /api/v1/events       事件推送（check_done / update_progress…）
- POST /api/v1/update       M2 实现
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anyio
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import __version__
from ..config import Config
from ..core.checker import Checker
from ..core.harbor import HarborClient
from ..core.local import StateStore
from ..core.models import UpdatePlan
from ..core.sync.config import CloudSyncConfig, SyncConfigError
from ..core.sync.runtime import SqlSyncRuntime
from ..core.updaters.runner import UpdateRunner
from ..paths import log_file, runtime_file, state_file
from .hub import EventHub
from .runtime import clear_runtime, write_runtime
from .scheduler import start_scheduler

log = logging.getLogger(__name__)


@dataclass
class CheckState:
    busy: bool = False
    updating: bool = False
    plan: UpdatePlan | None = None
    last_check: datetime | None = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """/api/* 校验 Bearer token；WS 端点在自身内用 query 参数校验。"""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/v1/events":
            auth = request.headers.get("Authorization", "")
            expected = f"Bearer {self._token}"
            if not secrets.compare_digest(auth, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


def _tail_lines(path, n: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def create_app(
    cfg: Config,
    checker: Checker | None = None,
    token: str | None = None,
    enable_scheduler: bool = True,
    write_runtime_file: bool = False,
    port: int | None = None,
    sqlsync_cfg="auto",
) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    hub = EventHub()
    state = CheckState()
    main_loop: asyncio.AbstractEventLoop | None = None

    # sqlsync 可选域：未配置 CLOUD_DB_* 时整体停用（不影响制品更新）
    if sqlsync_cfg == "auto":
        try:
            sqlsync_cfg = CloudSyncConfig.from_env()
        except SyncConfigError:
            sqlsync_cfg = None
            log.info("sqlsync 未配置（缺 CLOUD_DB_*），SQL 同步域停用")

    def emit(event: str, data: dict) -> None:
        """工作线程 → 主事件循环的 WS 广播桥。"""
        if main_loop is not None:
            asyncio.run_coroutine_threadsafe(hub.broadcast({"event": event, "data": data}), main_loop)

    async def do_check() -> dict | None:
        """串行执行检查；busy 或更新中返回 None（检查与更新互斥）。"""
        if state.busy or state.updating:
            return None
        state.busy = True
        try:
            plan = await anyio.to_thread.run_sync(checker.run)
            # sqlsync 漂移并入检查结果（只读 plan，2026-09-21 补：此前检查按钮
            # 对数据库变更无感，漂移只在定时任务执行时才被感知）。
            # 评估失败同样可见（伪条目），不静默吞掉。
            if sqlsync_cfg is not None:
                try:
                    drift = await anyio.to_thread.run_sync(_sqlsync_drift_item)
                    if drift is not None:
                        plan.pending.append(drift)
                except Exception as e:
                    log.warning("sqlsync 漂移评估失败（不影响制品检查）: %s", e)
                    from ..core.models import PendingItem

                    plan.pending.append(PendingItem(
                        name="(sqlsync)", type="sqlsync", old=None,
                        new="漂移评估失败", changelog=f"{type(e).__name__}: {e}",
                    ))
            state.plan = plan
            state.last_check = datetime.now(timezone.utc)
            state.error = None
            payload = plan.to_dict()
            await hub.broadcast({"event": "check_done", "data": payload})
            log.info("检查完成: %d 项待更新", len(plan.pending))
            return payload
        except Exception as e:
            state.error = f"{type(e).__name__}: {e}"
            log.exception("检查失败")
            raise
        finally:
            state.busy = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal checker, main_loop
        main_loop = asyncio.get_running_loop()
        if checker is None:
            from ..core.local import StateStore

            checker = Checker(cfg, HarborClient(
                cfg.harbor_api_url, cfg.harbor_project, cfg.harbor_username, cfg.harbor_password
            ), state=StateStore(state_file()))
        # 服务重启后恢复上次的检查结果
        checker_state = getattr(checker, "state", None)
        if state.plan is None and checker_state is not None and checker_state.last_plan:
            try:
                state.plan = UpdatePlan.from_dict(checker_state.last_plan)
                if checker_state.checked_at:
                    state.last_check = datetime.fromisoformat(checker_state.checked_at)
            except (ValueError, KeyError):
                pass
        task = None
        sqlsync_task = None
        if enable_scheduler:
            task = start_scheduler(cfg, state, do_check)
        # 定时自动执行 SQL 同步：默认关闭（2026-09-21 决策变更——SQL 仅在
        # "立即更新"时执行；SQLSYNC_AUTO_APPLY=1 恢复全自动）
        if enable_scheduler and sqlsync_cfg is not None and sqlsync_cfg.policy.auto_apply:
            async def _sqlsync_loop():
                await asyncio.sleep(60)  # 启动缓冲
                while True:
                    try:
                        await anyio.to_thread.run_sync(lambda: _run_sqlsync_once())
                    except Exception:
                        log.exception("定时 sqlsync 失败")
                    await asyncio.sleep(sqlsync_cfg.interval_hours * 3600)

            sqlsync_task = asyncio.create_task(_sqlsync_loop())
        if write_runtime_file and port is not None:
            write_runtime(runtime_file(), port, token, os.getpid())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if sqlsync_task is not None:
                sqlsync_task.cancel()
            if write_runtime_file:
                clear_runtime(runtime_file())

    app = FastAPI(title="callfans service", version=__version__, lifespan=lifespan)
    app.add_middleware(TokenAuthMiddleware, token=token)
    # 供测试与诊断读取
    app.state.token = token
    app.state.hub = hub

    @app.get("/api/v1/status")
    async def status() -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": state.started_at,
            "last_check": state.last_check.isoformat() if state.last_check else None,
            "busy": state.busy,
            "updating": state.updating,
            "pending_count": len(state.plan.pending) if state.plan else 0,
            "error": state.error,
            "sqlsync": StateStore(state_file()).get("sqlsync"),
        }

    @app.post("/api/v1/check")
    async def check() -> dict:
        if state.busy:
            raise HTTPException(status_code=409, detail="check in progress")
        try:
            payload = await do_check()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        if payload is None:
            raise HTTPException(status_code=409, detail="check in progress")
        return payload

    @app.get("/api/v1/pending")
    async def pending() -> dict:
        if state.plan is not None:
            return state.plan.to_dict()
        return {"checked_at": None, "pending": []}

    @app.get("/api/v1/logs/tail")
    async def logs_tail(n: int = Query(200, ge=1, le=5000)) -> dict:
        return {"lines": _tail_lines(log_file(), n)}

    @app.post("/api/v1/update")
    async def update(payload: dict | None = Body(default=None)) -> dict:
        if state.updating or state.busy:
            raise HTTPException(status_code=409, detail="check or update in progress")
        selected = None
        if payload and isinstance(payload.get("items"), list):
            selected = set(payload["items"])  # 按名称勾选更新（None=全部）
        state.updating = True
        try:
            report = await anyio.to_thread.run_sync(_run_update_sync, selected)
            return report
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            state.updating = False

    def _sqlsync_drift_item():
        """只读评估数据库漂移；无差异返回 None，有则返回待更新伪条目（含变更 SQL）。"""
        from ..core.models import PendingItem

        runtime = SqlSyncRuntime(sqlsync_cfg)
        plan = runtime.build()
        n = plan.statement_count
        if n == 0:
            return None
        return PendingItem(
            name="(sqlsync)", type="sqlsync", old=None,
            new=f"{n} 条数据库变更", changelog=plan.report_text(),
            sql=plan.up_statements(),
        )

    def _sqlsync_item(result: str, error, status: str = "", run_id: str = "") -> dict:
        return {
            "name": "(sqlsync)", "type": "sqlsync", "old": None, "new": None,
            "result": result, "error": error, "status": status, "run_id": run_id,
        }

    def _run_sqlsync_once() -> dict:
        """定时/手动共用的 SQL 同步执行（护栏触发返回 aborted 状态）。"""
        if state.updating or state.busy:
            log.info("更新/检查进行中，跳过本轮定时 sqlsync（互斥）")
            return None
        runtime = SqlSyncRuntime(sqlsync_cfg, on_event=emit)
        report = runtime.apply()
        emit("sqlsync_done", {
            "status": report.status, "run_id": report.run_id,
            "error": report.error, "guard": report.guard_summary,
            "executed": report.executed, "total": report.total,
        })
        return report

    def _run_update_sync(selected=None) -> dict:
        """更新链路：sqlsync → server → 前端（Q9 后 SQL 由同步域承担）。

        selected：勾选的条目名称集合；None = 全部。sqlsync 伪条目名 "(sqlsync)"。
        """
        items: list[dict] = []
        summary = {"success": 0, "failed": 0, "rolled_back": 0}
        run_sqlsync = sqlsync_cfg is not None and (selected is None or "(sqlsync)" in selected)

        if run_sqlsync:
            emit("update_progress", {"item": "(sqlsync)", "type": "sqlsync", "stage": "start"})
            try:
                rep = _run_sqlsync_once()
                result = "success" if rep.status == "success" else "failed"
                items.append(_sqlsync_item(result, rep.error, rep.status, rep.run_id))
            except Exception as e:  # sqlsync 域故障不阻断制品更新
                log.exception("sqlsync 执行异常")
                items.append(_sqlsync_item("failed", f"{type(e).__name__}: {e}"))
            summary[items[-1]["result"]] += 1
            emit("update_progress", {"item": "(sqlsync)", "stage": "done", "record": items[-1]})

        plan = checker.run()
        if selected is not None:
            plan.pending = [p for p in plan.pending if p.name in selected]
        runner = UpdateRunner(cfg, state=getattr(checker, "state", None), on_event=emit)
        report = runner.run(plan)
        for key in summary:
            summary[key] += report["summary"].get(key, 0)
        report["items"] = items + report["items"]
        report["summary"] = summary
        return report

    @app.websocket("/api/v1/events")
    async def events(ws: WebSocket, token: str = Query("")):
        if not secrets.compare_digest(token, app.state.token):
            await ws.close(code=4401)
            return
        await ws.accept()
        hub.add(ws)
        try:
            while True:
                await ws.receive_text()  # 忽略客户端消息，只作保活
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(ws)

    return app
