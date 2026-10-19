"""功能组件：compose 清单驱动的容器运行状态采集与一键恢复（2026-10-19）。

组件列表从部署 compose 文件解析（服务名/镜像/端口）；运行状态逐个
inspect 容器（按 compose service 标签定位，项目无关）：
- running   运行中（有 healthcheck 时须为 healthy）
- exited    存在但已退出
- missing   容器不存在（未创建/被删）
恢复 = docker compose up -d --no-deps <service>（容器不在则创建，在则按
compose 定义拉起；与更新链路复用同一 compose_up 原语）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .compose_scan import scan_compose_images
from .updaters.compose_env import read_text_loose
from .updaters.docker_cli import DockerCLI, DockerError

log = logging.getLogger(__name__)

_MIN = datetime.min.replace(tzinfo=timezone.utc)

_STATUS_RUNNING = "running"
_STATUS_UNHEALTHY = "unhealthy"
_STATUS_RESTARTING = "restarting"
_STATUS_EXITED = "exited"
_STATUS_MISSING = "missing"

# 可视状态（UI 徽标用）：异常集合 = unhealthy / restarting / exited / missing
ABNORMAL = {_STATUS_UNHEALTHY, _STATUS_RESTARTING, _STATUS_EXITED, _STATUS_MISSING}

_STATUS_TEXT = {
    _STATUS_RUNNING: "运行中",
    _STATUS_UNHEALTHY: "健康检查失败",
    _STATUS_RESTARTING: "重启循环",
    _STATUS_EXITED: "已退出",
    _STATUS_MISSING: "未创建",
}


def _norm_ports(raw: list) -> list[str]:
    """compose config 的 ports 归一化为展示字符串。

    新版 compose 输出 dict（{published, target}），旧版输出 "3306:3306" 字符串。
    """
    out: list[str] = []
    for p in raw or []:
        if isinstance(p, dict):
            pub, tgt = p.get("published"), p.get("target")
            if pub and tgt and pub != tgt:
                out.append(f"{pub}:{tgt}")
            else:
                out.append(str(pub or tgt or ""))
        else:
            out.append(str(p))
    return [p for p in out if p]


@dataclass
class Component:
    service: str      # compose 服务名（恢复操作的单位）
    image: str        # 解析后的镜像引用（含 ${VAR} 原样）
    ports: list[str]  # 端口映射（展示用）
    status: str = _STATUS_MISSING
    detail: str = ""  # 异常补充（exit code / health 状态）

    @property
    def status_text(self) -> str:
        text = _STATUS_TEXT.get(self.status, self.status)
        return f"{text}（{self.detail}）" if self.detail else text

    @property
    def abnormal(self) -> bool:
        return self.status in ABNORMAL

    def to_dict(self) -> dict:
        return {
            "service": self.service, "image": self.image, "ports": self.ports,
            "status": self.status, "status_text": self.status_text,
            "abnormal": self.abnormal, "detail": self.detail,
        }


def _ports_of(compose_file: Path, service: str) -> list[str]:
    """从 compose 原文取服务块内的 ports 行（轻量解析，与 compose_scan 同风格）。"""
    import re

    try:
        text = read_text_loose(compose_file)
    except OSError:
        return []
    svc_re = re.compile(r"^  (\w[\w.-]*):\s*$", re.M)
    matches = list(svc_re.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1) != service:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        return re.findall(r'^\s*-\s*"?([^"\n]+)"?\s*$', text[m.end():end], re.M)
    return []


def list_components(cfg, docker: DockerCLI | None = None) -> list[dict]:
    """采集全部组件及运行状态。compose/docker 不可用时返回空列表（UI 显示不可用）。"""
    if not cfg.compose_file:
        return []
    compose_file = Path(cfg.compose_file)
    if not compose_file.exists():
        return []
    docker = docker or DockerCLI()

    try:
        config = docker.compose_config(compose_file)
    except DockerError as e:
        log.warning("compose config 失败，组件列表不可用: %s", e)
        return []
    services_cfg = config.get("services") or {}

    # 镜像引用优先用解析后的 config（变量已代入），fallback 原文模板
    images = {ci.service: ci.ref for ci in scan_compose_images(compose_file, cfg.harbor_project)}

    comps: list[Component] = []
    for service, scfg in services_cfg.items():
        comps.append(Component(
            service=service,
            image=scfg.get("image") or images.get(service, ""),
            ports=_norm_ports(scfg.get("ports")),
        ))
        if not comps[-1].ports:  # config 无端口（罕见）→ 回原文取
            comps[-1].ports = _ports_of(compose_file, service)

    for comp in comps:
        try:
            names = docker.containers_by_service(comp.service)
        except DockerError:
            names = []
        if not names:
            comp.status = _STATUS_MISSING
            comp.detail = ""
            continue
        # 取最新创建的同名容器（固定 container_name 场景只有一个）
        name = names[0]
        try:
            info = docker.inspect_container(name)
        except DockerError as e:
            comp.status = _STATUS_MISSING
            comp.detail = f"inspect 失败: {e}"
            continue
        st = info.get("State", {})
        if st.get("Restarting"):
            comp.status = _STATUS_RESTARTING
        elif not st.get("Running"):
            comp.status = _STATUS_EXITED
            comp.detail = f"exit={st.get('ExitCode')}"
        else:
            health = (st.get("Health") or {}).get("Status")
            if health == "unhealthy":
                comp.status = _STATUS_UNHEALTHY
            else:
                comp.status = _STATUS_RUNNING
                if health == "starting":  # 健康检查启动期不算异常
                    comp.detail = "healthcheck 启动中"
    return [c.to_dict() for c in comps]


def _fix_dir_placeholder_mounts(cfg, compose_file: Path) -> list[str]:
    """修复「挂载源是目录占位」的文件挂载（2026-10-19 实测 xray 三件套中招）。

    现象：compose 文件挂载（./server/xray/config.json:/etc/xray/config.json）
    的宿主侧不存在时，Docker Desktop 自动创建同名目录占位；此后文件永远
    挂不进去（not a directory: mount onto a file）。

    修复：检测 compose 全部「宿主侧相对路径 → 容器侧文件」的挂载，宿主侧
    已是目录的，删目录占位并从安装包 assets 对应文件拷回（nginx →
    assets/nginx，server/* → assets/xray，其余来源无种子 → 仅删占位并告警）。
    返回修复的挂载源列表。
    """
    import re
    import shutil

    try:
        text = read_text_loose(compose_file)
    except OSError:
        return []
    deploy_dir = compose_file.parent
    # 相对路径文件挂载：- ./server/xray/config.json:/etc/xray/config.json[:rw]
    mounts = re.findall(
        r'^\s*-\s+(\./[^:\s]+)\s*:\s*/[^:\s]+(?::\w+)?\s*$', text, re.M)
    # 安装包种子源（assets/nginx → 部署根/nginx，assets/xray → 部署根/server/xray）
    seeds: list[Path] = []
    if cfg.app_root:
        assets = Path(cfg.app_root) / "assets"
        seeds = [assets / "nginx", assets / "xray"]

    def _find_seed(rel_norm: str) -> Path | None:
        parts = Path(rel_norm).parts
        for s in seeds:
            if s.name == "nginx" and rel_norm.startswith("nginx/") and len(parts) > 1:
                cand = s.joinpath(*parts[1:])
                if cand.is_file():
                    return cand
            elif s.name == "xray" and rel_norm.startswith("server/xray/") and len(parts) > 2:
                cand = s.joinpath(*parts[2:])
                if cand.is_file():
                    return cand
        return None

    fixed: list[str] = []
    for m in mounts:
        if not str(m).startswith("./"):
            continue  # 只处理相对挂载（部署目录内）
        rel_norm = str(m)[2:]
        target = deploy_dir / Path(rel_norm)
        if not (target.exists() and target.is_dir()):
            continue  # 只处理「应为文件却成了目录」的占位
        seed = _find_seed(rel_norm)
        if seed is None:
            # 无种子（目录挂载如 ./nginx/config 也走不到这——种子映射只命中
            # 文件路径；真无种子的文件挂载不自动删，避免破坏现场数据）
            log.warning("挂载源 %s 是目录占位但找不到安装包种子文件，未自动修复", target)
            continue
        shutil.rmtree(target)  # 占位目录（docker 自动创建的空目录）
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, target)
        log.info("已修复挂载占位目录并拷回种子文件: %s（源 %s）", target, seed)
        fixed.append(str(target))
    return fixed


