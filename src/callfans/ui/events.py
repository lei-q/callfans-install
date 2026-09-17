"""WS 事件监听：daemon 线程连服务 /api/v1/events，事件经信号（队列投递）回主线程。

每次重连都重新解析 runtime（服务重启换端口/token 后自动跟随），
不可达时退避重试，供进度条与实时日志消费 update_* / check_done 事件。
"""

from __future__ import annotations

import asyncio
import json
import threading

from PySide6.QtCore import QObject, Signal

_RETRY_SECONDS = 3


class EventListener(QObject):
    event_received = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()

    # ---------- 内部 ----------

    def _loop(self) -> None:
        asyncio.run(self._connect_loop())

    def _ws_url(self) -> str | None:
        from .client import _find_runtime

        rt = _find_runtime()
        if not rt or not rt.get("port") or not rt.get("token"):
            return None
        return f"ws://127.0.0.1:{rt['port']}/api/v1/events?token={rt['token']}"

    async def _connect_loop(self) -> None:
        import websockets

        while not self._stop_evt.is_set():
            url = self._ws_url()
            if url is None:
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            try:
                async with websockets.connect(url, open_timeout=5, ping_interval=20) as ws:
                    async for raw in ws:
                        if self._stop_evt.is_set():
                            return
                        try:
                            payload = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(payload, dict):
                            self.event_received.emit(payload)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(_RETRY_SECONDS)
