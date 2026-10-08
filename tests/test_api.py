"""service/api.py：token 鉴权与核心端点（stub checker，无外部依赖）。"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from callfans import __version__
from callfans.config import Config
from callfans.core.models import PendingItem, UpdatePlan
from callfans.service.api import create_app

TOKEN = "test-token"


def make_cfg(**kw):
    base = dict(
        harbor_api_url="https://harbor.example.com",
        harbor_project="callfans",
        harbor_username="u",
        harbor_password="p",
    )
    base.update(kw)
    return Config(**base)


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
    app = create_app(make_cfg(), checker=stub, token=TOKEN,
                     enable_scheduler=False, sqlsync_cfg=None)
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
    from callfans.service import api as api_mod

    class FakePlan:
        statement_count = 3

        def report_text(self):
            return "语句总数: 3｜涉及表: 1\n结构: 修改列 mobile_arm_server"

        def up_statements(self):
            return ["ALTER TABLE `db`.`mobile_arm_server` MODIFY COLUMN `k` varchar(64);",
                    "ALTER TABLE `db`.`t2` ADD COLUMN `c` int;"]

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
        # #2：详情携带变更 SQL（UI 导出/预览用）
        assert pending[0]["sql"] and "mobile_arm_server" in pending[0]["sql"][0]


def test_check_sqlsync_eval_failure_visible(monkeypatch):
    """漂移评估失败不静默：以待更新伪条目暴露原因。"""
    from callfans.service import api as api_mod

    class FakeRuntime:
        def __init__(self, cfg, on_event=None, **kw):
            pass

        def build(self):
            raise RuntimeError("云端元表不可读: table doesn't exist")

    monkeypatch.setattr(api_mod, "SqlSyncRuntime", FakeRuntime)

    class EmptyChecker:
        def run(self):
            return UpdatePlan(checked_at="t", pending=[])

    app = create_app(make_cfg(), checker=EmptyChecker(), token=TOKEN,
                     enable_scheduler=False, sqlsync_cfg=object())
    with TestClient(app) as client:
        resp = client.post("/api/v1/check",
                           headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        pending = resp.json()["pending"]
        assert pending[0]["new"] == "漂移评估失败"
        assert "元表不可读" in pending[0]["changelog"]



def test_update_selective_items(monkeypatch, tmp_path):
    """立即更新支持勾选（#3）：只更新 items 指定的条目（FakeRunner 打桩）。"""
    import callfans.service.api as api_mod
    from callfans.core.models import PendingItem, UpdatePlan

    seen_plans: list = []

    class FakeRunner:
        def __init__(self, *a, **kw):
            pass

        def run(self, plan):
            seen_plans.append([i.name for i in plan.pending])
            return {
                "started_at": "t", "finished_at": "t", "preflight_error": None,
                "items": [{"name": i.name, "type": i.type, "old": i.old, "new": i.new,
                           "result": "success", "error": None} for i in plan.pending],
                "summary": {"success": len(plan.pending), "failed": 0, "rolled_back": 0},
            }

    monkeypatch.setattr(api_mod, "UpdateRunner", FakeRunner)

    class Checker2:
        def run(self):
            return UpdatePlan(checked_at="t", pending=[
                PendingItem(name="callfans/api", type="server", old="1", new="2"),
                PendingItem(name="callfans/web", type="frontend", old="1", new="2"),
            ])

    app = create_app(make_cfg(frontend_output_dir=tmp_path / "web"),
                     checker=Checker2(), token=TOKEN,
                     enable_scheduler=False, sqlsync_cfg=None)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        resp = client.post("/api/v1/update", headers=headers,
                           json={"items": ["callfans/web"]})
        assert resp.status_code == 200
        # 只勾选 web → runner 只收到 web
        assert seen_plans[-1] == ["callfans/web"]
        report = resp.json()
        assert [r["name"] for r in report["items"]] == ["callfans/web"]
        # 不带 body（全部）：runner 收到全部
        resp2 = client.post("/api/v1/update", headers=headers)
        assert seen_plans[-1] == ["callfans/api", "callfans/web"]


def test_host_ip_refresh(monkeypatch, tmp_path):
    """刷新宿主机 IP：写部署 .env + 重建 xray-rest + status 暴露。"""
    import callfans.service.api as api_mod
    from callfans.core.updaters import docker_cli as dc_mod

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  xray-rest:\n    image: xray-rest\n", encoding="utf-8")
    deploy_env = tmp_path / ".env"
    deploy_env.write_text("HOST_IP=127.0.0.1\n", encoding="utf-8")

    monkeypatch.setattr(api_mod, "detect_host_ip", lambda: "172.25.1.100")
    ups: list = []

    class FakeDocker:
        def version_ok(self):
            return True

        def compose_up(self, f, service, force_recreate=False, on_line=None):
            ups.append((service, force_recreate))

    monkeypatch.setattr(dc_mod.DockerCLI, "version_ok", FakeDocker.version_ok)
    monkeypatch.setattr(dc_mod.DockerCLI, "compose_up",
                        lambda self, f, s, force_recreate=False, on_line=None:
                        ups.append((s, force_recreate)))

    app = create_app(make_cfg(compose_file=compose), checker=StubChecker(),
                     token=TOKEN, enable_scheduler=False)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        resp = client.post("/api/v1/host-ip/refresh", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ip"] == "172.25.1.100" and data["recreated"] is True
        # xray-rest 被强制重建
        assert ups == [("xray-rest", True)]
        # 部署 .env 已更新
        assert "HOST_IP=172.25.1.100" in deploy_env.read_text(encoding="utf-8")
        # status 暴露当前 IP
        assert client.get("/api/v1/status", headers=headers).json()["host_ip"] == "172.25.1.100"


def test_host_ip_refresh_no_compose():
    app = create_app(make_cfg(), checker=StubChecker(), token=TOKEN,
                     enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.post("/api/v1/host-ip/refresh",
                           headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 400
