"""server 更新器：FakeDocker 模拟，覆盖成功清理与失败回滚两条关键路径。"""

import pytest

from callfans.core.local import DockerError, DockerImage, StateStore
from callfans.core.models import PendingItem, TYPE_SERVER
from callfans.core.updaters.compose_env import read_env, render_ref
from callfans.core.updaters.docker_cli import DockerError
from callfans.core.updaters.server_updater import ServerUpdater
from tests.test_checker import make_cfg

OLD = "20260901102030-abc1234"
NEW = "20260912132921-123a066"
TPL = "harbor.example.com/callfans/api:${API_TAG}"


class FakeDocker:
    """按 server_updater 用到的原语模拟 docker 行为（内存态）。"""

    def __init__(self, compose_file, templates: dict[str, str], fail_up=False, health="healthy"):
        self.compose_file = compose_file
        self.templates = templates
        self.fail_up = fail_up
        self.health = health
        self.containers: dict[str, dict] = {}
        self.images: dict[str, str] = {}
        self.env_exclusions: set[str] = set()
        self.actions: list[str] = []
        self._n = 0

    def _rendered(self, service):
        from callfans.core.updaters.compose_env import extract_tag_var

        tpl = self.templates[service]
        var = extract_tag_var(tpl)
        if var is None:
            return tpl
        env = read_env(self.compose_file.parent / ".env")
        return render_ref(tpl, env.get(var, "latest"), env=env)

    def compose_config(self, f):
        return {"services": {svc: {"image": self._rendered(svc)} for svc in self.templates}}

    def compose_ps(self, f):
        return [{"Name": n, "Service": c["service"]} for n, c in self.containers.items()]

    def compose_up(self, f, service):
        self.actions.append(f"up:{service}")
        ref = self._rendered(service)
        image_id = self.images.get(ref)
        if image_id is None:
            raise DockerError(f"pull required for {ref}")
        name = f"proj-{service}-1"
        self.containers.pop(name, None)  # compose 用同名重建
        self.containers[name] = {
            "service": service, "image_id": image_id, "running": not self.fail_up,
        }

    def inspect_container(self, name):
        c = self.containers[name]
        state = {"Running": c["running"], "Restarting": False, "RestartCount": 0,
                 "ExitCode": 0 if c["running"] else 1}
        if c["running"] and self.health:
            state["Health"] = {"Status": self.health}
        return {"Name": name, "Image": c["image_id"], "State": state}

    def inspect_image(self, ref):
        if ref not in self.images:
            raise DockerError(f"no such image: {ref}")
        return {"Id": self.images[ref]}

    def container_exists(self, name):
        return name in self.containers

    def stop(self, name, timeout):
        self.actions.append(f"stop:{name}")
        self.containers[name]["running"] = False

    def rename(self, old, new):
        self.actions.append(f"rename:{old}->{new}")
        self.containers[new] = self.containers.pop(old)

    def rm(self, name, force=False):
        self.actions.append(f"rm:{name}")
        self.containers.pop(name, None)

    def rmi(self, ref):
        self.actions.append(f"rmi:{ref}")
        self.images = {r: i for r, i in self.images.items() if i != ref and r != ref}

    def pull(self, ref):
        self.actions.append(f"pull:{ref}")
        self._n += 1
        self.images[ref] = f"img-{self._n}"

    def start(self, name):
        self.actions.append(f"start:{name}")
        self.containers[name]["running"] = True

    def containers_using_image(self, image_id):
        return [n for n, c in self.containers.items() if c["image_id"] == image_id]

    def logs(self, name, tail=30):
        return "INFO starting\nERROR boom"

    def version_ok(self):
        return True


