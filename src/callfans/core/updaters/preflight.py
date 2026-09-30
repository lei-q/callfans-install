"""更新前置检查（§6 preflight）：按待更新项类型校验依赖，任一失败整体报错、不动任何东西。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ...config import Config
from ..models import TYPE_FRONTEND, TYPE_SERVER, UpdatePlan
from .compose_env import (
    extract_tag_var, find_image_template, iter_image_templates, read_env,
    read_text_loose, vars_in,
)
from .docker_cli import DockerCLI, DockerError

_MIN_FREE_BYTES = 100 * 1024 * 1024  # 100MB



class PreflightError(RuntimeError):
    pass


def preflight(cfg: Config, plan: UpdatePlan, docker: DockerCLI | None = None) -> None:
    if not plan.pending:
        return
    errors: list[str] = []
    types = {p.type for p in plan.pending}

    if TYPE_SERVER in types:
        if not cfg.compose_file:
            errors.append("COMPOSE_FILE 未配置")
        else:
            compose_file = Path(cfg.compose_file)
            d = docker or DockerCLI()
            if not compose_file.exists():
                errors.append(f"compose 文件不存在: {compose_file}")
            else:
                try:
                    d.version_ok()
                    _, var_warnings = d.compose_config_checked(compose_file)
                    text = read_text_loose(compose_file)
                    templates = iter_image_templates(text)
                    # 所有 tag 变量放行（更新器写入目标 tag / backfill 回填当前版本），
                    # 无论该服务本次是否有待更新；非 tag 变量缺失仍拦截
                    tag_vars = {v for v in (extract_tag_var(t) for t in templates) if v}
                    # 确定性检查：镜像行引用的非 tag 变量必须在 compose .env 中有值
                    #（不依赖 docker 告警文本格式——不同 compose 版本输出有差异）
                    env_values = read_env(compose_file.parent / ".env")
                    required = {
                        v for t in templates for v in vars_in(t)
                        if v != extract_tag_var(t) and not env_values.get(v)
                    }
                    for p in [x for x in plan.pending if x.type == TYPE_SERVER]:
                        template = find_image_template(text, p.name, cfg.harbor_project)
                        if template is None or extract_tag_var(template) is None:
                            errors.append(f"{p.name}: compose image 未使用 ${{VAR}} 形式（Q5）")
                    missing = sorted((set(var_warnings) | required) - tag_vars)
                    if missing:
                        errors.append(
                            f"compose 变量未定义: {', '.join(missing)}"
                            "（需在 compose 同目录 .env 定义；tag 变量由更新器维护）"
                        )
                    # HTTP 仓库必须加入 daemon insecure-registries（pull 走 Docker
                    # 守护进程，对非本机 registry 默认强制 HTTPS，与 .env 协议无关
                    # ——2026-09-30 实测：EOF 根因）
                    registry = env_values.get("HARBOR_REGISTRY")
                    insecure_ok = True
                    if registry and (cfg.harbor_api_url or "").startswith("http://"):
                        secure_map = d.registry_secure_map()
                        host = registry.split(":")[0]
                        insecure_ok = any(
                            name in (registry, host) and not secure
                            for name, secure in secure_map.items())
                        if not insecure_ok:
                            errors.append(
                                f"HTTP 仓库 {registry} 未加入 Docker daemon 的 insecure-registries"
                                "（docker pull 对非本机仓库默认强制 HTTPS，EOF 的根因）。"
                                "修复：Docker Desktop → Settings → Docker Engine，在 daemon.json "
                                f'增加 "insecure-registries": ["{host}"]，Apply & Restart。'
                                f"（当前 daemon 名单: {sorted(secure_map) or '空'}）"
                            )
                    if registry and insecure_ok:
                        try:
                            d.login(registry, cfg.harbor_username, cfg.harbor_password)
                        except DockerError as e:
                            errors.append(
                                f"{e}（检查 HARBOR_USERNAME/PASSWORD 在该仓库有效）"
                            )
                except Exception as e:
                    errors.append(f"docker/compose 不可用: {e}")

    if TYPE_FRONTEND in types:
        if not cfg.frontend_output_dir:
            errors.append("FRONTEND_OUTPUT_DIR 未配置")
        else:
            try:
                Path(cfg.frontend_output_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                errors.append(f"前端目录不可写: {e}")

    # 磁盘
    check_dirs = [("临时目录", Path(tempfile.gettempdir()))]
    if cfg.frontend_output_dir:
        check_dirs.append(("前端目录", Path(cfg.frontend_output_dir)))
    for label, p in check_dirs:
        try:
            if shutil.disk_usage(p).free < _MIN_FREE_BYTES:
                errors.append(f"{label}剩余空间不足 100MB")
        except OSError:
            pass

    if errors:
        raise PreflightError("; ".join(errors))
