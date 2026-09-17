"""docker_cli：失败输出为 None 时不崩溃且带出真实原因（v0.1.4 Windows 实测回归）。"""

import subprocess

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
