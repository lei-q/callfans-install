"""平台标准路径（platformdirs 封装）。"""

import os
import sys
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "callfans"

_dirs = PlatformDirs(APP_NAME, appauthor=False)


def env_file() -> Path:
    """应用 .env 定位（与 UI 自拉起服务的 --env 同源）。

    CALLFANS_ENV 显式指定 → 打包形态取可执行文件目录/上级目录
    （CLI 在 {app}\\cli、UI 在 {app}\\ui，.env 在 {app}）→ 开发态当前目录。
    """
    override = os.environ.get("CALLFANS_ENV")
    if override:
        return Path(override)
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidates += [exe_dir / ".env", exe_dir.parent / ".env"]
    candidates.append(Path.cwd() / ".env")
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]

# 系统级安装（.deb，服务以 root 运行）的共享 runtime 文件位置；
# 用户级安装不受影响（candidates 依次探测）
SYSTEM_RUNTIME_FILE = Path("/run/callfans/runtime.json")


def state_file() -> Path:
    """持久状态文件（版本记账 + 最近一次检查结果）。"""
    d = Path(_dirs.user_data_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def history_file() -> Path:
    """更新历史（update_history.jsonl）。"""
    d = Path(_dirs.user_data_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "update_history.jsonl"


def log_file() -> Path:
    d = Path(_dirs.user_log_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "callfans.log"


def runtime_file() -> Path:
    """IPC runtime 文件写入位置（port/token/pid）。

    优先环境变量 CALLFANS_RUNTIME_FILE（系统级 systemd unit 注入
    /run/callfans/runtime.json），默认用户级运行时目录。
    """
    env = os.environ.get("CALLFANS_RUNTIME_FILE")
    if env:
        p = Path(env)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    d = Path(_dirs.user_runtime_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "runtime.json"


def runtime_candidates() -> list[Path]:
    """runtime 文件读取探测顺序：环境变量 → 用户级 → 系统级。"""
    cands = []
    env = os.environ.get("CALLFANS_RUNTIME_FILE")
    if env:
        cands.append(Path(env))
    cands.append(Path(_dirs.user_runtime_dir) / "runtime.json")
    cands.append(SYSTEM_RUNTIME_FILE)
    return cands
