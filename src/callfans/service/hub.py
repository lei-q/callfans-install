"""WebSocket 事件广播（UI/CLI 实时收检查与更新进度事件）。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class EventHub:
    def __init__(self) -> None:
        self._subs: set = set()

    def add(self, ws) -> None:
        self._subs.add(ws)

    def remove(self, ws) -> None:
        self._subs.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        for ws in list(self._subs):
            try:
                await ws.send_json(payload)
            except Exception:  # 断线即剔除
                self.remove(ws)
