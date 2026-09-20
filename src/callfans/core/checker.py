"""检查编排：Harbor ↔ 本地 对比，产出 UpdatePlan（§5 检查流程）。

- 类型路由（Q3）：annotation 有 com.callfans.type=frontend/sql 走 state 记账对比；
  其余默认 server，对领先候选拉 config Labels 确认类型并读 changelog（Q4）
- docker 不可用时跳过 server 类（避免把"查不到"当成"全都有新版"），frontend/sql 照常
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Config
from .compare import effective_time, find_leading, is_version_tag, pick_latest, tag_timestamp
from .harbor import HarborClient
from .local import DockerError, StateStore, docker_images, harbor_repo_of
from .models import (
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
        docker_ok = True
        try:
            for img in self._docker_images():
                repo = harbor_repo_of(img.repository, self.cfg.harbor_project)
                if repo:
                    local.setdefault(repo, {})[img.tag] = img.digest
        except DockerError as e:
            log.warning("docker 不可用，本次跳过 server 类制品: %s", e)
            docker_ok = False

        exclude = set(self.cfg.tag_exclude)
        pending: list[PendingItem] = []
        for repo in self.harbor.list_repos():
            tags = [t for t in self.harbor.repo_tags(repo) if is_version_tag(t.tag, exclude)]
            if not tags:
                continue
            declared = next(
                (t.annotations.get(KEY_TYPE) for t in tags if t.annotations.get(KEY_TYPE)), None
            )
            if declared == TYPE_FRONTEND:
                item = self._state_repo(repo, declared, tags)
            elif declared == TYPE_SQL:
                # Q9（2026-09-20）：sql 制品流由 sqlsync 域（云库→B库同步）替代
                log.debug("sql 仓库 %s 由 sqlsync 域处理，制品流忽略", repo)
                continue
            else:
                item = self._server_repo(repo, tags, local if docker_ok else None)
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
