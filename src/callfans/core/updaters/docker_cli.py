"""docker / docker compose 命令封装（统一 CLI 子进程通道，§2 架构决策）。

粒度设计为可注入的"原语"方法（inspect/stop/rename/up …），
server_updater 的回滚编排依赖这些原语，测试用 Fake 注入。
"""

from __future__ import annotations

import json
import json
import logging
import os
import re
import subprocess

from ..procs import run as proc_run

log = logging.getLogger(__name__)

_VAR_WARNING_RE = re.compile(r'The \\"(\w+)\\" variable is not set')  # docker logfmt 转义内层引号


class DockerError(RuntimeError):
    pass


class DockerCLI:
    def __init__(self):
        # compose 会用 shell 环境变量覆盖 .env 注入（优先级高于 --env-file），
        # 因此执行 compose 时剔除我们管理的 tag 变量，保证 .env 是唯一事实源
        self.env_exclusions: set[str] = set()

    # ---------- 基础 ----------

    def _raw(self, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        cmd = ["docker", *args]
        env = {k: v for k, v in os.environ.items() if k not in self.env_exclusions}
        try:
            return proc_run(cmd, timeout=timeout, env=env)  # CREATE_NO_WINDOW，防黑窗
        except FileNotFoundError as e:
            raise DockerError("未找到 docker 命令") from e
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker {' '.join(args[:3])} 超时") from e

    def _run(self, args: list[str], timeout: int = 300) -> str:
        proc = self._raw(args, timeout)
        if proc.returncode != 0:
            # stderr 可能为 None（Windows 控制台句柄差异），空时带出 stdout；
            # 过滤 level=warning 行（compose 的变量告警会淹没真实错误），保留尾部
            detail = self._failure_detail(proc.stderr, proc.stdout)
            raise DockerError(f"docker {' '.join(args[:4])} 失败: {detail}")
        return proc.stdout or ""

    @staticmethod
    def _failure_detail(stderr: str | None, stdout: str | None) -> str:
        def drop_warnings(text: str) -> str:
            return "\n".join(
                line for line in (text or "").splitlines() if "level=warning" not in line
            ).strip()

        detail = drop_warnings(stderr) or drop_warnings(stdout)
        return detail[-300:] if detail else "(无输出)"

    def compose(self, compose_file, *args: str, timeout: int = 300) -> str:
        return self._run(["compose", "-f", str(compose_file), *args], timeout=timeout)

    # ---------- 原语 ----------

    def version_ok(self) -> bool:
        """docker daemon 可达性检查。"""
        self._run(["version", "--format", "{{.Server.Version}}"])
        return True

    def login(self, registry: str, username: str, password: str) -> None:
        """docker login（密码经 stdin，不进进程列表）。

        pull 认证走 daemon 侧凭据，与服务自身的 Harbor API 账号无关：
        私有仓库（如 47.87.66.98）必须先 login 才能 pull。
        """
        try:
            proc = proc_run(
                ["docker", "login", registry, "-u", username, "--password-stdin"],
                input=password, timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker login {registry} 超时") from e
        if proc.returncode != 0:
            raise DockerError(
                f"docker login {registry} 失败: {self._failure_detail(proc.stderr, proc.stdout)}"
            )

    def compose_config(self, compose_file) -> dict:
        return self.compose_config_checked(compose_file)[0]

    def compose_config_checked(self, compose_file) -> tuple[dict, list[str]]:
        """返回 (配置, 未定义变量列表)。preflight 用后者提前拦截 .env 缺变量。"""
        proc = self._raw(["compose", "-f", str(compose_file), "config", "--format", "json"])
        if proc.returncode != 0:
            raise DockerError(f"compose 解析失败: {self._failure_detail(proc.stderr, proc.stdout)}")
        output = (proc.stderr or "") + (proc.stdout or "")
        warnings = sorted(set(_VAR_WARNING_RE.findall(output)))
        try:
            config = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as e:
            raise DockerError(f"compose config 输出解析失败: {e}") from e
        return config, warnings

    def compose_ps(self, compose_file) -> list[dict]:
        out = self.compose(compose_file, "ps", "-a", "--format", "json").strip()
        if not out:
            return []
        try:
            data = json.loads(out)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:  # 旧版本逐行输出
            return [json.loads(line) for line in out.splitlines() if line.strip()]

    def compose_up(self, compose_file, service: str, force_recreate: bool = False) -> None:
        # --no-deps：只重建目标服务，不动其他服务
        args = ["up", "-d", "--no-deps"]
        if force_recreate:
            args.append("--force-recreate")
        self.compose(compose_file, *args, service, timeout=600)

    def containers_by_service(self, service: str) -> list[str]:
        """按 compose service 标签过滤容器名（项目无关，比 compose ps 可靠）。"""
        out = self._run(
            ["ps", "-a", "--format", "{{.Names}}",
             "--filter", f"label=com.docker.compose.service={service}"]
        ) or ""
        return [n for n in out.splitlines() if n.strip()]

    def all_container_names(self) -> list[str]:
        out = self._run(["ps", "-a", "--format", "{{.Names}}"]) or ""
        return [n for n in out.splitlines() if n.strip()]

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
        proc = self._raw(["logs", "--tail", str(tail), name])
        # 容器日志可能分布在 stdout 与 stderr，合并返回
        return (proc.stdout or "") + (proc.stderr or "")

    def containers_using_image(self, image_id: str) -> list[str]:
        out = self._run(["ps", "-a", "-q", "--filter", f"ancestor={image_id}"]) or ""
        return [line for line in out.splitlines() if line.strip()]
