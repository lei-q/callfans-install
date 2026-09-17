"""UI → service 的 IPC 客户端（每次调用即时创建，自动跟随服务重启换端口/token）。"""

from __future__ import annotations

import httpx

from ..paths import runtime_candidates
from ..service.runtime import read_runtime


class ServiceUnavailable(RuntimeError):
    """服务未运行或不可达。"""


def _find_runtime() -> dict | None:
    for cand in runtime_candidates():
        rt = read_runtime(cand)
        if rt and rt.get("port") and rt.get("token"):
            return rt
    return None


def _connect(timeout: float = 5.0) -> httpx.Client:
    rt = _find_runtime()
    if rt is None:
        raise ServiceUnavailable("服务未运行（可先启动 callfans serve）")
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{rt['port']}",
        headers={"Authorization": f"Bearer {rt['token']}"},
        timeout=timeout,
    )
    try:
        client.get("/api/v1/status").raise_for_status()
        return client
    except httpx.HTTPError as e:
        client.close()
        raise ServiceUnavailable(f"服务不可达: {e}") from e


def _call(method: str, path: str, timeout: float = 5.0) -> dict:
    try:
        client = _connect(timeout=5.0)
    except ServiceUnavailable:
        raise
    try:
        resp = client.request(method, path, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        raise ServiceUnavailable(f"{path} -> {e.response.status_code}: {detail}") from e
    except httpx.HTTPError as e:
        raise ServiceUnavailable(f"{path} 请求失败: {e}") from e
    finally:
        client.close()


def status() -> dict:
    return _call("GET", "/api/v1/status")


def pending() -> dict:
    return _call("GET", "/api/v1/pending")


def check() -> dict:
    """触发检查（阻塞至完成，可能数十秒）。"""
    return _call("POST", "/api/v1/check", timeout=600)


def update() -> dict:
    """触发更新（阻塞至完成，可能数分钟）。"""
    return _call("POST", "/api/v1/update", timeout=3600)
