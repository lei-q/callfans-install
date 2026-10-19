"""components.py：功能组件清单、状态采集与一键恢复（2026-10-19）。"""

from pathlib import Path

import pytest

from callfans.core.components import list_components, recover_components
from callfans.core.updaters.docker_cli import DockerError

from tests.test_checker import make_cfg

COMPOSE = """\
services:
  callfans-db:
    image: ${HARBOR_REGISTRY}/callfans/mysql:8.0.24
    ports:
      - "3306:3306"
  redis:
    image: ${HARBOR_REGISTRY}/callfans/redis:5.0.14
  xray-core:
    image: ${HARBOR_REGISTRY}/callfans/teddysun/xray:latest
    ports:
      - "10000-10100:10000-10100"
"""


class FakeDocker:
    def __init__(self, containers: dict[str, dict], up_error: dict[str, str] | None = None):
        # containers: service -> state dict（Running/Health/ExitCode/Restarting）
        self.containers = containers
        self.up_error = up_error or {}
        self.ups: list[str] = []

    def compose_config(self, f):
        return {"services": {
            "callfans-db": {"image": "47.87.66.98/callfans/mysql:8.0.24",
                            "ports": ["3306:3306"]},
            "redis": {"image": "47.87.66.98/callfans/redis:5.0.14"},
            "xray-core": {"image": "47.87.66.98/callfans/teddysun/xray:latest",
                          "ports": ["10000-10100:10000-10100"]},
        }}

    def containers_by_service(self, service):
        return [f"cf-{service}-1"] if service in self.containers else []

    def inspect_container(self, name):
        service = name.removeprefix("cf-").removesuffix("-1")
        c = self.containers[service]
        state = {"Running": c.get("Running", False),
                 "Restarting": c.get("Restarting", False),
                 "ExitCode": c.get("ExitCode", 0),
                 "Health": c.get("Health")}
        return {"Name": name, "State": state}

    def compose_up(self, f, service, force_recreate=False, on_line=None):
        if service in self.up_error:
            raise DockerError(self.up_error[service])
        self.ups.append(service)


@pytest.fixture
def compose_file(tmp_path):
    f = tmp_path / "docker-compose.yml"
    f.write_text(COMPOSE, encoding="utf-8")
    return f


def test_status_running_unhealthy_exited_missing(compose_file, tmp_path):
    """状态采集：running / unhealthy / exited / missing 各归其位。"""
    fake = FakeDocker({
        "callfans-db": {"Running": True, "Health": {"Status": "healthy"}},
        "redis": {"Running": True, "Health": {"Status": "unhealthy"}},
        # xray-core 不在 containers → missing
    })
    # 补一个退出态：callfans-db exited 放第二个服务验证
    cfg = make_cfg(compose_file=compose_file)
    comps = {c["service"]: c for c in list_components(cfg, fake)}
    assert comps["callfans-db"]["status"] == "running"
    assert comps["callfans-db"]["abnormal"] is False
    assert comps["redis"]["status"] == "unhealthy"
    assert comps["redis"]["abnormal"] is True
    assert comps["xray-core"]["status"] == "missing"
    assert comps["xray-core"]["abnormal"] is True
    assert "3306" in " ".join(comps["callfans-db"]["ports"])


def test_exited_and_restarting_states(compose_file):
    fake = FakeDocker({
        "callfans-db": {"Running": False, "ExitCode": 137},
        "redis": {"Running": True, "Restarting": True},
    })
    cfg = make_cfg(compose_file=compose_file)
    comps = {c["service"]: c for c in list_components(cfg, fake)}
    assert comps["callfans-db"]["status"] == "exited"
    assert "137" in comps["callfans-db"]["status_text"]
    assert comps["redis"]["status"] == "restarting"


def test_recover_only_lists(compose_file):
    """一键恢复：全部异常服务 compose up；成功/失败分账。"""
    fake = FakeDocker({"redis": {"Running": True}}, up_error={"xray-core": "port 冲突"})
    cfg = make_cfg(compose_file=compose_file)
    report = recover_components(cfg, ["redis", "xray-core", "ghost"], docker=fake)
    assert report["recovered"] == ["redis"]
    assert report["failed"] == ["xray-core", "ghost"]
    assert "port 冲突" in report["errors"]["xray-core"]
    assert "无此服务" in report["errors"]["ghost"]
    assert fake.ups == ["redis"]  # 成功的才记录；失败(xray-core)在 raise 前不记录，ghost 不执行 up


def test_compose_unavailable_returns_empty(tmp_path):
    """compose 解析失败 → 空列表（UI 显示不可用而非崩溃）。"""

    class BrokenDocker(FakeDocker):
        def compose_config(self, f):
            raise DockerError("docker 不可用")

    cfg = make_cfg(compose_file=tmp_path / "nope.yml")
    assert list_components(cfg, BrokenDocker({})) == []
