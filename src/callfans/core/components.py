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
from pathlib import Path

from .compose_scan import scan_compose_images
from .updaters.compose_env import read_text_loose
from .updaters.docker_cli import DockerCLI, DockerError

log = logging.getLogger(__name__)

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


def recover_components(cfg, services: list[str], docker: DockerCLI | None = None,
                       on_event=None) -> dict:
    """一键恢复：对异常服务执行 compose up -d --no-deps（创建/拉起容器）。

    与更新链路互斥由调用方（API 层）保证。返回 {recovered, failed, errors}。
    """
    docker = docker or DockerCLI()
    compose_file = Path(cfg.compose_file) if cfg.compose_file else None
    if compose_file is None or not compose_file.exists():
        return {"recovered": [], "failed": list(services),
                "errors": {"_": "COMPOSE_FILE 未配置或文件不存在"}}

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
