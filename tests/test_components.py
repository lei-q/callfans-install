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


def test_recover_backfills_missing_tag_vars(compose_file, tmp_path, monkeypatch):
    """恢复前补齐缺失 tag 变量：本地无镜像时从 Harbor 取最新 tag 写入 .env。

    2026-10-19 实测：首装后 .env 无 *_TAG，compose up 插值出空 tag →
    invalid reference format。"""
    from callfans.core.components import _backfill_missing_tag_vars
    from tests.test_checker import mk

    class FakeHarbor:
        def repo_tags(self, repo):
            # 只对 rpa 给版本 tag（其余仓库空 → 不写）
            if repo == "callfans/callfans-rpa":
                return [
                    mk(repo, "202609070203-aaa", "2026-09-07T02:03:00Z"),
                    mk(repo, "20260919024815-46a4ed8", "2026-09-19T02:48:15Z"),
                ]
            return []

    monkeypatch.setattr("callfans.core.local.docker_images", lambda: [])
    env_path = tmp_path / ".env"
    env_path.write_text("HARBOR_REGISTRY=47.87.66.98\n", encoding="utf-8")

    cfg = make_cfg(compose_file=compose_file)
    # compose 模板里只有 rpa 用 ${VAR} 形式 tag（COMPOSE 里没有 → 造一个）
    compose_file.write_text(COMPOSE + (
        "  callfans-rpa:\n"
        "    image: ${HARBOR_REGISTRY}/callfans/callfans-rpa:${CALLFANS_RPA_TAG}\n"
    ), encoding="utf-8")

    changed = _backfill_missing_tag_vars(cfg, compose_file, harbor=FakeHarbor())
    assert changed == ["CALLFANS_RPA_TAG"]
    from callfans.core.updaters.compose_env import read_env
    assert read_env(env_path)["CALLFANS_RPA_TAG"] == "20260919024815-46a4ed8"  # 最新

    # 已有值不覆盖（现场手动改过 tag 不动）
    write_env = read_env(env_path)
    from callfans.core.updaters.compose_env import write_env as _write
    _write(env_path, {"CALLFANS_RPA_TAG": "202609070203-aaa"})
    changed2 = _backfill_missing_tag_vars(cfg, compose_file, harbor=FakeHarbor())
    assert changed2 == []
    assert read_env(env_path)["CALLFANS_RPA_TAG"] == "202609070203-aaa"


def test_fix_dir_placeholder_mounts(tmp_path):
    """挂载占位目录修复：xray 三件套是目录 → 删占位、拷回种子；目录挂载不动。"""
    import os
    from callfans.core.components import _fix_dir_placeholder_mounts

    deploy = tmp_path / "deploy"
    (deploy / "server/xray").mkdir(parents=True)
    compose = deploy / "docker-compose.yml"
    compose.write_text(
        "services:\n  xray-core:\n    volumes:\n"
        "      - ./server/xray/config.json:/etc/xray/config.json:rw\n"
        "      - ./server/xray/geoip.dat:/usr/local/bin/geoip.dat\n"
        "      - ./nginx/config:/etc/nginx/conf.d/\n",
        encoding="utf-8",
    )
    # 现场状态：config.json 是目录占位（docker 自动建的），geoip.dat 正常
    (deploy / "server/xray/config.json").mkdir()
    (deploy / "server/xray/geoip.dat").write_text("geo data", encoding="utf-8")
    # nginx/config 是合法目录挂载源（里面还有现场文件）
    (deploy / "nginx/config").mkdir(parents=True)
    (deploy / "nginx/config/live.conf").write_text("conf", encoding="utf-8")

    # 安装包种子
    app = tmp_path / "app"
    (app / "assets/xray").mkdir(parents=True)
    (app / "assets/xray/config.json").write_text("{}", encoding="utf-8")

    cfg = make_cfg(compose_file=compose, app_root=app)
    result = _fix_dir_placeholder_mounts(cfg, compose)
    assert result["fixed"] == [str(deploy / "server/xray/config.json")]
    assert result["blocked"] == []
    # 占位目录变回文件（内容来自种子）
    p = deploy / "server/xray/config.json"
    assert p.is_file() and p.read_text(encoding="utf-8") == "{}"
    # 正常文件与目录挂载原样
    assert (deploy / "server/xray/geoip.dat").read_text(encoding="utf-8") == "geo data"
    assert (deploy / "nginx/config/live.conf").exists()


