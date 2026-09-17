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
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import __version__
from ..config import Config
from ..core.checker import Checker
from ..core.harbor import HarborClient
from ..core.models import UpdatePlan
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
) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    hub = EventHub()
    state = CheckState()
    main_loop: asyncio.AbstractEventLoop | None = None

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
        if enable_scheduler:
            task = start_scheduler(cfg, state, do_check)
        if write_runtime_file and port is not None:
            write_runtime(runtime_file(), port, token, os.getpid())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
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
    async def update() -> dict:
        if state.updating or state.busy:
            raise HTTPException(status_code=409, detail="check or update in progress")
        state.updating = True
        try:
            report = await anyio.to_thread.run_sync(_run_update_sync)
            return report
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            state.updating = False

    def _run_update_sync() -> dict:
        """更新前先重新检查（§2），再执行编排。"""
        plan = checker.run()
        runner = UpdateRunner(cfg, state=getattr(checker, "state", None), on_event=emit)
        return runner.run(plan)

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