@pytest.fixture
def compose_file(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(f"services:\n  api:\n    image: {TPL}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"API_TAG={OLD}\n", encoding="utf-8")
    return f


def make_item():
    return PendingItem(name="callfans/api", type=TYPE_SERVER, old=OLD, new=NEW, digest_new="sha256:new")


def seed_running_stack(fake: FakeDocker):
    """预置：旧镜像已 pull，旧容器运行中。"""
    old_ref = render_ref(TPL, OLD)
    fake.pull(old_ref)
    fake.containers["proj-api-1"] = {"service": "api", "image_id": fake.images[old_ref], "running": True}
    return fake.images[old_ref]


def test_success_flow(compose_file, tmp_path):
    fake = FakeDocker(compose_file, {"api": TPL})
    old_image_id = seed_running_stack(fake)
    cfg = make_cfg(compose_file=compose_file, health_wait_seconds=2, docker_stop_timeout=1)
    state = StateStore(tmp_path / "state.json")

    record = ServerUpdater(cfg, state, fake).update(make_item())

    assert record["result"] == "success", record["error"]
    env = read_env(compose_file.parent / ".env")
    assert env["API_TAG"] == NEW
    # 旧容器清理、新容器在跑
    assert "proj-api-1-callfans-old" not in fake.containers
    assert fake.containers["proj-api-1"]["running"] is True
    # 旧镜像删除（无引用）、新镜像存在
    assert old_image_id not in fake.images.values()
    assert render_ref(TPL, NEW) in fake.images
    # 关键动作齐全
    joined = " ".join(fake.actions)
    assert "stop:proj-api-1" in joined
    assert "rename:proj-api-1->proj-api-1-callfans-old" in joined
    assert f"pull:{render_ref(TPL, NEW)}" in joined
    # state 记账
    assert state.get("server")["callfans/api"]["tag"] == NEW


def test_failure_rolls_back(compose_file, tmp_path):
    fake = FakeDocker(compose_file, {"api": TPL}, fail_up=True)  # 新容器启动即退出
    seed_running_stack(fake)
    cfg = make_cfg(compose_file=compose_file, health_wait_seconds=2, docker_stop_timeout=1)
    state = StateStore(tmp_path / "state.json")

    record = ServerUpdater(cfg, state, fake).update(make_item())

    assert record["result"] == "rolled_back"
    assert "未在运行" in record["error"]
    assert "boom" in record["error"]  # 带回日志尾部
    # .env 写回旧 tag
    assert read_env(compose_file.parent / ".env")["API_TAG"] == OLD
    # 旧容器恢复原名并重启
    assert fake.containers["proj-api-1"]["running"] is True
    assert "proj-api-1-callfans-old" not in fake.containers
    assert "start:proj-api-1" in " ".join(fake.actions)
    # 新容器已移除
    assert len([n for n in fake.containers if n != "proj-api-1"]) == 0
    # 失败不记账
    assert state.get("server") is None


def test_first_deploy_no_old_container(compose_file, tmp_path):
    fake = FakeDocker(compose_file, {"api": TPL})
    cfg = make_cfg(compose_file=compose_file, health_wait_seconds=2)
    state = StateStore(tmp_path / "state.json")

    record = ServerUpdater(cfg, state, fake).update(make_item())

    assert record["result"] == "success", record["error"]
    assert read_env(compose_file.parent / ".env")["API_TAG"] == NEW
    assert fake.containers["proj-api-1"]["running"] is True


def test_compose_image_without_var_rejected(compose_file, tmp_path):
    fixed = "harbor.example.com/callfans/api:20260901102030-abc1234"
    compose_file.write_text(f"services:\n  api:\n    image: {fixed}\n", encoding="utf-8")
    fake = FakeDocker(compose_file, {"api": fixed})
    cfg = make_cfg(compose_file=compose_file)
    record = ServerUpdater(cfg, None, fake).update(make_item())
    assert record["result"] == "failed"
    assert "${VAR}" in record["error"]


def test_registry_var_template(compose_file, tmp_path):
    """镜像地址含 ${HARBOR_REGISTRY} 变量：pull 引用须用 .env 值解析，不得残留变量。"""
    tpl = "${HARBOR_REGISTRY}/callfans/api:${API_TAG}"
    compose_file.write_text(f"services:\n  api:\n    image: {tpl}\n", encoding="utf-8")
    (compose_file.parent / ".env").write_text(
        f"HARBOR_REGISTRY=harbor.example.com\nAPI_TAG={OLD}\n", encoding="utf-8"
    )
    fake = FakeDocker(compose_file, {"api": tpl})
    old_ref = f"harbor.example.com/callfans/api:{OLD}"
    fake.pull(old_ref)
    fake.containers["proj-api-1"] = {"service": "api", "image_id": fake.images[old_ref], "running": True}
    cfg = make_cfg(compose_file=compose_file, health_wait_seconds=2)
    record = ServerUpdater(cfg, StateStore(tmp_path / "state.json"), fake).update(make_item())
    assert record["result"] == "success", record["error"]
    joined = " ".join(fake.actions)
    assert f"pull:harbor.example.com/callfans/api:{NEW}" in joined  # 变量已解析
    assert "${" not in joined
    assert read_env(compose_file.parent / ".env")["API_TAG"] == NEW
    # 其他变量原样保留
    assert read_env(compose_file.parent / ".env")["HARBOR_REGISTRY"] == "harbor.example.com"


def test_registry_var_undefined_rejected(compose_file, tmp_path):
    """HARBOR_REGISTRY 未在 .env 定义 → 明确报错，不动容器。"""
    tpl = "${HARBOR_REGISTRY}/callfans/api:${API_TAG}"
    compose_file.write_text(f"services:\n  api:\n    image: {tpl}\n", encoding="utf-8")
    (compose_file.parent / ".env").write_text(f"API_TAG={OLD}\n", encoding="utf-8")
    fake = FakeDocker(compose_file, {"api": tpl})
    cfg = make_cfg(compose_file=compose_file)
    record = ServerUpdater(cfg, None, fake).update(make_item())
    assert record["result"] == "rolled_back"
    assert "未定义变量" in record["error"]


class TestBackfillTagVars:
    """*_TAG 不手写：缺失变量按本地当前版本自动补齐。"""

    COMPOSE_TEXT = (
        "services:\n"
        "  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n"
        "  worker:\n    image: ${HARBOR_REGISTRY}/callfans/worker:${WORKER_TAG}\n"
        "  db:\n    image: mysql:8.0.24\n"
    )

    def _patch_images(self, monkeypatch, rows):
        import callfans.core.updaters.server_updater as su

        monkeypatch.setattr(su, "docker_images", lambda: rows)

    def test_backfills_missing_only(self, compose_file, monkeypatch):
        compose_file.write_text(self.COMPOSE_TEXT, encoding="utf-8")
        env = compose_file.parent / ".env"
        env.write_text("HARBOR_REGISTRY=172.25.1.220\nAPI_TAG=EXISTING\n", encoding="utf-8")
        self._patch_images(monkeypatch, [
            DockerImage("172.25.1.220/callfans/api", "20260901102030-abc1234", "sha256:a"),
            DockerImage("172.25.1.220/callfans/worker", "20260910150000-def5678", "sha256:b"),
            DockerImage("mysql", "8.0.24", "sha256:m"),  # 非 Harbor 管理
        ])
        from callfans.core.updaters.server_updater import backfill_tag_vars

        filled = backfill_tag_vars(make_cfg(compose_file=compose_file))
        assert filled == ["WORKER_TAG"]
        values = read_env(env)
        assert values["WORKER_TAG"] == "20260910150000-def5678"
        assert values["API_TAG"] == "EXISTING"  # 已有不覆盖

    def test_picks_latest_local_tag(self, compose_file, monkeypatch):
        compose_file.write_text(self.COMPOSE_TEXT, encoding="utf-8")
        env = compose_file.parent / ".env"
        env.write_text("HARBOR_REGISTRY=x\n", encoding="utf-8")
        self._patch_images(monkeypatch, [
            DockerImage("172.25.1.220/callfans/api", "20260901102030-abc1234", "sha256:a"),
            DockerImage("172.25.1.220/callfans/api", "20260912132921-123a066", "sha256:b"),
        ])
        from callfans.core.updaters.server_updater import backfill_tag_vars

        backfill_tag_vars(make_cfg(compose_file=compose_file))
        assert read_env(env)["API_TAG"] == "20260912132921-123a066"  # 本地最高版本

    def test_docker_unavailable_silent(self, compose_file, monkeypatch):
        import callfans.core.updaters.server_updater as su

        compose_file.write_text(self.COMPOSE_TEXT, encoding="utf-8")
        env = compose_file.parent / ".env"
        env.write_text("HARBOR_REGISTRY=x\n", encoding="utf-8")
        monkeypatch.setattr(su, "docker_images", lambda: (_ for _ in ()).throw(DockerError("down")))
        from callfans.core.updaters.server_updater import backfill_tag_vars

        assert backfill_tag_vars(make_cfg(compose_file=compose_file)) == []
        assert "API_TAG" not in read_env(env)
