"""子进程统一封装：Windows 下无控制台父进程拉起控制台程序（docker 等）时
不加该标志会弹出黑窗口，全部子进程调用必须走这里。"""

from __future__ import annotations

import subprocess

# Windows 才有 CREATE_NO_WINDOW；POSIX 为 0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, creationflags=NO_WINDOW, **kwargs)
