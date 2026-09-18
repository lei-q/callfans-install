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


def test_pull_streams_lines_to_callback(monkeypatch):
    """docker pull 流式：进度行实时回调，失败时带尾部输出。"""
    import callfans.core.updaters.docker_cli as dc

    class _Stream:
        def __init__(self, lines):
            self._it = iter(lines)

        def readline(self):
            try:
                return next(self._it)
            except StopIteration:
                return b""

        def close(self):
            pass

    lines = [
        b"aaa: Pulling fs layer\n",
        b"aaa: Downloading [===>   ] 12.3MB/65.5MB\n",
        b"aaa: Pull complete\n",
        b"Status: Downloaded newer image\n",
    ]

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = _Stream(lines)

        def wait(self):
            return 0

    monkeypatch.setattr(dc.subprocess, "Popen", lambda *a, **k: FakeProc())
    got: list[str] = []
    out = DockerCLI().pull("reg/x:tag", on_line=got.append)
    assert "aaa: Pull complete" in got
    assert "Status: Downloaded newer image" in out

    class FailingProc(FakeProc):
        returncode = 1

        def wait(self):
            return 1

    lines.append(b"Error response from daemon: not found\n")
    monkeypatch.setattr(dc.subprocess, "Popen", lambda *a, **k: FailingProc())
    with pytest.raises(DockerError, match="not found"):
        DockerCLI().pull("reg/x:missing", on_line=None)
