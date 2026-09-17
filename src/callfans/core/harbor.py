"""Harbor 客户端：管理面 API（/api/v2.0）+ registry 面 API（/v2）。

- 列 repo / 列 artifact（含 tag 的 push_time 与 manifest annotations）
- 读 docker 镜像 Label：manifest → config blob 两步（Q4 决策，不下载镜像层）
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import httpx

from .models import ArtifactTag

log = logging.getLogger(__name__)

_PAGE_SIZE = 100

# 同时接受 docker schema2 与 OCI manifest
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)


class HarborError(RuntimeError):
    pass


def parse_time(value: str | None) -> datetime | None:
    """解析 Harbor 返回的时间（RFC3339，形如 2026-09-12T13:35:02.000Z）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class HarborClient:
    def __init__(self, base_url: str, project: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.project = project
        self._http = httpx.Client(base_url=self.base_url, auth=(username, password), timeout=30)

    # ---------- 基础 ----------

    def _get_json(self, path: str, params: dict | None = None, headers: dict | None = None):
        resp = self._http.get(path, params=params, headers=headers)
        if resp.status_code >= 400:
            raise HarborError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    def _paginated(self, path: str, extra_params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            params = {"page": page, "page_size": _PAGE_SIZE, **(extra_params or {})}
            data = self._get_json(path, params=params).json()
            items.extend(data)
            if len(data) < _PAGE_SIZE:
                return items
            page += 1

    # ---------- 管理面 /api/v2.0 ----------

    def list_repos(self) -> list[str]:
        """返回 project 下全部 repo 全名（形如 'callfans/api'）。"""
        path = f"/api/v2.0/projects/{quote(self.project, safe='')}/repositories"
        repos: list[str] = []
        for item in self._paginated(path):
            # 不同 Harbor 版本字段有差异：优先 full_name，name 可能带或不带 project 前缀
            name = item.get("full_name") or item.get("name") or ""
            name = name.strip("/")
            if not name:
                continue
            if not name.startswith(f"{self.project}/"):
                name = f"{self.project}/{name}"
            repos.append(name)
        return repos

    def repo_tags(self, repo_full: str) -> list[ArtifactTag]:
        """列出 repo 全部 artifact 的 tag（带 push_time 与 manifest annotations）。"""
        short = repo_full.split("/", 1)[1]
        path = (
            f"/api/v2.0/projects/{quote(self.project, safe='')}"
            f"/repositories/{quote(short, safe='')}/artifacts"
        )
        tags: list[ArtifactTag] = []
        for art in self._paginated(path, extra_params={"with_tag": "true"}):
            digest = art.get("digest", "")
            annotations = art.get("annotations") or {}
            pushed = parse_time(art.get("push_time"))
            for t in art.get("tags") or []:
                tag_name = t.get("name")
                if not tag_name:
                    continue
                tags.append(
                    ArtifactTag(
                        tag=tag_name,
                        digest=digest,
                        push_time=parse_time(t.get("push_time")) or pushed,
                        annotations=annotations,
                    )
                )
        return tags

    # ---------- registry 面 /v2（读 docker 镜像 Label，Q4）----------

    def get_manifest(self, repo_full: str, ref: str) -> dict:
        path = f"/v2/{repo_full}/manifests/{ref}"
        return self._get_json(path, headers={"Accept": _MANIFEST_ACCEPT}).json()

    def image_labels(self, repo_full: str, ref: str) -> dict[str, str]:
        """manifest → config.digest → config blob → Labels。manifest list 时下钻第一层。"""
        manifest = self.get_manifest(repo_full, ref)
        if "config" not in manifest and "manifests" in manifest:
            # manifest list / OCI index：取第一个子 manifest
            child = (manifest.get("manifests") or [{}])[0].get("digest")
            if not child:
                return {}
            manifest = self.get_manifest(repo_full, child)
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return {}
        blob = self._get_json(f"/v2/{repo_full}/blobs/{config_digest}").json()
        return blob.get("config", {}).get("Labels") or {}

    def close(self) -> None:
        self._http.close()