def _backfill_missing_tag_vars(cfg, compose_file: Path, harbor=None) -> list[str]:
    """compose .env 缺失的 tag 变量补齐：本地镜像 tag 优先，Harbor 最新兜底。

    场景：首装后尚未跑过任何更新，.env 无 *_TAG（本地也无镜像可回填），
    手动/一键恢复时 compose 插值出空 tag。写回后任何 compose up 都能插值成功。
    返回写入的变量名列表。
    """
    from .compare import is_version_tag, pick_latest, tag_timestamp, effective_time
    from .local import docker_images, harbor_repo_of
    from .updaters.compose_env import (
        extract_tag_var, iter_image_templates, read_env, strip_tag, strip_vars, write_env,
    )

    try:
        text = read_text_loose(compose_file)
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
        pass  # docker 不可用 → 全靠 Harbor 兜底

    env_path = compose_file.parent / ".env"
    env = read_env(env_path)
    changed: dict[str, str] = {}
    for template in iter_image_templates(text):
        var = extract_tag_var(template)
        if var is None or env.get(var) or var in changed:
            continue
        repo = harbor_repo_of(strip_tag(strip_vars(template)), cfg.harbor_project)
        tag = local_map.get(repo)
        if tag is None and harbor is not None and repo:
            try:
                tags = [t for t in harbor.repo_tags(repo) if is_version_tag(t.tag, exclude)]
                cand = pick_latest(sorted(tags, key=lambda t: effective_time(t) or _MIN))
                tag = cand.tag if cand else None
            except Exception as e:  # Harbor 不可达不阻断恢复（compose 用空 tag 会自然报错）
                log.debug("Harbor 取 %s 最新 tag 失败: %s", repo, e)
        if tag:
            changed[var] = tag
    if changed:
        try:
            write_env(env_path, changed)
            log.info("恢复前补齐 compose tag 变量: %s", changed)
        except OSError as e:
            log.error("tag 变量补齐写入失败: %s", e)
            return []
    return list(changed)


