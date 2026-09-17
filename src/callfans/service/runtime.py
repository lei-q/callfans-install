"""IPC runtime 文件：{port, token, pid}，写入用户级运行时目录（0600）。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def write_runtime(path: Path, port: int, token: str, pid: int) -> None:
    data = {
        "port": port,
        "token": token,
        "pid": pid,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="runtime.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o640)  # 系统级安装时组（callfans）可读，桌面 UI 由此访问 IPC
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_runtime(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_runtime(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
