"""UI 侧服务自拉起（tech-design §6.1 Windows 模式 A；Linux 桌面同作兜底）。

服务不在运行时，UI 尝试以分离子进程方式启动 callfans-service，
随后靠既有轮询自然连上。带冷却，避免轮询期间反复拉起。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import client

_SPAWN_COOLDOWN_SECONDS = 30


def env_file_path() -> Path:
    """.env 定位：CALLFANS_ENV → 可执行文件目录 → 上级目录（Inno 布局 app/ui + app/.env）→ cwd。"""
    env = os.environ.get("CALLFANS_ENV")
    if env:
        return Path(env)
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    candidates = [exe_dir / ".env", exe_dir.parent / ".env", Path.cwd() / ".env"]
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def _service_command() -> list[str] | None:
    exe = shutil.which("callfans-service")
    if exe:
        return [exe]
    if getattr(sys, "frozen", False):  # 打包布局：ui 目录旁的 service 目录
        name = "callfans-service.exe" if os.name == "nt" else "callfans-service"
        for base in (Path(sys.executable).parent, Path(sys.executable).parent.parent / "service"):
            cand = base / name
            if cand.exists():
                return [str(cand)]
        return None
    return [sys.executable, "-m", "callfans.service.app"]


def ensure_service_running(state: dict) -> bool:
    """服务不可达时尝试拉起（state 为调用方持有的冷却记录）。返回是否发起了拉起。"""
    now = time.monotonic()
    if now - state.get("last_attempt", 0.0) < _SPAWN_COOLDOWN_SECONDS:
        return False
    state["last_attempt"] = now
    try:
        client.status()
        return False  # 已在运行
    except client.ServiceUnavailable:
        pass
    cmd = _service_command()
    if cmd is None:
        return False
    env_path = env_file_path()
    args = [*cmd, "--env", str(env_path)]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # noqa: SIM115
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, cwd=str(env_path.parent), **kwargs)  # noqa: S603
    return True
