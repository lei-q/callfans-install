"""preflight：docker login 失败后按 Harbor 管理面探测结果给出分层指引。

2026-11-10 现场：47 仓库 admin/Callfans@123 实测有效，但 Windows 机器上
docker login 401——先分清"凭据不对"还是"daemon 侧网络不对"。
"""

import pytest

from callfans.core.models import PendingItem, UpdatePlan
from callfans.core.updaters import preflight as pf
from callfans.core.updaters.docker_cli import DockerError
from callfans.core.updaters.preflight import PreflightError, preflight
from tests.test_checker import make_cfg


class FakeDocker:
    def __init__(self):
        self.login_args = None

    def version_ok(self):
        return True

    def compose_config_checked(self, compose_file):
        return {}, []

    def registry_secure_map(self):
        return {"47.87.66.98": False}

    def login(self, registry, username, password):
        self.login_args = (registry, username, password)
        raise DockerError(
            f"docker login {registry}（用户 {username}）失败: "
            "Error response from daemon: login attempt to http://47.87.66.98/v2/ "
            "failed with status: 401 Unauthorized"
        )


def _plan():
    return UpdatePlan(checked_at="2026-11-10T00:00:00+00:00", pending=[
        PendingItem(name="callfans/api", type="server", old="t-old", new="t-new"),
    ])


def _cfg(tmp_path, api_url="http://47.87.66.98"):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  api:\n    image: ${HARBOR_REGISTRY}/callfans/api:${API_TAG}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("HARBOR_REGISTRY=47.87.66.98\n", encoding="utf-8")
    return make_cfg(harbor_api_url=api_url,
                    harbor_password="S3cret!pass", compose_file=compose)


def _run(tmp_path, monkeypatch, verdict, api_url="http://47.87.66.98"):
    monkeypatch.setattr(pf, "_credential_verdict", lambda cfg: verdict)
    monkeypatch.setattr(pf, "env_file", lambda: tmp_path / ".env")
    with pytest.raises(PreflightError) as ei:
        preflight(_cfg(tmp_path, api_url), _plan(), docker=FakeDocker())
    return str(ei.value)


def test_login_failure_with_invalid_credentials(tmp_path, monkeypatch):
    msg = _run(tmp_path, monkeypatch, False)
    assert "用户 u" in msg  # 带出实际登录用户名，便于发现现场旧值
    assert "Harbor 管理面同样 401" in msg
    assert str(tmp_path / ".env") in msg
    assert "S3cret!pass" not in msg  # 不泄露密码


def test_login_failure_with_daemon_side_network(tmp_path, monkeypatch):
    msg = _run(tmp_path, monkeypatch, True)
    assert "凭据有效" in msg
    assert "Docker daemon 侧" in msg
    assert "noProxy" in msg


def test_login_failure_when_verdict_unknown(tmp_path, monkeypatch):
    msg = _run(tmp_path, monkeypatch, None)
    assert "无法判定" in msg


def test_login_failure_registry_not_same_as_api_host(tmp_path, monkeypatch):
    """多台 Harbor 共存时不得用另一台的凭据结论误导现场。"""

    def boom(cfg):
        raise AssertionError("不同主机的管理面不应被探测")

    monkeypatch.setattr(pf, "_credential_verdict", boom)
    monkeypatch.setattr(pf, "env_file", lambda: tmp_path / ".env")
    with pytest.raises(PreflightError) as ei:
        preflight(_cfg(tmp_path, api_url="http://172.25.1.220"), _plan(),
                  docker=FakeDocker())
    msg = str(ei.value)
    assert "不是同一台" in msg
    assert "HARBOR_REGISTRY" in msg