def test_fix_mounts_ignores_unmanaged_dirs(tmp_path):
    """非安装包管理的挂载（数据目录等）：不看、不报（v0.9.2 误报 21 条的回归）。

    2026-10-20 修正：占位判据从「宿主侧是目录」改为「种子对位」——
    mysql/redis/minio 等数据目录挂载即使宿主侧是目录也完全不进结果。"""
    from callfans.core.components import _fix_dir_placeholder_mounts

    deploy = tmp_path / "deploy"
    (deploy / "mysql/data").mkdir(parents=True)      # 正常数据目录
    (deploy / "redis/config").mkdir(parents=True)    # 正常配置目录
    (deploy / "server/xray/config.json").mkdir(parents=True)  # 真占位
    compose = deploy / "docker-compose.yml"
    compose.write_text(
        "services:\n  db:\n    volumes:\n"
        "      - ./mysql/data:/var/lib/mysql\n"
        "      - ./redis/config/redis.conf:/etc/redis/redis.conf\n"
        "      - ./server/xray/config.json:/etc/xray/config.json\n",
        encoding="utf-8",
    )
    app = tmp_path / "app"
    (app / "assets/xray").mkdir(parents=True)
    (app / "assets/xray/config.json").write_text("{}", encoding="utf-8")

    cfg = make_cfg(compose_file=compose, app_root=app)
    result = _fix_dir_placeholder_mounts(cfg, compose)
    # 只修 xray 的占位；mysql/redis 数据目录不进 fixed 也不进 blocked
    assert result["fixed"] == [str(deploy / "server/xray/config.json")]
    assert result["blocked"] == []
    assert (deploy / "mysql/data").is_dir()  # 未被动过

    # 无 APP_ROOT：静默返回空（数据目录依旧不看）
    result2 = _fix_dir_placeholder_mounts(make_cfg(compose_file=compose), compose)
    assert result2 == {"fixed": [], "blocked": []}


def test_fix_mounts_rpa_xxljob_seed_mapping(tmp_path):
    """rpa / xxl_job 的占位修复：种子映射去掉 config/ 层。

    compose 挂载 ./server/rpa/config/config.yaml、
    ./server/xxl_job/config/application.properties；
    种子为 assets/rpa/config.yaml、assets/xxl_job/application.properties。"""
    from callfans.core.components import _fix_dir_placeholder_mounts

    deploy = tmp_path / "deploy"
    (deploy / "server/rpa/config").mkdir(parents=True)
    (deploy / "server/xxl_job/config").mkdir(parents=True)
    compose = deploy / "docker-compose.yml"
    compose.write_text(
        "services:\n  rpa:\n    volumes:\n"
        "      - ./server/rpa/config/config.yaml:/app/rpa/config.yaml\n"
        "      - ./server/xxl_job/config/application.properties:/config/application.properties\n",
        encoding="utf-8",
    )
    # 两处都是目录占位
    (deploy / "server/rpa/config/config.yaml").mkdir()
    (deploy / "server/xxl_job/config/application.properties").mkdir()

    app = tmp_path / "app"
    (app / "assets/rpa").mkdir(parents=True)
    (app / "assets/rpa/config.yaml").write_text("mysql:", encoding="utf-8")
    (app / "assets/xxl_job").mkdir(parents=True)
    (app / "assets/xxl_job/application.properties").write_text("name=job", encoding="utf-8")

    cfg = make_cfg(compose_file=compose, app_root=app)
    result = _fix_dir_placeholder_mounts(cfg, compose)
    assert result["blocked"] == []
    assert len(result["fixed"]) == 2
    p1 = deploy / "server/rpa/config/config.yaml"
    p2 = deploy / "server/xxl_job/config/application.properties"
    assert p1.is_file() and p1.read_text(encoding="utf-8") == "mysql:"
    assert p2.is_file() and p2.read_text(encoding="utf-8") == "name=job"
