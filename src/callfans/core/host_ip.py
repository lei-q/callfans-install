"""宿主机 IP 探测（安装器外的运行时刷新用，UI 刷新按钮走这里）。"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger(__name__)


def detect_host_ip() -> str | None:
    """探测宿主机 IPv4：

    1. UDP connect 探测默认路由出口（不实际发包，8.8.8.8 只作路由目标）
    2. 退化：gethostbyname(hostname)（Windows 通常返回 LAN IP）
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None
