"""compose 清单扫描：列出部署栈引用的全部镜像（首装/缺镜像检测用）。

为什么需要：检查更新此前只覆盖 Harbor 里"有版本 tag 的仓库"，无法发现
**本机完全没有的镜像**（首次安装场景）——compose 是本机部署的真值来源，
按它逐镜像核对"存在与否"，才能支撑首次安装。

镜像分两类：
- harbor 托管镜像（`${HARBOR_REGISTRY}/callfans/...`）→ 走版本比对；
- 第三方镜像（mysql/redis/minio 等）→ 只查存在性（版本由 compose 固定）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .local import harbor_repo_of
from .updaters.compose_env import read_text_loose, strip_tag, strip_vars

# service 名（缩进两空格）与其后的 image 行
_SERVICE_RE = re.compile(r"^  (\w[\w.-]*):\s*$", re.M)


@dataclass
class ComposeImage:
    service: str          # compose 服务名
    ref: str              # 镜像引用（原样，可能含 ${VAR}）
    repo: str             # 去变量后的仓库名（含 registry 前缀，若有）
    is_harbor: bool       # 是否 Harbor 托管（决定走版本比对还是存在性检查）
    harbor_repo: str = ""  # Harbor 端 repo 全名（callfans/api），仅 harbor 镜像


def scan_compose_images(compose_file: Path, project: str) -> list[ComposeImage]:
    """按 compose 原文解析每个 service 的 image（不展开变量）。"""
    try:
        text = read_text_loose(Path(compose_file))
    except OSError:
        return []
    images: list[ComposeImage] = []
    # 逐个 service 块解析：找到 service 行后，取其块内第一处 image
    matches = list(_SERVICE_RE.finditer(text))
    for i, m in enumerate(matches):
        service = m.group(1)
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():block_end]
        img_m = re.search(r"^\s*image:\s*(\S+)\s*$", block, re.M)
        if not img_m:
            continue
        ref = img_m.group(1)
        repo = strip_tag(strip_vars(ref))
        harbor_repo = harbor_repo_of(repo, project) or ""
        images.append(ComposeImage(
            service=service, ref=ref, repo=repo,
            is_harbor=bool(harbor_repo), harbor_repo=harbor_repo,
        ))
    return images


def local_has_image(images: list, repo: str, tag: str | None = None) -> bool:
    """本机 docker 中是否存在该 repo（可选精确到 tag）。

    images 为 local.docker_images() 结果；按"repo 后缀匹配"容忍
    registry 前缀差异（docker images 的 Repository 列可能带/不带 host）。
    """
    short = repo.split("/", 1)[1] if "/" in repo else repo
    for img in images:
        r = img.repository
        if r == repo or r.endswith("/" + short) or r.endswith("/" + repo):
            if tag is None or img.tag == tag:
                return True
    return False
