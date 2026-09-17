"""定时检查调度：默认每 CHECK_INTERVAL_HOURS 小时；手动检查会重置计时。

实现为分片睡眠（每分钟醒一次重算到期时间），手动检查更新 last_check 后自然推迟。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_STARTUP_GRACE_SECONDS = 15
_TICK_SECONDS = 60


def start_scheduler(cfg, state, run_check) -> asyncio.Task:
    """state 需带 last_check 属性（service.api.CheckState）。"""

    async def _loop() -> None:
        await asyncio.sleep(_STARTUP_GRACE_SECONDS)
        while True:
            interval = timedelta(hours=cfg.check_interval_hours)
            last = state.last_check
            now = datetime.now(timezone.utc)
            due = last + interval if last else now
            wait = (due - now).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, _TICK_SECONDS) + 1)
                continue
            try:
                await run_check()
            except Exception:
                log.exception("定时检查失败")
            await asyncio.sleep(1)

    return asyncio.create_task(_loop())
