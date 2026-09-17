"""本地侧：docker 镜像读取 + state.json 记账（Q2/Q14 决策：前端与 SQL 由应用自维护账本）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class DockerError(RuntimeError):
    pass


@dataclass
class DockerImage:
    repository: str  # docker images 的 Repository 列，可能带 registry host 前缀
    tag: str
    digest: str      # repo digest（可能为 "<none>"）


def docker_images() -> list[DockerImage]:
    """`docker images --digests` 结构化读取。"""
    from .procs import run as proc_run

    try:
        proc = proc_run(
            ["docker", "images", "--format", "{{json .}}", "--digests"],
            timeout=60,
        )
    except FileNotFoundError as e:
        raise DockerError("未找到 docker 命令") from e
    except subprocess.TimeoutExpired as e:
        raise DockerError("docker images 超时") from e
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise DockerError(f"docker images 失败: {(stderr or stdout)[:300]}")
    images: list[DockerImage] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        repo, tag = row.get("Repository"), row.get("Tag")
        if not repo or not tag or repo == "<none>" or tag == "<none>":
            continue
        images.append(DockerImage(repository=repo, tag=tag, digest=row.get("Digest", "<none>")))
    return images


def harbor_repo_of(repository: str, project: str) -> str | None:
    """把本地 repository（可能带 host 前缀，如 harbor.example.com/callfans/api）
    归一化为 Harbor repo 全名（callfans/api）；不属于该 project 返回 None。"""
    parts = repository.split("/")
    try:
        idx = parts.index(project)
    except ValueError:
        return None
    if idx == len(parts) - 1:  # project 是最后一段，不是目录名
        return None
    return "/".join(parts[idx:])


class StateStore:
    """state.json：frontend/sql 版本记账、server 已装 digest 缓存、最近一次 UpdatePlan。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.error("state.json 读取失败，按空状态处理: %s", e)
                self._data = {}
        else:
            self._data = {}
        return self._data

    def save(self) -> None:
        """原子写（临时文件 + rename）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # dict 风格访问
    def get(self, section: str, default=None):
        return self._data.get(section, default)

    def set(self, section: str, value) -> None:
        self._data[section] = value

    def section_repo(self, section: str, repo: str) -> dict | None:
        return (self._data.get(section) or {}).get(repo)

    def set_last_plan(self, plan_dict: dict, checked_at: str) -> None:
        self._data["last_plan"] = plan_dict
        self._data["checked_at"] = checked_at

    @property
    def last_plan(self) -> dict | None:
        return self._data.get("last_plan")

    @property
    def checked_at(self) -> str | None:
        return self._data.get("checked_at")
