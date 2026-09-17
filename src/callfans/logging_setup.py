"""日志装配：控制台 + 轮转文件（10MB × 5）。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_path: Path | None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:  # 已初始化则只调整级别
        return
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)
    if log_path is not None:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FMT))
        root.addHandler(file_handler)
