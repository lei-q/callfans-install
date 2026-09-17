"""版本比较器（Q1 决策：push_time 策略）。

tag 形如 20260912132921-123a066（时间戳-commitId，commitId 可选——
真实仓库存在纯时间戳 tag 如 20260910161528，2026-09-17 放宽）：
- 排除 TAG_EXCLUDE（默认 latest/dev）与不符合格式的 tag（跳过并告警）
- 基准 = 当前 tag 在 Harbor 的 push_time；本地 tag 已被 Harbor 清理时退化为解析 tag 时间戳
- 同 tag 但 digest 与记录不同也判领先（CI 重推保护）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .models import ArtifactTag

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"^\d{14}(-[0-9a-f]{7,40})?$")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def is_version_tag(tag: str, exclude: set[str]) -> bool:
    if tag in exclude:
        return False
    if not TAG_RE.match(tag):
        log.warning("tag 不符合 时间戳-commitId 格式，跳过: %s", tag)
        return False
    return True


def tag_timestamp(tag: str) -> datetime | None:
    """从 tag 前缀解析时间（UTC，基准退化用）。"""
    try:
        return datetime.strptime(tag[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def effective_time(t: ArtifactTag) -> datetime | None:
    """排序时间：push_time 优先，缺失时退化为 tag 时间戳。"""
    return t.push_time or tag_timestamp(t.tag)


def find_leading(
    remote: list[ArtifactTag], current_tag: str | None, current_digest: str | None
) -> list[ArtifactTag]:
    """返回全部领先 tag，按时间升序（sql 积压场景直接可用；server/frontend 取末位）。"""
    base: datetime | None = None
    if current_tag:
        cur = next((t for t in remote if t.tag == current_tag), None)
        if cur is not None and cur.push_time is not None:
            base = cur.push_time
        else:
            base = tag_timestamp(current_tag)
    leading: list[ArtifactTag] = []
    for t in remote:
        if current_tag is not None and t.tag == current_tag:
            # 同 tag 但 digest 变化 → CI 重推，判领先
            if current_digest and t.digest and t.digest != current_digest:
                leading.append(t)
            continue
        t_time = effective_time(t)
        if t_time is None:
            log.warning("tag 无 push_time 且时间戳不可解析，跳过: %s", t.tag)
            continue
        if base is None or t_time > base:
            leading.append(t)
    leading.sort(key=lambda t: effective_time(t) or _EPOCH)
    return leading


def pick_latest(leading: list[ArtifactTag]) -> ArtifactTag | None:
    return leading[-1] if leading else None