def recover_components(cfg, services: list[str], docker: DockerCLI | None = None,
                       on_event=None, harbor=None) -> dict:
    """一键恢复：对异常服务执行 compose up -d --no-deps（创建/拉起容器）。

    与更新链路互斥由调用方（API 层）保证。恢复前先把 compose .env 缺失的
    tag 变量补齐（本地镜像有 tag 优先，否则 Harbor 最新 tag），否则 compose
    插值出空 tag → invalid reference format（2026-10-19 实测）。
    返回 {recovered, failed, errors}。
    """
    docker = docker or DockerCLI()
    compose_file = Path(cfg.compose_file) if cfg.compose_file else None
    if compose_file is None or not compose_file.exists():
        return {"recovered": [], "failed": list(services),
                "errors": {"_": "COMPOSE_FILE 未配置或文件不存在"}}

    backfilled = _backfill_missing_tag_vars(cfg, compose_file, harbor=harbor)
    if backfilled and on_event:
        on_event("recover_progress", {"service": "(compose)", "stage": "tag_backfill",
                                      "vars": backfilled})

    fixed_mounts = _fix_dir_placeholder_mounts(cfg, compose_file)
    if fixed_mounts and on_event:
        on_event("recover_progress", {"service": "(mounts)", "stage": "mount_fix",
                                      "files": fixed_mounts})

    known = {c["service"] for c in list_components(cfg, docker)}
    recovered: list[str] = []
    failed: list[str] = []
    errors: dict[str, str] = {}
    for svc in services:
        if svc not in known:
            failed.append(svc)
            errors[svc] = "compose 中无此服务"
            continue
        if on_event:
            on_event("recover_progress", {"service": svc, "stage": "start"})
        try:
            docker.compose_up(compose_file, svc)
            recovered.append(svc)
            if on_event:
                on_event("recover_progress", {"service": svc, "stage": "done", "ok": True})
        except DockerError as e:
            failed.append(svc)
            errors[svc] = str(e)
            if on_event:
                on_event("recover_progress", {"service": svc, "stage": "done", "ok": False,
                                              "error": str(e)})
    return {"recovered": recovered, "failed": failed, "errors": errors}
