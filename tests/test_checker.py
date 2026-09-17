"""checker.py：三类制品的路由与对比编排（Fake Harbor + Fake docker）。"""

from datetime import datetime, timezone

from callfans.config import Config
from callfans.core.checker import Checker
from callfans.core.local import DockerError, DockerImage, StateStore
from callfans.core.models import (
    TYPE_FRONTEND,
    TYPE_SERVER,
    TYPE_SQL,
    KEY_ALIAS,
    KEY_CHANGELOG,
    KEY_TYPE,
    ArtifactTag,
)


def pt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def mk(repo, tag, pushed, digest="sha256:x", ann=None):
    return ArtifactTag(tag=tag, digest=digest, push_time=pt(pushed), annotations=ann or {})


def make_cfg(**kw):
    base = dict(
        harbor_api_url="https://harbor.example.com",
        harbor_project="callfans",
        harbor_username="u",
        harbor_password="p",
    )
    base.update(kw)
    return Config(**base)


class FakeHarbor:
    def __init__(self, repos: dict[str, list[ArtifactTag]], labels=None):
        self.repos = repos
        self.labels = labels or {}
        self.label_calls: list[tuple[str, str]] = []

    def list_repos(self):
        return list(self.repos)

    def repo_tags(self, repo):
        return self.repos[repo]

    def image_labels(self, repo, ref):
        self.label_calls.append((repo, ref))
        return self.labels.get((repo, ref), {})


def run_checker(harbor, docker_rows=None, docker_err=None, state: StateStore | None = None, cfg=None):
    def fake_docker():
        if docker_err:
            raise docker_err
        return docker_rows or []

    return Checker(cfg or make_cfg(), harbor, docker_images_fn=fake_docker, state=state).run()


# ---------- server ----------


