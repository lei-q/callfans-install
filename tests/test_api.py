"""service/api.py：token 鉴权与核心端点（stub checker，无外部依赖）。"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from callfans import __version__
from callfans.config import Config
from callfans.core.models import PendingItem, UpdatePlan
from callfans.service.api import create_app

TOKEN = "test-token"


def make_cfg():
    return Config(
        harbor_api_url="https://harbor.example.com",
        harbor_project="callfans",
        harbor_username="u",
        harbor_password="p",
    )


class StubChecker:
    def __init__(self):
        self.plan = UpdatePlan(
            checked_at="2026-09-17T10:00:00+00:00",
            pending=[PendingItem(
                name="callfans/api", type="server",
                old="20260901102030-abc1234", new="20260912132921-123a066",
                changelog="修复",
            )],
        )
        self.calls = 0

    def run(self):
        self.calls += 1
        return self.plan


def make_client():
    stub = StubChecker()
    app = create_app(make_cfg(), checker=stub, token=TOKEN, enable_scheduler=False)
    return TestClient(app), stub


def test_unauthorized():
    client, _ = make_client()
    with client:
        assert client.get("/api/v1/status").status_code == 401
        assert client.get(
            "/api/v1/status", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401


def test_status_check_pending():
    client, stub = make_client()
    with client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        # 初始无结果
        assert client.get("/api/v1/pending", headers=headers).json() == {
            "checked_at": None, "pending": []
        }
        # 触发检查
        resp = client.post("/api/v1/check", headers=headers)
        assert resp.status_code == 200
        plan = resp.json()
        assert plan["pending"][0]["new"] == "20260912132921-123a066"
        assert stub.calls == 1
        # pending 与 status 联动
        assert client.get("/api/v1/pending", headers=headers).json() == plan
        status = client.get("/api/v1/status", headers=headers).json()
        assert status["pending_count"] == 1
        assert status["version"] == __version__
        assert status["last_check"] is not None


def test_update_empty_plan():
    client, _ = make_client()
    with client:
        resp = client.post("/api/v1/update", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        report = resp.json()
        assert report["items"] == []
        assert report["summary"] == {"success": 0, "failed": 0, "rolled_back": 0}


def test_update_merges_sqlsync_then_artifacts(monkeypatch):
    """更新链路：sqlsync → 制品；sqlsync 失败不阻断后续（Q9/M6）。"""
    from callfans.service import api as api_mod

    class FakeReport:
        status = "aborted_by_guard"
        run_id = "R1"
        executed = 0
        total = 0
        error = "护栏拦截（--force 可越过）:\n[G2] 语句超限"
        guard_summary = "[G2] 语句超限"

    class FakeRuntime:
        def __init__(self, cfg, on_event=None, **kw):
            pass

        def apply(self, force=False):
            return FakeReport()

        def build(self):
            raise AssertionError("update 链路不应构建漂移计划")

    monkeypatch.setattr(api_mod, "SqlSyncRuntime", FakeRuntime)

    class EmptyChecker:
        def run(self):
            return UpdatePlan(checked_at="t", pending=[])

    app = create_app(make_cfg(), checker=EmptyChecker(), token=TOKEN,
                     enable_scheduler=False, sqlsync_cfg=object())
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        resp = client.post("/api/v1/update", headers=headers)
        assert resp.status_code == 200
        report = resp.json()
        assert report["items"][0]["type"] == "sqlsync"
        assert report["items"][0]["result"] == "failed"
        assert report["summary"]["failed"] == 1
        assert "sqlsync" in str(client.get("/api/v1/status", headers=headers).json())


def test_check_includes_sqlsync_drift(monkeypatch):
    """检查按钮对数据库漂移可见（2026-09-21 补）：有漂移进待更新列表。"""
    from callfans.core.sync.plan import SyncPlan
    from callfans.service import api as api_mod

    class FakePlan:
        statement_count = 3

        def report_text(self):
            return "语句总数: 3｜涉及表: 1\n结构: 修改列 mobile_arm_server"

    class FakeRuntime:
        def __init__(self, cfg, on_event=None, **kw):
            pass

        def build(self):
            return FakePlan()

    monkeypatch.setattr(api_mod, "SqlSyncRuntime", FakeRuntime)

    class EmptyChecker:
        def run(self):
            return UpdatePlan(checked_at="t", pending=[])

    app = create_app(make_cfg(), checker=EmptyChecker(), token=TOKEN,
                     enable_scheduler=False, sqlsync_cfg=object())
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        resp = client.post("/api/v1/check", headers=headers)
        assert resp.status_code == 200
        pending = resp.json()["pending"]
        assert [p["type"] for p in pending] == ["sqlsync"]
        assert pending[0]["new"] == "3 条数据库变更"
        assert "mobile_arm_server" in pending[0]["changelog"]


def test_ws_events_broadcast():
    client, stub = make_client()
    with client:
        with client.websocket_connect(f"/api/v1/events?token={TOKEN}") as ws:
            client.post("/api/v1/check", headers={"Authorization": f"Bearer {TOKEN}"})
            msg = ws.receive_json()
            assert msg["event"] == "check_done"
            assert msg["data"]["pending"][0]["name"] == "callfans/api"


def test_ws_wrong_token_rejected():
    client, _ = make_client()
    with client:
        # 拒绝发生在握手阶段：close(4401)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/events?token=bad"):
                pass
