"""自身版本检查与升级：查询 GitHub 最新发行版、比较、下载安装包。

网络策略（2026-09-29 实测教训）：客户机系统代理可能是死的（Harbor 10061
同源问题），但 GitHub 出网又可能需要代理——所以全部外部请求走双通道：
先按环境（走代理）尝试，连接失败再直连重试。
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import httpx

from .. import __version__

log = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/lei-q/callfans-install/releases/latest"
_TIMEOUT = 15.0


class SelfUpdateError(RuntimeError):
    pass


def _parse(version: str) -> tuple | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def is_newer(latest: str, current: str = __version__) -> bool:
    l, c = _parse(latest), _parse(current)
    return l is not None and c is not None and l > c


def _get_json_dual(url: str) -> dict:
    """先走环境代理（GitHub 出网常需代理），连接失败再直连（代理可能是死的）。"""
    errors = []
    for trust_env in (True, False):
        try:
            resp = httpx.get(url, timeout=_TIMEOUT, trust_env=trust_env,
                             headers={"Accept": "application/vnd.github+json"})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            errors.append(f"trust_env={trust_env}: {type(e).__name__}")
    raise SelfUpdateError(f"GitHub 不可达（{'; '.join(errors)}）")


def fetch_latest() -> dict | None:
    """最新 Release 完整信息（含 assets，供下载安装用）；网络失败返回 None（静默）。"""
    try:
        data = _get_json_dual(RELEASES_API)
    except Exception as e:
        log.debug("版本检查失败（离线/代理不可用，均忽略）: %s", e)
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
        "assets": [
            {"name": a.get("name"), "url": a.get("browser_download_url"),
             "size": a.get("size")}
            for a in (data.get("assets") or [])
        ],
    }


def find_setup_asset(release: dict) -> dict | None:
    """Release 资产里挑 Windows 安装包。"""
    for a in release.get("assets") or []:
        name = str(a.get("name") or "").lower()
        if "setup" in name and name.endswith(".exe") and a.get("url"):
            return a
    return None


def download_setup(release: dict, dest_dir: Path | None = None,
                   on_progress=None) -> Path:
    """下载安装包到临时目录；on_progress(已下载字节, 总字节) 节流回调。"""
    asset = find_setup_asset(release)
    if asset is None:
        raise SelfUpdateError("Release 未找到 Windows 安装包（callfans-setup-x64.exe）")
    dest = Path(dest_dir or tempfile.gettempdir()) / str(asset["name"])
    errors = []
    for trust_env in (True, False):
        try:
            with httpx.Client(follow_redirects=True, timeout=600.0,
                              trust_env=trust_env) as client:
                with client.stream("GET", str(asset["url"])) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or asset.get("size") or 0)
                    done = 0
                    last_mb = -1
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(256 * 1024):
                            f.write(chunk)
                            done += len(chunk)
                            mb = done // (1024 * 1024)
                            if on_progress and mb != last_mb:
                                last_mb = mb
                                on_progress(done, total)
            if total and done != total:
                raise SelfUpdateError(
                    f"下载不完整: {done}/{total} 字节，请重试或改用【前往下载页】")
            return dest
        except SelfUpdateError:
            raise
        except Exception as e:
            errors.append(f"trust_env={trust_env}: {type(e).__name__}")
            dest.unlink(missing_ok=True)
    raise SelfUpdateError(f"下载失败（{'; '.join(errors)}）")
