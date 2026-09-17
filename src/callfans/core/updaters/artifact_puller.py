"""ORAS 制品拉取封装（oras-py）。

frontend zip / sql 文件都是 OCI artifact，经 oras-py 拉取到临时目录。
若 oras-py 与 Harbor 兼容性问题（Q9 风险），降级方案是 registry v2 API 直下 blob。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class ArtifactPullError(RuntimeError):
    pass


class ArtifactPuller:
    def __init__(self, registry_base_url: str, username: str, password: str):
        parsed = urlparse(registry_base_url)
        self.host = parsed.netloc or registry_base_url
        # 跟随配置的 scheme：http:// 仓库以 insecure=True 构造（oras-py 语义为改走 http）
        self.insecure = parsed.scheme == "http"
        self.username = username
        self.password = password

    def pull(self, repo_full: str, tag: str, dest_dir: Path) -> list[Path]:
        """拉取 artifact 到调用方提供的目录（生命周期归调用方），返回文件列表。"""
        from oras.client import OrasClient

        client = OrasClient(hostname=self.host, insecure=self.insecure)
        # 直接设置 basic auth（login() 会依赖 docker CLI，不必走）
        client.auth.set_basic_auth(self.username, self.password)
        target = f"{self.host}/{repo_full}:{tag}"
        try:
            result = client.pull(target=target, outdir=str(dest_dir))
        except Exception as e:
            raise ArtifactPullError(f"oras 拉取 {target} 失败: {e}") from e
        if isinstance(result, (str, Path)):
            result = [result]
        files = [Path(p) for p in result]
        if not files:
            raise ArtifactPullError(f"oras 拉取 {target} 未返回文件")
        return files


def pick_file(files: list[Path], suffix: str) -> Path:
    """按后缀挑选制品文件；无匹配时若只有一个文件则直接用。"""
    matched = [f for f in files if f.suffix.lower() == suffix]
    if matched:
        return matched[0]
    if len(files) == 1:
        return files[0]
    raise ArtifactPullError(f"制品中未找到 {suffix} 文件: {[f.name for f in files]}")