def test_server_leading_with_host_prefixed_local_repo(tmp_path):
    harbor = FakeHarbor(
        repos={
            "callfans/api": [
                mk("callfans/api", "20260901102030-abc1234", "2026-09-01T10:20:30Z", "sha256:old"),
                mk("callfans/api", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:new"),
            ]
        },
        labels={("callfans/api", "20260912132921-123a066"): {
            KEY_TYPE: TYPE_SERVER, KEY_CHANGELOG: "修复登录",
        }},
    )
    # 本地 repository 带 registry host 前缀（docker pull harbor.example.com/callfans/api:tag）
    rows = [DockerImage("harbor.example.com/callfans/api", "20260901102030-abc1234", "sha256:old")]
    plan = run_checker(harbor, docker_rows=rows, state=StateStore(tmp_path / "state.json"))
    assert len(plan.pending) == 1
    item = plan.pending[0]
    assert (item.type, item.name, item.old, item.new) == (
        TYPE_SERVER, "callfans/api", "20260901102030-abc1234", "20260912132921-123a066"
    )
    assert item.changelog == "修复登录"
    assert item.digest_new == "sha256:new"
    # Label 只对领先候选拉取，不对全部 tag
    assert harbor.label_calls == [("callfans/api", "20260912132921-123a066")]


def test_server_up_to_date(tmp_path):
    harbor = FakeHarbor(repos={"callfans/api": [
        mk("callfans/api", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:new"),
    ]})
    rows = [DockerImage("callfans/api", "20260912132921-123a066", "sha256:whatever")]
    plan = run_checker(harbor, docker_rows=rows, state=StateStore(tmp_path / "state.json"))
    assert plan.pending == []


def test_server_label_declares_frontend_reclassified(tmp_path):
    harbor = FakeHarbor(repos={"callfans/odd": [
        mk("callfans/odd", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:x"),
    ]}, labels={("callfans/odd", "20260912132921-123a066"): {KEY_TYPE: TYPE_FRONTEND}})
    plan = run_checker(harbor, docker_rows=[], state=StateStore(tmp_path / "state.json"))
    assert [p.type for p in plan.pending] == [TYPE_FRONTEND]


def test_server_repo_skipped_when_docker_unavailable(tmp_path):
    harbor = FakeHarbor(
        repos={
            "callfans/api": [mk("callfans/api", "20260912132921-123a066", "2026-09-12T13:29:21Z")],
            "callfans/web": [mk("callfans/web", "20260912132921-123a066", "2026-09-12T13:29:21Z",
                                ann={KEY_TYPE: TYPE_FRONTEND})],
        }
    )
    plan = run_checker(harbor, docker_err=DockerError("docker down"),
                       state=StateStore(tmp_path / "state.json"))
    # docker 不可用只跳过 server；frontend 照常
    assert [p.name for p in plan.pending] == ["callfans/web"]


# ---------- frontend ----------


def test_frontend_with_alias_and_state(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.set("frontend", {"callfans/web-admin": {"tag": "20260901102030-abc1234", "digest": "sha256:old"}})
    harbor = FakeHarbor(repos={"callfans/web-admin": [
        mk("callfans/web-admin", "20260901102030-abc1234", "2026-09-01T10:20:30Z", "sha256:old",
           ann={KEY_TYPE: TYPE_FRONTEND, KEY_ALIAS: "admin", KEY_CHANGELOG: "旧"}),
        mk("callfans/web-admin", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:new",
           ann={KEY_TYPE: TYPE_FRONTEND, KEY_ALIAS: "admin", KEY_CHANGELOG: "新首页"}),
    ]})
    plan = run_checker(harbor, state=state)
    assert len(plan.pending) == 1
    item = plan.pending[0]
    assert (item.type, item.old, item.new, item.alias, item.changelog) == (
        TYPE_FRONTEND, "20260901102030-abc1234", "20260912132921-123a066", "admin", "新首页"
    )


def test_frontend_alias_empty_falls_back_to_repo_name(tmp_path):
    harbor = FakeHarbor(repos={"callfans/portal": [
        mk("callfans/portal", "20260912132921-123a066", "2026-09-12T13:29:21Z",
           ann={KEY_TYPE: TYPE_FRONTEND}),  # 无 alias
    ]})
    plan = run_checker(harbor, state=StateStore(tmp_path / "state.json"))
    assert plan.pending[0].alias == "portal"


def test_frontend_first_install_no_state(tmp_path):
    harbor = FakeHarbor(repos={"callfans/web-admin": [
        mk("callfans/web-admin", "20260912132921-123a066", "2026-09-12T13:29:21Z",
           ann={KEY_TYPE: TYPE_FRONTEND, KEY_ALIAS: "admin"}),
    ]})
    plan = run_checker(harbor, state=StateStore(tmp_path / "state.json"))
    item = plan.pending[0]
    assert item.old is None and item.new == "20260912132921-123a066"


# ---------- sql ----------


def test_sql_backlog_ascending(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.set("sql", {"callfans/db": {"applied": [
        {"tag": "20260901102030-abc1234", "digest": "sha256:1"},
    ]}})
    harbor = FakeHarbor(repos={"callfans/db": [
        mk("callfans/db", "20260901102030-abc1234", "2026-09-01T10:20:30Z", "sha256:1",
           ann={KEY_TYPE: TYPE_SQL, KEY_CHANGELOG: "已执行"}),
        mk("callfans/db", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:3",
           ann={KEY_TYPE: TYPE_SQL, KEY_CHANGELOG: "加表"}),
        mk("callfans/db", "20260910150000-def5678", "2026-09-10T15:00:00Z", "sha256:2",
           ann={KEY_TYPE: TYPE_SQL, KEY_CHANGELOG: "加列"}),
    ]})
    plan = run_checker(harbor, state=state)
    item = plan.pending[0]
    assert item.type == TYPE_SQL
    assert item.old == "20260901102030-abc1234"
    # 未执行的按时间升序
    assert item.new == ["20260910150000-def5678", "20260912132921-123a066"]
    assert item.changelog["20260910150000-def5678"] == "加列"


def test_sql_all_applied_no_pending(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.set("sql", {"callfans/db": {"applied": [
        {"tag": "20260901102030-abc1234", "digest": "sha256:1"},
        {"tag": "20260912132921-123a066", "digest": "sha256:2"},
    ]}})
    harbor = FakeHarbor(repos={"callfans/db": [
        mk("callfans/db", "20260901102030-abc1234", "2026-09-01T10:20:30Z", "sha256:1", ann={KEY_TYPE: TYPE_SQL}),
        mk("callfans/db", "20260912132921-123a066", "2026-09-12T13:29:21Z", "sha256:2", ann={KEY_TYPE: TYPE_SQL}),
    ]})
    plan = run_checker(harbor, state=state)
    assert plan.pending == []


# ---------- 其他 ----------


def test_repo_with_only_excluded_tags_skipped(tmp_path):
    harbor = FakeHarbor(repos={"callfans/misc": [
        mk("callfans/misc", "latest", "2026-09-12T13:29:21Z"),
        mk("callfans/misc", "dev", "2026-09-12T13:29:21Z"),
    ]})
    plan = run_checker(harbor, state=StateStore(tmp_path / "state.json"))
    assert plan.pending == []


def test_plan_persisted_to_state(tmp_path):
    harbor = FakeHarbor(repos={"callfans/web": [
        mk("callfans/web", "20260912132921-123a066", "2026-09-12T13:29:21Z",
           ann={KEY_TYPE: TYPE_FRONTEND}),
    ]})
    state = StateStore(tmp_path / "state.json")
    plan = run_checker(harbor, state=state)
    st2 = StateStore(tmp_path / "state.json")
    assert st2.last_plan["pending"][0]["name"] == "callfans/web"
    assert st2.last_plan["checked_at"] == plan.checked_at
