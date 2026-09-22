"""自身版本检查：查询 GitHub 最新发行版并比较。

只读检查（不打扰、失败静默）；下载与安装仍走安装包升级流程——
Windows 下运行中的 exe 无法自替换，弹框引导到 Release 页是务实做法。
"""

from __future__ import annotations

import logging
import re

import httpx

from .. import __version__

log = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/lei-q/callfans-install/releases/latest"


def _parse(version: str) -> tuple | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def is_newer(latest: str, current: str = __version__) -> bool:
    l, c = _parse(latest), _parse(current)
    return l is not None and c is not None and l > c


def fetch_latest(timeout: float = 10.0) -> dict | None:
    """GitHub 最新 Release 信息；网络失败返回 None（静默）。"""
    try:
        resp = httpx.get(RELEASES_API, timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("版本检查失败（离线/被墙均可忽略）: %s", e)
        return None
    tag = str(data.get("tag_name") or "")
    if not tag:
        return None
    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "url": data.get("html_url") or
            "https://github.com/lei-q/callfans-install/releases/latest",
        "published_at": data.get("published_at"),
    }
