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
    """compose config 报缺变量（v0.1.5 Windows 实测场景）；login 可注入。"""

    def __init__(self, missing, login_error=None):
        self.missing = missing
        self.login_error = login_error
        self.logins: list = []

    def version_ok(self):
        return True

    def compose_config_checked(self, f):
        return {"services": {}}, self.missing

    def login(self, registry, username, password):
        self.logins.append((registry, username))
        if self.login_error:
            from callfans.core.updaters.docker_cli import DockerError

            raise DockerError(self.login_error)


def test_preflight_blocks_undefined_compose_vars(tmp_path):
    """compose 同目录 .env 缺 TIMEZONE 等变量 → preflight 拦截，不动任何容器。"""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n"
        "  scrcpy:\n    image: ${HARBOR_REGISTRY}/callfans/scrcpy-live:${SCRCPY_LIVE_TAG}\n",
        encoding="utf-8",
    )
    # SCRCPY_LIVE_TAG 属于本次无待更新的服务，同为 tag 变量也须放行（v0.1.7 回归）
    docker = MissingVarDocker(["TIMEZONE", "MYSQL_ROOT_PASSWORD", "API_TAG", "SCRCPY_LIVE_TAG"])
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                          updaters=stubs, history_path=tmp_path / "h.jsonl")
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert report["preflight_error"] is not None
    # 非 tag 变量必须拦
    assert "TIMEZONE" in report["preflight_error"]
    assert "MYSQL_ROOT_PASSWORD" in report["preflight_error"]
    # tag 变量（含非待更新服务的）一律放行
    assert "API_TAG" not in report["preflight_error"]
    assert "SCRCPY_LIVE_TAG" not in report["preflight_error"]
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


def test_preflight_deterministic_env_check(tmp_path):
    """不依赖 docker 告警文本：.env 缺 HARBOR_REGISTRY 直接按内容判定（v0.1.8 回归）。"""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n", encoding="utf-8"
    )
    env = tmp_path / ".env"
    env.write_text("API_TAG=T1\n", encoding="utf-8")  # 缺 HARBOR_REGISTRY
    docker = MissingVarDocker([])  # docker 侧无告警（模拟格式不匹配/未输出）
    stubs = {"server": StubUpdater()}
    runner = UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                          updaters=stubs, history_path=tmp_path / "h.jsonl")
    report = runner.run(plan_of(make_item("server", "callfans/api")))
    assert "HARBOR_REGISTRY" in report["preflight_error"]
    assert "API_TAG" not in report["preflight_error"]
    # 补上后通过
    env.write_text("API_TAG=T1\nHARBOR_REGISTRY=172.25.1.220\n", encoding="utf-8")
    runner2 = UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                           updaters=stubs, history_path=tmp_path / "h.jsonl")
    assert runner2.run(plan_of(make_item("server", "callfans/api")))["preflight_error"] is None


def test_preflight_docker_login(tmp_path):
    """私有仓库：preflight 用 Harbor 凭据自动 docker login（v0.2.2 回归）。"""
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "HARBOR_REGISTRY=47.87.66.98\nAPI_TAG=T1\n", encoding="utf-8"
    )
    stubs = {"server": StubUpdater()}

    # 登录成功 → preflight 通过，且确实对仓库地址 login
    docker = MissingVarDocker([])
    UpdateRunner(make_cfg(compose_file=compose), docker=docker,
                 updaters=stubs, history_path=tmp_path / "h.jsonl"
                 ).run(plan_of(make_item("server", "callfans/api")))
    assert docker.logins == [("47.87.66.98", "u")]

    # 登录失败 → 拦截并提示凭据
    docker2 = MissingVarDocker([], login_error="docker login 47.87.66.98 失败: unauthorized")
    report = UpdateRunner(make_cfg(compose_file=compose), docker=docker2,
                          updaters=stubs, history_path=tmp_path / "h.jsonl"
                          ).run(plan_of(make_item("server", "callfans/api")))
    assert "docker login" in report["preflight_error"]
    assert "HARBOR_USERNAME/PASSWORD" in report["preflight_error"]


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
