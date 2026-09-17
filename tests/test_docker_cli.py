"""docker_cli：失败输出为 None 时不崩溃且带出真实原因（v0.1.4 Windows 实测回归）。"""

import subprocess
from pathlib import Path

import pytest

from callfans.core.updaters import docker_cli as dc
from callfans.core.updaters.docker_cli import DockerCLI, DockerError


def _patch_proc(monkeypatch, returncode=0, stdout="", stderr=""):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(dc, "proc_run", fake_run)


def test_failure_with_none_outputs_does_not_crash(monkeypatch):
    """stderr/stdout 为 None（Windows 控制台句柄差异）不得抛 AttributeError。"""
    _patch_proc(monkeypatch, returncode=1, stdout=None, stderr=None)
    with pytest.raises(DockerError) as ei:
        DockerCLI()._run(["ps"])
    assert "失败" in str(ei.value)


def test_failure_prefers_stderr(monkeypatch):
    _patch_proc(monkeypatch, returncode=1, stdout="progress…", stderr="boom reason")
    with pytest.raises(DockerError, match="boom reason"):
        DockerCLI()._run(["pull", "x"])


def test_failure_falls_back_to_stdout(monkeypatch):
    """compose 有时把错误打到 stdout。"""
    _patch_proc(monkeypatch, returncode=1, stdout="Error response from daemon: nope", stderr="")
    with pytest.raises(DockerError, match="nope"):
        DockerCLI()._run(["compose", "-f", "x", "up"])


def test_logs_merges_stdout_and_stderr(monkeypatch):
    _patch_proc(monkeypatch, returncode=0, stdout="INFO line", stderr="ERROR line")
    assert DockerCLI().logs("c") == "INFO lineERROR line"


def test_failure_detail_filters_warnings_keeps_real_error(monkeypatch):
    """compose 失败时 warning 行淹没真实错误：过滤 warning、保留尾部真实错误。"""
    stderr = (
        'time="2026-09-17T18:48:33+08:00" level=warning msg="The \\"TIMEZONE\\" variable is not set."\n'
        'time="2026-09-17T18:48:33+08:00" level=warning msg="The \\"MYSQL_ROOT_PASSWORD\\" variable is not set."\n'
        "Error response from daemon: driver failed programming connectivity: port is already allocated"
    )
    _patch_proc(monkeypatch, returncode=1, stdout="", stderr=stderr)
    with pytest.raises(DockerError) as ei:
        DockerCLI()._run(["compose", "-f", "x", "up"])
    msg = str(ei.value)
    assert "port is already allocated" in msg
    assert "TIMEZONE" not in msg  # warning 被过滤


def test_compose_config_checked_collects_missing_vars(monkeypatch):
    _patch_proc(
        monkeypatch, returncode=0, stdout='{"services": {}}',
        stderr=(
            'time="x" level=warning msg="The \\"TIMEZONE\\" variable is not set. Defaulting to a blank string."\n'
            'time="x" level=warning msg="The \\"MYSQL_ROOT_PASSWORD\\" variable is not set."\n'
            'time="x" level=warning msg="The \\"TIMEZONE\\" variable is not set."\n'  # 重复
        ),
    )
    config, warnings = DockerCLI().compose_config_checked(Path("docker-compose.yml"))
    assert config == {"services": {}}
    assert warnings == ["MYSQL_ROOT_PASSWORD", "TIMEZONE"]
