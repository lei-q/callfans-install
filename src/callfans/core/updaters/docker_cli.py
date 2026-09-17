"""docker / docker compose 命令封装（统一 CLI 子进程通道，§2 架构决策）。

粒度设计为可注入的"原语"方法（inspect/stop/rename/up …），
server_updater 的回滚编排依赖这些原语，测试用 Fake 注入。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from ..procs import run as proc_run

log = logging.getLogger(__name__)


class DockerError(RuntimeError):
    pass


class DockerCLI:
    def __init__(self):
        # compose 会用 shell 环境变量覆盖 .env 注入（优先级高于 --env-file），
        # 因此执行 compose 时剔除我们管理的 tag 变量，保证 .env 是唯一事实源
        self.env_exclusions: set[str] = set()

    # ---------- 基础 ----------

    def _run(self, args: list[str], timeout: int = 300) -> str:
        cmd = ["docker", *args]
        env = {k: v for k, v in os.environ.items() if k not in self.env_exclusions}
        try:
            proc = proc_run(cmd, timeout=timeout, env=env)  # CREATE_NO_WINDOW，防黑窗
        except FileNotFoundError as e:
            raise DockerError("未找到 docker 命令") from e
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker {' '.join(args[:3])} 超时") from e
        if proc.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:4])} 失败: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def compose(self, compose_file, *args: str, timeout: int = 300) -> str:
        return self._run(["compose", "-f", str(compose_file), *args], timeout=timeout)

    # ---------- 原语 ----------

    def version_ok(self) -> bool:
        """docker daemon 可达性检查。"""
        self._run(["version", "--format", "{{.Server.Version}}"])
        return True

    def compose_config(self, compose_file) -> dict:
        return json.loads(self.compose(compose_file, "config", "--format", "json"))

    def compose_ps(self, compose_file) -> list[dict]:
        out = self.compose(compose_file, "ps", "-a", "--format", "json").strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:  # 旧版本逐行输出
            return [json.loads(line) for line in out.splitlines() if line.strip()]

    def compose_up(self, compose_file, service: str) -> None:
        # --no-deps：只重建目标服务，不动其他服务
        self.compose(compose_file, "up", "-d", "--no-deps", service, timeout=600)

    def inspect_container(self, name: str) -> dict:
        return json.loads(self._run(["inspect", name]))[0]

    def inspect_image(self, ref: str) -> dict:
        return json.loads(self._run(["image", "inspect", ref]))[0]

    def container_exists(self, name: str) -> bool:
        try:
            self.inspect_container(name)
            return True
        except DockerError:
            return False

    def stop(self, name: str, timeout: int) -> None:
        self._run(["stop", "-t", str(timeout), name])

    def start(self, name: str) -> None:
        self._run(["start", name])

    def rename(self, old: str, new: str) -> None:
        self._run(["rename", old, new])

    def rm(self, name: str, force: bool = False) -> None:
        args = ["rm", "-f", name] if force else ["rm", name]
        self._run(args)

    def rmi(self, ref: str) -> None:
        self._run(["rmi", ref], timeout=120)

    def pull(self, ref: str) -> None:
        self._run(["pull", ref], timeout=1800)

    def logs(self, name: str, tail: int = 30) -> str:
        return self._run(["logs", "--tail", str(tail), name])

    def containers_using_image(self, image_id: str) -> list[str]:
        out = self._run(["ps", "-a", "-q", "--filter", f"ancestor={image_id}"])
        return [line for line in out.splitlines() if line.strip()]
