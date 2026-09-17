"""更新编排：顺序（SQL → server → 前端）、失败隔离、preflight、历史写入。"""

from callfans.core.models import PendingItem, UpdatePlan
from callfans.core.updaters.docker_cli import DockerError
from callfans.core.updaters.runner import UpdateRunner
from tests.test_checker import make_cfg


class StubUpdater:
    def __init__(self, result="success"):
        self.result = result
        self.items = []

    def update(self, item):
        self.items.append(item)
        return {
            "name": item.name, "type": item.type, "old": item.old, "new": item.new,
            "result": self.result, "error": None,
        }


class RaisingDocker:
    def version_ok(self):
        raise DockerError("daemon down")

    def compose_config(self, f):
        raise DockerError("daemon down")


def plan_of(*items):
    return UpdatePlan(checked_at="2026-09-17T00:00:00+00:00", pending=list(items))


def make_item(type_, name):
    return PendingItem(name=name, type=type_, old="t-old", new="t-new")


def test_order_sql_server_frontend(tmp_path):
    stubs = {"server": StubUpdater(), "frontend": StubUpdater(), "sql": StubUpdater()}
    events: list = []
    runner = UpdateRunner(make_cfg(), updaters=stubs, history_path=tmp_path / "h.jsonl",
                          run_preflight=False,
                          on_event=lambda e, d: events.append((e, d)))
    report = runner.run(plan_of(make_item("frontend", "callfans/web"),
                                make_item("server", "callfans/api"),
                                make_item("sql", "callfans/db")))
    assert [i.type for i in stubs["sql"].items] == ["sql"]
    assert [i.type for i in stubs["server"].items] == ["server"]
    assert [i.type for i in stubs["frontend"].items] == ["frontend"]
    # 执行顺序也体现在 report.items
    assert [r["type"] for r in report["items"]] == ["sql", "server", "frontend"]
    assert report["summary"] == {"success": 3, "failed": 0, "rolled_back": 0}
    # 历史逐条落盘
    lines = (tmp_path / "h.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # 事件流：update_begin（含总数，UI 进度条分母）→ 各项 → update_done
    assert events[0][0] == "update_begin"
    assert events[0][1]["total"] == 3
    assert events[-1][0] == "update_done"
    stage_events = [e for e, d in events if e == "update_progress" and d.get("stage") == "done"]
    assert len(stage_events) == 3


def test_single_failure_does_not_block(tmp_path):
    stubs = {"sql": StubUpdater("failed"), "server": StubUpdater(), "frontend": StubUpdater()}
    runner = UpdateRunner(make_cfg(), updaters=stubs, history_path=tmp_path / "h.jsonl",
                          run_preflight=False)
    report = runner.run(plan_of(make_item("sql", "callfans/db"),
                                make_item("server", "callfans/api"),
                                make_item("frontend", "callfans/web")))
    assert len(report["items"]) == 3
    assert report["summary"]["failed"] == 1
    assert report["summary"]["success"] == 2
    assert len(stubs["server"].items) == 1  # 失败后其他项照常执行


def test_empty_plan_no_preflight(tmp_path):
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(), docker=RaisingDocker(), updaters=stubs,
                          history_path=tmp_path / "h.jsonl", run_preflight=True)
    report = runner.run(plan_of())
    assert report["summary"] == {"success": 0, "failed": 0, "rolled_back": 0}
    assert stubs["server"].items == []


def test_preflight_failure_aborts_all(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  api:\n    image: h/callfans/api:${API_TAG}\n", encoding="utf-8")
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(compose_file=compose), docker=RaisingDocker(),
                          updaters=stubs, history_path=tmp_path / "h.jsonl")
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert "docker/compose 不可用" in report["preflight_error"]
    assert report["items"] == []
    assert stubs["server"].items == []


class MissingVarDocker:
    """compose config 报缺变量（v0.1.5 Windows 实测场景）。"""

    def __init__(self, missing):
        self.missing = missing

    def version_ok(self):
        return True

    def compose_config_checked(self, f):
        return {"services": {}}, self.missing


def test_preflight_blocks_undefined_compose_vars(tmp_path):
    """compose 同目录 .env 缺 TIMEZONE 等变量 → preflight 拦截，不动任何容器。"""
    from callfans.core.updaters.runner import UpdateRunner as UR  # noqa: F401

    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n", encoding="utf-8"
    )
    docker = MissingVarDocker(["TIMEZONE", "MYSQL_ROOT_PASSWORD", "API_TAG"])
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                          updaters=stubs, history_path=tmp_path / "h.jsonl")
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert report["preflight_error"] is not None
    # tag 变量（API_TAG）允许缺失（首装由更新器写入）；其余必须拦
    assert "TIMEZONE" in report["preflight_error"]
    assert "MYSQL_ROOT_PASSWORD" in report["preflight_error"]
    assert "API_TAG" not in report["preflight_error"].split("（")[0]
    assert stubs["server"].items == []


def test_preflight_allows_only_tag_var_missing(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  api:\n    image: h/callfans/api:${API_TAG}\n", encoding="utf-8")
    docker = MissingVarDocker(["API_TAG"])  # 仅缺 tag 变量：合法（首装场景）
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                          updaters=stubs, history_path=tmp_path / "h.jsonl")
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert report["preflight_error"] is None
    assert report["summary"]["success"] == 1


def test_updater_exception_isolated(tmp_path):
    class Exploding:
        def update(self, item):
            raise RuntimeError("boom")

    runner = UpdateRunner(make_cfg(), updaters={"server": Exploding()},
                          history_path=tmp_path / "h.jsonl", run_preflight=False)
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert report["summary"]["failed"] == 1
    assert "boom" in report["items"][0]["error"]


def test_artifact_puller_follows_scheme():
    """http:// 仓库 → oras-py insecure（走 http）；https/无 scheme → 默认 https。"""
    from callfans.core.updaters.artifact_puller import ArtifactPuller

    p = ArtifactPuller("http://172.25.1.220", "u", "pw")
    assert p.host == "172.25.1.220" and p.insecure is True
    p2 = ArtifactPuller("https://harbor.example.com", "u", "pw")
    assert p2.host == "harbor.example.com" and p2.insecure is False
    p3 = ArtifactPuller("harbor.example.com", "u", "pw")
    assert p3.host == "harbor.example.com" and p3.insecure is False
