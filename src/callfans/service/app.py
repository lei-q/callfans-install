"""服务入口：callfans-service / callfans serve。"""

from __future__ import annotations

import argparse
import logging
import socket

import uvicorn

from ..config import Config
from ..logging_setup import setup_logging
from ..paths import log_file
from .api import create_app

log = logging.getLogger(__name__)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_service(env_path, port: int | None = None) -> None:
    cfg = Config.from_env(env_path)
    setup_logging(log_file())
    port = port or cfg.bind_port or _free_port()
    app = create_app(cfg, write_runtime_file=True, port=port)
    log.info("callfans 服务启动: 127.0.0.1:%d (project=%s)", port, cfg.harbor_project)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    try:
        server.run()
    finally:
        log.info("callfans 服务退出")


def main() -> None:
    parser = argparse.ArgumentParser(prog="callfans-service", description="callfans 后台服务")
    parser.add_argument("--env", default=".env", help=".env 配置路径（默认当前目录）")
    parser.add_argument("--port", type=int, default=None, help="固定 IPC 端口（默认随机）")
    args = parser.parse_args()
    try:
        run_service(args.env, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
