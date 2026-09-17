"""数据模型与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 制品类型（com.callfans.type 的取值）
TYPE_SERVER = "server"
TYPE_FRONTEND = "frontend"
TYPE_SQL = "sql"

# annotation / Label 约定 key
KEY_TYPE = "com.callfans.type"
KEY_CHANGELOG = "com.callfans.changelog"
KEY_ALIAS = "com.callfans.alias"


@dataclass
class ArtifactTag:
    """Harbor 中某个 tag 指向的制品（digest 即制品 manifest digest）。"""

    tag: str
    digest: str
    push_time: datetime | None = None
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class PendingItem:
    """一条待更新项（UpdatePlan 成员）。"""

    name: str                      # Harbor repo 全名，如 callfans/api
    type: str                      # server / frontend / sql
    old: str | None                # 当前版本 tag；None 表示本地未安装
    new: str | list[str]           # 目标 tag；sql 为升序列表
    changelog: Any = None          # str；sql 为 {tag: str}
    alias: str | None = None       # 仅 frontend
    digest_new: str | None = None  # 目标制品 digest（sql 无单一值）
    new_pushed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "old": self.old,
            "new": self.new,
            "changelog": self.changelog,
            "alias": self.alias,
            "digest_new": self.digest_new,
            "new_pushed_at": self.new_pushed_at,
        }


@dataclass
class UpdatePlan:
    """一次检查的产出。"""

    checked_at: str
    pending: list[PendingItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "pending": [p.to_dict() for p in self.pending],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UpdatePlan":
        pending = [
            PendingItem(
                name=p["name"],
                type=p["type"],
                old=p.get("old"),
                new=p.get("new"),
                changelog=p.get("changelog"),
                alias=p.get("alias"),
                digest_new=p.get("digest_new"),
                new_pushed_at=p.get("new_pushed_at"),
            )
            for p in data.get("pending", [])
        ]
        return cls(checked_at=data.get("checked_at", ""), pending=pending)
