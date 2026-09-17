"""server（docker 镜像）更新执行器（§6.1）。

流程：停旧容器并改名保留（-callfans-old）→ pull 新镜像 → 持久写 compose .env 的
tag 变量（Q5）→ compose up 重建 → 健康判定 → 成功删旧容器/旧镜像；失败回滚
（.env 写回旧 tag、删新容器、旧容器改回原名并 start）。
验证通过前绝不删除旧容器/旧镜像。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ...config import Config
from ..compare import is_version_tag, tag_timestamp
from ..local import DockerError, StateStore, docker_images, harbor_repo_of
from ..models import PendingItem
from .compose_env import (
    extract_tag_var, find_image_template, has_unresolved_vars, iter_image_templates,
    read_env, render_ref, strip_tag, strip_vars, write_env,
)
from .docker_cli import DockerCLI, DockerError

log = logging.getLogger(__name__)

_OLD_SUFFIX = "-callfans-old"
_VERIFY_POLL_SECONDS = 1.0
_STABLE_POLLS = 3


class UpdateFailure(RuntimeError):
    pass


_MIN = datetime.min.replace(tzinfo=timezone.utc)


def backfill_tag_vars(cfg: Config) -> list[str]:
    """compose .env 缺失的 *_TAG 变量按本地当前运行版本自动补齐。

    tag 一律不手写（2026-09-18 决策）：更新中的服务由更新器写入 Harbor 目标 tag；
    未更新/迁移中的服务按本地镜像当前最高版本 tag 回填，保证任意时刻手动
    `docker compose up` 都能完成插值。docker 不可用时静默跳过（不影响更新流程）。
    """
    if not cfg.compose_file:
        return []
    compose_file = Path(cfg.compose_file)
    try:
        text = compose_file.read_text(encoding="utf-8")
    except OSError:
        return []

    # 本地 repo → 最高版本 tag
    local_map: dict[str, str] = {}
    exclude = set(cfg.tag_exclude)
    try:
        for img in docker_images():
            repo = harbor_repo_of(img.repository, cfg.harbor_project)
            if repo and is_version_tag(img.tag, exclude):
                cur = local_map.get(repo)
                if cur is None or (tag_timestamp(img.tag) or _MIN) > (tag_timestamp(cur) or _MIN):
                    local_map[repo] = img.tag
    except DockerError:
        return []

    env_path = compose_file.parent / ".env"
    env = read_env(env_path)
    changed: dict[str, str] = {}
    for template in iter_image_templates(text):
        var = extract_tag_var(template)
        if var is None or env.get(var) or var in changed:
            continue
        repo = harbor_repo_of(strip_tag(strip_vars(template)), cfg.harbor_project)
        tag = local_map.get(repo)
        if tag:
            changed[var] = tag
    if changed:
        try:
            write_env(env_path, changed)
        except OSError as e:
            log.error("tag 变量回填写入失败: %s", e)
            return []
        log.info("已按本地当前版本补齐 compose tag 变量: %s", changed)
    return list(changed)


class ServerUpdater:
    def __init__(self, cfg: Config, state: StateStore | None = None,
                 docker: DockerCLI | None = None, on_event=None):
        self.cfg = cfg
        self.state = state
        self.docker = docker or DockerCLI()
        self.on_event = on_event or (lambda *a, **k: None)

    def update(self, item: PendingItem) -> dict:
        record = {
            "name": item.name, "type": "server", "old": item.old, "new": item.new,
            "result": "failed", "error": None,
        }
        if not self.cfg.compose_file:
            record["error"] = "COMPOSE_FILE 未配置"
            return record
        compose_file = Path(self.cfg.compose_file)
        try:
            text = compose_file.read_text(encoding="utf-8")
        except OSError as e:
            record["error"] = f"compose 文件读取失败: {e}"
            return record
        template = find_image_template(text, item.name, self.cfg.harbor_project)
        if template is None:
            record["error"] = f"compose.yml 未找到 {item.name} 的 image 定义"
            return record
        var = extract_tag_var(template)
        if var is None:
            record["error"] = f"{item.name} 的 image 未使用 ${{VAR}} 形式（Q5）: {template}"
            return record
        env_path = compose_file.parent / ".env"
        env_values = read_env(env_path)
        old_tag = env_values.get(var)

        try:
            cfg_json = self.docker.compose_config(compose_file)
        except DockerError as e:
            record["error"] = f"compose 解析失败: {e}"
            return record
        service = self._match_service(cfg_json, item.name)
        if service is None:
            record["error"] = f"compose 服务中未匹配到镜像 {item.name}"
            return record
        # 让 .env 成为 tag 唯一事实源（compose 调用时剔除 shell 环境变量覆盖）
        self.docker.env_exclusions.add(var)

        old_ctn = self._find_container(
            compose_file, service,
            container_name=(cfg_json.get("services", {}).get(service, {}) or {}).get("container_name"),
        )
        new_ctn: str | None = None
        env_written = False
        old_image_id: str | None = None
        try:
            if old_ctn:
                info = self.docker.inspect_container(old_ctn)
                old_image_id = info.get("Image")
                if info.get("State", {}).get("Running"):
                    self._emit(item, "stop_old", container=old_ctn)
                    self.docker.stop(old_ctn, self.cfg.docker_stop_timeout)
                # 已存在的容器先停再删（2026-09-18 决策：适配 container_name 固定名，
                # 旧容器可能属于其他 compose 项目，compose ps 不一定能找到）
                self.docker.rm(old_ctn)

            new_ref = render_ref(template, item.new, var, env_values)
            if has_unresolved_vars(new_ref):
                raise UpdateFailure(f"镜像引用存在未定义变量: {new_ref}（检查 compose 同目录 .env）")
            self._emit(item, "pull", ref=new_ref)
            self.docker.pull(new_ref)
            new_image_id = self.docker.inspect_image(new_ref).get("Id")

            write_env(env_path, {var: item.new})
            env_written = True
            self._emit(item, "up", service=service)
            self.docker.compose_up(compose_file, service)

            new_ctn = self._find_container(compose_file, service)
            if new_ctn is None:
                raise UpdateFailure("compose up 后未找到新容器")
            cinfo = self.docker.inspect_container(new_ctn)
            if cinfo.get("Image") != new_image_id:
                raise UpdateFailure("新容器未使用新镜像（image id 不符）")

            self._emit(item, "verify", wait=self.cfg.health_wait_seconds)
            self._verify(new_ctn)

            # 成功清理；清理失败只记 warning，不影响结果
            warns: list[str] = []
            if old_image_id:
                try:
                    if not self.docker.containers_using_image(old_image_id):
                        self.docker.rmi(old_image_id)
                except DockerError as e:
                    warns.append(f"旧镜像清理失败: {e}")
            self._record_state(item.name, item.new, new_image_id)
            record.update(result="success", error="; ".join(warns) or None)
            return record
        except Exception as e:
            reason = self._failure_reason(new_ctn, e)
            log.error("%s 更新失败: %s", item.name, reason)
            rb_error = self._rollback(env_path, var, old_tag, env_written, new_ctn, compose_file, service)
            record.update(
                result="rolled_back",
                error=reason + (f"；回滚失败: {rb_error}" if rb_error else "；已回滚"),
            )
            return record

    # ---------- 内部 ----------

    def _emit(self, item: PendingItem, stage: str, **extra) -> None:
        self.on_event("update_progress", {"item": item.name, "type": "server", "stage": stage, **extra})

    def _match_service(self, cfg_json: dict, repo_full: str) -> str | None:
        for svc, scfg in (cfg_json.get("services") or {}).items():
            image = scfg.get("image") or ""
            if harbor_repo_of(strip_tag(image), self.cfg.harbor_project) == repo_full:
                return svc
        return None

    def _find_container(self, compose_file: Path, service: str, container_name: str | None = None) -> str | None:
        """定位服务当前容器：先按 compose 项目查，再按固定 container_name 直查兜底。

        旧容器可能由别的 compose 项目 / 旧版 docker-compose 创建（本项目 ps 找不到），
        compose up 会因 container_name 冲突失败，必须先停删。
        """
        try:
            entries = self.docker.compose_ps(compose_file)
        except DockerError:
            entries = []
        for e in entries:
            name = e.get("Name") or e.get("Names") or e.get("ID")
            if not name or name.endswith(_OLD_SUFFIX):
                continue
            svc = e.get("Service") or (e.get("Labels") or {}).get("com.docker.compose.service")
            if svc == service:
                return name
        if container_name and self.docker.container_exists(container_name):
            return container_name
        return None

    def _verify(self, new_ctn: str) -> None:
        """运行成功判定：观察窗口内持续 Running、不重启循环；有 healthcheck 则 healthy。"""
        deadline = time.monotonic() + max(5, self.cfg.health_wait_seconds)
        stable = 0
        while time.monotonic() < deadline:
            state = self.docker.inspect_container(new_ctn).get("State", {})
            if not state.get("Running"):
                raise UpdateFailure(f"新容器未在运行（exit={state.get('ExitCode')}）")
            if state.get("Restarting") or state.get("RestartCount", 0) > 3:
                raise UpdateFailure("新容器处于重启循环")
            health = state.get("Health")
            if health:
                status = health.get("Status")
                if status == "unhealthy":
                    raise UpdateFailure("healthcheck unhealthy")
                if status == "healthy":
                    stable += 1
                    if stable >= 2:
                        return
            else:
                stable += 1
                if stable >= _STABLE_POLLS:
                    return
            time.sleep(_VERIFY_POLL_SECONDS)
        raise UpdateFailure(f"观察窗口 {self.cfg.health_wait_seconds}s 内未达到健康标准")

    def _failure_reason(self, new_ctn: str | None, exc: Exception) -> str:
        reason = str(exc) if isinstance(exc, (UpdateFailure, DockerError)) else f"{type(exc).__name__}: {exc}"
        if new_ctn:
            try:
                tail = [l for l in self.docker.logs(new_ctn, 20).strip().splitlines() if l.strip()][-5:]
                if tail:
                    reason += " | 日志尾部: " + " / ".join(tail)
            except DockerError:
                pass
        return reason

    def _rollback(self, env_path: Path, var: str, old_tag: str | None, env_written: bool,
                  new_ctn: str | None, compose_file: Path, service: str) -> str | None:
        """回滚：写回旧 tag → 移除新容器 → 用本地旧镜像按旧版本重建旧容器。

        旧镜像仅在更新成功后才清理，故失败时必然还在本地可重建。
        """
        errors: list[str] = []
        if env_written:
            try:
                write_env(env_path, {var: old_tag})  # old_tag None → 删除变量（首装场景）
            except OSError as e:
                errors.append(f"恢复 .env 失败: {e}")
        if new_ctn:
            try:
                if self.docker.container_exists(new_ctn):
                    self.docker.rm(new_ctn, force=True)
            except DockerError as e:
                errors.append(f"移除新容器失败: {e}")
        if old_tag:
            try:
                self.docker.compose_up(compose_file, service)  # 旧镜像重建旧容器
            except DockerError as e:
                errors.append(f"旧容器重建失败: {e}")
        return "; ".join(errors) or None

    def _record_state(self, repo: str, tag: str, image_id: str | None) -> None:
        """记录已装版本（checker 用于同 tag 重推检测）。"""
        if self.state is None:
            return
        data = self.state.get("server") or {}
        data[repo] = {
            "tag": tag,
            "image_id": image_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.set("server", data)
        self.state.save()
