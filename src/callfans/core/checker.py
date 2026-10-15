"""检查编排：以 compose 清单为部署真值，统一「存在性 →（版本 tag 则）时间比对」。

- 统一规则（2026-10-15）：
  1. 存在性：Harbor 仓库在、本地没有 → 未安装（old=None）
  2. 待更新：版本 tag（时间戳格式）的仓库，Harbor 最新时间 > 本地 → 待更新
- 类型路由（Q3）：版本 tag 仓库按 annotation com.callfans.type=frontend/sql 走
  state 记账对比；其余默认 server，读 config Labels 确认类型与 changelog（Q4）
- base（固定 tag，如 mysql:8.0.24 / latest）只做存在性、不检验 tag
- docker 不可用时跳过 server/base 类（避免把"查不到"当成"全都有新版"），frontend 照常
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Config
from .compare import effective_time, find_leading, is_version_tag, pick_latest, tag_timestamp
from .compose_scan import ComposeImage, local_has_image, scan_compose_images
from .harbor import HarborClient
from .local import DockerError, StateStore, docker_images, harbor_repo_of
from .models import (
    TYPE_BASE,
    TYPE_FRONTEND,
    TYPE_SERVER,
    TYPE_SQL,
    KEY_ALIAS,
    KEY_CHANGELOG,
    KEY_TYPE,
    PendingItem,
    UpdatePlan,
)

log = logging.getLogger(__name__)

_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _tag_of(ref: str) -> str | None:
    """从镜像引用取 tag（无 tag 视为 latest）。"""
    from .updaters.compose_env import strip_tag

    ref = ref.split("@", 1)[0]
    if ref == strip_tag(ref):
        return None  # 无 tag 部分
    return ref.rsplit(":", 1)[1]


class Checker:
    def __init__(
        self,
        cfg: Config,
        harbor: HarborClient,
        docker_images_fn=docker_images,
        state: StateStore | None = None,
    ):
        self.cfg = cfg
        self.harbor = harbor
        self._docker_images = docker_images_fn
        self.state = state

    def run(self) -> UpdatePlan:
        state = self.state
        if state is None:
            from ..paths import state_file

            state = StateStore(state_file())
            self.state = state

        local: dict[str, dict[str, str]] = {}  # repo 全名 -> {tag: digest}
        local_images: list = []                # 全量本地镜像（含第三方）
        docker_ok = True
        try:
            for img in self._docker_images():
                local_images.append(img)
                repo = harbor_repo_of(img.repository, self.cfg.harbor_project)
                if repo:
                    local.setdefault(repo, {})[img.tag] = img.digest
        except DockerError as e:
            log.warning("docker 不可用，本次跳过 server/base 类制品: %s", e)
            docker_ok = False

        exclude = set(self.cfg.tag_exclude)
        pending: list[PendingItem] = []
        # compose 清单（部署真值来源）：逐个镜像走统一「存在性 →（版本 tag 则）时间比对」
        compose_images: list[ComposeImage] = []
        if self.cfg.compose_file and docker_ok:
            compose_images = scan_compose_images(self.cfg.compose_file, self.cfg.harbor_project)

        if compose_images:
            for ci in compose_images:
                item = self._check_image(ci, local, local_images, docker_ok, exclude)
                if item is not None:
                    pending.append(item)
        else:
            # 无 compose：全量 harbor 仓库走版本比对（向后兼容）
            for repo in self.harbor.list_repos():
                try:
                    tags = [t for t in self.harbor.repo_tags(repo) if is_version_tag(t.tag, exclude)]
                except Exception as e:
                    log.warning("仓库 %s tag 获取失败，跳过: %s", repo, e)
                    continue
                if not tags:
                    continue
                item = self._check_version_repo(repo, tags, local, docker_ok)
                if item is not None:
                    pending.append(item)

        checked_at = datetime.now(timezone.utc).isoformat()
        plan = UpdatePlan(checked_at=checked_at, pending=pending)
        state.set_last_plan(plan.to_dict(), checked_at)
        try:
            state.save()
        except OSError as e:  # 记账失败不影响检查结果返回
            log.error("state.json 写入失败: %s", e)
        log.info("检查完成: %d 项待更新", len(pending))
        return plan

    # ---------- 统一入口 ----------

    def _check_image(self, ci: ComposeImage, local: dict, local_images: list,
                     docker_ok: bool, exclude: set[str]) -> PendingItem | None:
        """单个 compose 镜像的统一判定：存在性 →（版本 tag 则）时间比对。

        - 第三方（无 harbor 前缀）→ base 存在性
        - harbor 托管：取 tag；有版本 tag → server/frontend 时间比对；
          只有非版本 tag → base 存在性；无任何 artifact / 404 → 不报
        """
        if not ci.is_harbor:
            return self._base_item(ci, local_images)
        try:
            all_tags = self.harbor.repo_tags(ci.harbor_repo)
        except Exception as e:
            log.warning("仓库 %s tag 获取失败，跳过: %s", ci.harbor_repo, e)
            return None
        if not all_tags:
            return None  # 仓库无任何 artifact（未推送/空仓库）→ 不报
        tags = [t for t in all_tags if is_version_tag(t.tag, exclude)]
        if tags:
            return self._check_version_repo(ci.harbor_repo, tags, local, docker_ok)
        return self._base_item(ci, local_images)

    def _check_version_repo(self, repo: str, tags, local: dict,
                            docker_ok: bool) -> PendingItem | None:
        """版本 tag 仓库：server/frontend 的「存在性 + 时间比对」（未安装 old=None）。"""
        declared = next(
            (t.annotations.get(KEY_TYPE) for t in tags if t.annotations.get(KEY_TYPE)), None
        )
        if declared == TYPE_FRONTEND:
            return self._state_repo(repo, TYPE_FRONTEND, tags)
        if declared == TYPE_SQL:
            # Q9（2026-09-20）：sql 制品流由 sqlsync 域（云库→B库同步）替代
            log.debug("sql 仓库 %s 由 sqlsync 域处理，制品流忽略", repo)
            return None
        return self._server_repo(repo, tags, local if docker_ok else None)

    def _base_item(self, ci: ComposeImage, local_images: list) -> PendingItem | None:
        """base：只判断是否存在（不检验 tag；版本由 compose 固定）。"""
        if local_has_image(local_images, ci.repo):
            return None
        tag = _tag_of(ci.ref)
        return PendingItem(
            name=ci.repo,
            type=TYPE_BASE,
            old=None,
            new=tag or "latest",
            changelog=f"compose 服务 {ci.service}：镜像未安装，将拉取 {ci.ref}",
            alias=ci.service,  # 复用 alias 字段携带 compose 服务名
        )

    # ---------- server（docker 镜像）----------

    def _server_repo(self, repo: str, tags, local: dict | None) -> PendingItem | None:
        if local is None:
            return None
        repo_local = local.get(repo) or {}
        cur_tags = [t for t in repo_local if is_version_tag(t, set(self.cfg.tag_exclude))]
        current_tag = (
            max(cur_tags, key=lambda t: tag_timestamp(t) or _MIN) if cur_tags else None
        )
        # 同 tag 重推检测用 state 记录的 digest（docker 本地 digest 与 manifest list 不可靠）
        rec = self.state.section_repo("server", repo) if self.state else None
        current_digest = rec.get("digest") if rec and rec.get("tag") == current_tag else None

        cand = pick_latest(find_leading(tags, current_tag, current_digest))
        if cand is None:
            return None
        try:
            labels = self.harbor.image_labels(repo, cand.tag)
        except Exception as e:
            log.warning("读取 %s:%s 的 Label 失败: %s", repo, cand.tag, e)
            labels = {}
        label_type = labels.get(KEY_TYPE)
        if label_type == TYPE_FRONTEND:
            # Label 声明为前端（罕见）：按 state 记账分支处理
            return self._state_repo(repo, label_type, tags)
        if label_type == TYPE_SQL:
            return None  # Q9：由 sqlsync 域处理
        return PendingItem(
            name=repo,
            type=TYPE_SERVER,
            old=current_tag,
            new=cand.tag,
            changelog=labels.get(KEY_CHANGELOG),
            digest_new=cand.digest,
            new_pushed_at=_iso(cand.push_time),
        )

    # ---------- frontend / sql（state 记账）----------

    def _state_repo(self, repo: str, type_: str, tags) -> PendingItem | None:
        rec = self.state.section_repo(type_, repo) if self.state else None
        if type_ == TYPE_SQL:
            applied: list[dict] = (rec or {}).get("applied") or []
            done = {a.get("tag") for a in applied}
            lead = sorted(
                (t for t in tags if t.tag not in done),
                key=lambda t: effective_time(t) or _MIN,
            )
            if not lead:
                return None
            old = applied[-1].get("tag") if applied else None
            return PendingItem(
                name=repo,
                type=TYPE_SQL,
                old=old,
                new=[t.tag for t in lead],
                changelog={t.tag: t.annotations.get(KEY_CHANGELOG) for t in lead},
            )
        # frontend
        cur_tag = (rec or {}).get("tag")
        cur_digest = (rec or {}).get("digest")
        cand = pick_latest(find_leading(tags, cur_tag, cur_digest))
        if cand is None:
            return None
        alias = cand.annotations.get(KEY_ALIAS) or repo.split("/", 1)[1]  # Q12
        return PendingItem(
            name=repo,
            type=TYPE_FRONTEND,
            old=cur_tag,
            new=cand.tag,
            changelog=cand.annotations.get(KEY_CHANGELOG),
            alias=alias,
            digest_new=cand.digest,
            new_pushed_at=_iso(cand.push_time),
        )
