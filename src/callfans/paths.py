"""平台标准路径（platformdirs 封装）。"""

from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "callfans"

_dirs = PlatformDirs(APP_NAME, appauthor=False)


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
    """IPC runtime 文件（port/token/pid），放用户级运行时目录。"""
    d = Path(_dirs.user_runtime_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "runtime.json"
