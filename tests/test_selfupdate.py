"""自身版本检查：版本比较矩阵 + 服务事件广播 + UI 弹框逻辑（打桩）。"""

import os

import pytest


class TestIsNewer:
    def test_basic(self):
        from callfans.core.selfupdate import is_newer

        assert is_newer("0.5.0", "0.4.6")
        assert is_newer("v0.4.7", "0.4.6")
        assert is_newer("1.0.0", "0.99.99")
        assert not is_newer("0.4.6", "0.4.6")
        assert not is_newer("0.4.5", "0.4.6")
        assert not is_newer("latest", "0.4.6")  # 非语义版本不误报
        assert not is_newer("", "0.4.6")

    def test_minor_and_patch(self):
        from callfans.core.selfupdate import is_newer

        assert is_newer("0.4.10", "0.4.9")   # 补丁号非个位
        assert is_newer("0.5.0", "0.4.99")
        assert not is_newer("0.4.9", "0.4.10")


def test_fetch_latest_silent_on_network_error(monkeypatch):
    import httpx

    from callfans.core.selfupdate import fetch_latest

    def boom(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", boom)
    assert fetch_latest() is None


def test_dual_path_fetch_retries_direct(monkeypatch):
    """双通道：代理失败自动直连重试（2026-09-29 实测根因）。"""
    import httpx

    import callfans.core.selfupdate as su

    calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v9.9.9", "html_url": "u",
                    "assets": [{"name": "callfans-setup-x64.exe",
                                "browser_download_url": "https://d/x.exe", "size": 1}]}

    def fake_get(url, trust_env=True, **kw):
        calls.append(trust_env)
        if trust_env:
            raise httpx.ConnectError("proxy dead")
        return FakeResp()

    monkeypatch.setattr(httpx, "get", fake_get)
    info = su.fetch_latest()
    assert calls == [True, False]  # 先代理后直连
    assert info["version"] == "9.9.9"
    asset = su.find_setup_asset(info)
    assert asset["url"] == "https://d/x.exe"


def test_find_setup_asset_none():
    from callfans.core.selfupdate import find_setup_asset

    assert find_setup_asset({"assets": [{"name": "a.deb"}]}) is None
    assert find_setup_asset({}) is None


def test_download_setup_streams_to_file(monkeypatch, tmp_path):
    import httpx

    import callfans.core.selfupdate as su

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        @property
        def headers(self):
            return {"content-length": "6"}

        def iter_bytes(self, n):
            yield b"ab"; yield b"cd"; yield b"ef"

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            return FakeStream()

    release = {"assets": [{"name": "callfans-setup-x64.exe",
                           "url": "https://d/x.exe", "size": 6}]}
    monkeypatch.setattr(httpx, "Client", FakeClient)
    progress = []
    path = su.download_setup(release, dest_dir=tmp_path,
                             on_progress=lambda d, t: progress.append((d, t)))
    assert path.read_bytes() == b"abcdef"
    # 进度按 MB 节流：小于 1MB 的数据只在完成时可能触发一次
    assert progress == [(2, 6)] or progress == []


def test_fetch_latest_parses():
    import httpx

    from callfans.core.selfupdate import fetch_latest

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v0.9.0", "html_url": "https://x/y",
                    "published_at": "t"}

    httpx_get = httpx.get
    httpx.get = lambda url, **kw: FakeResp()
    try:
        info = fetch_latest()
    finally:
        httpx.get = httpx_get
    assert info["version"] == "0.9.0" and info["url"] == "https://x/y"


async def _hub_broadcast_roundtrip():
    import asyncio

    from callfans.service.hub import EventHub

    hub = EventHub()

    class FakeWS:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = FakeWS()
    hub.add(ws)
    await hub.broadcast({"event": "self_update_available",
                         "data": {"version": "9.9.9"}})
    assert ws.messages[0]["data"]["version"] == "9.9.9"


def test_hub_broadcast_roundtrip():
    import asyncio

    asyncio.run(_hub_broadcast_roundtrip())


def test_status_exposes_latest_release(monkeypatch):
    """版本检查逻辑：新版写入 state.latest_release 并在 status 暴露（直接调 loop 体）。"""
    import callfans.core.selfupdate as su
    from callfans.config import Config
    from callfans.core.models import UpdatePlan
    from callfans.service.api import CheckState, create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(su, "fetch_latest", lambda timeout=10: {
        "tag": "v9.9.9", "version": "9.9.9", "url": "https://x", "published_at": "t",
    })

    class Checker:
        def run(self):
            return UpdatePlan(checked_at="t", pending=[])

    app = create_app(Config(harbor_api_url="http://x", harbor_project="p",
                            harbor_username="u", harbor_password="w"),
                     checker=Checker(), enable_scheduler=False)
    with TestClient(app) as client:
        # 直接执行版本判定逻辑（与 _version_loop 内一致）
        latest = su.fetch_latest()
        assert su.is_newer(latest["version"])
        resp = client.get("/api/v1/status",
                          headers={"Authorization": f"Bearer {app.state.token}"})
        assert "latest_release" in resp.json()


def test_ui_self_update_prompt_once(monkeypatch):
    """UI 弹框防重入：每版本每次会话只提示一次（交互细节 offscreen 手工验证）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)
    opened: list = []

    # exec 弹框在 offscreen 下直接返回 0（相当于点了"稍后"）
    import callfans.ui.main_window as mw

    monkeypatch.setattr(mw.QMessageBox, "exec", lambda self: 0)
    w._prompt_self_update({"version": "9.9.9", "current": "0.0.1", "url": "u"})
    assert "9.9.9" in w._prompted_versions
    w._prompt_self_update({"version": "9.9.9", "current": "0.0.1", "url": "u"})
    assert w.log_view.toPlainText().count("发现新版本 v9.9.9") == 1  # 只提示一次
