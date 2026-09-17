"""前端静态文件更新器（§6.2）：oras 拉取 → 归档解压到 .new → 原子替换。

- 归档格式按 magic 识别（zip / tar.gz / gz 单文件），非归档单文件直接落盘
- 路径逃逸防护（zip-slip / tar ../）；单一顶层目录自动剥掉
- `<alias>.bak` 保留上一版，替换成功后删除；失败时正式目录不受影响
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ...config import Config
from ..local import StateStore
from ..models import PendingItem
from .archive import extract_archive, flatten_single_root, is_archive
from .artifact_puller import ArtifactPuller

log = logging.getLogger(__name__)


class FrontendError(RuntimeError):
    pass


class FrontendUpdater:
    def __init__(self, cfg: Config, state: StateStore | None = None,
                 puller: ArtifactPuller | None = None, on_event=None):
        self.cfg = cfg
        self.state = state
        self.puller = puller
        self.on_event = on_event or (lambda *a, **k: None)

    def update(self, item: PendingItem) -> dict:
        alias = item.alias or item.name.split("/", 1)[1]
        record = {
            "name": item.name, "type": "frontend", "old": item.old, "new": item.new,
            "result": "failed", "error": None, "alias": alias,
        }
        if not self.cfg.frontend_output_dir:
            record["error"] = "FRONTEND_OUTPUT_DIR 未配置"
            return record
        base = Path(self.cfg.frontend_output_dir)
        target = base / alias
        new_dir = base / f"{alias}.new"
        bak_dir = base / f"{alias}.bak"
        puller = self.puller or ArtifactPuller(
            self.cfg.harbor_api_url, self.cfg.harbor_username, self.cfg.harbor_password
        )
        tmpdir = Path(tempfile.mkdtemp(prefix="callfans-frontend-"))
        try:
            self.on_event("update_progress", {"item": item.name, "stage": "pull"})
            files = puller.pull(item.name, item.new, tmpdir)
            artifact = next((f for f in files if is_archive(f)), None)

            self.on_event("update_progress", {"item": item.name, "stage": "unzip"})
            if new_dir.exists():
                shutil.rmtree(new_dir)
            if artifact is not None:
                extract_archive(artifact, new_dir)
                flatten_single_root(new_dir)
            elif len(files) == 1:
                new_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(files[0], new_dir / files[0].name)
            else:
                raise FrontendError(f"制品中未找到压缩包: {[f.name for f in files]}")
            if not any(new_dir.iterdir()):
                raise FrontendError("解压结果为空目录")

            self.on_event("update_progress", {"item": item.name, "stage": "replace"})
            _remove(bak_dir)
            if target.exists():
                os.rename(target, bak_dir)
            try:
                os.rename(new_dir, target)
            except OSError:
                if not target.exists() and bak_dir.exists():  # 还原
                    os.rename(bak_dir, target)
                raise
            _remove(bak_dir)  # 成功后清理上一版

            if self.state is not None:
                data = self.state.get("frontend") or {}
                data[item.name] = {
                    "tag": item.new,
                    "digest": item.digest_new,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self.state.set("frontend", data)
                self.state.save()
            record.update(result="success")
            return record
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"
            return record
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)
