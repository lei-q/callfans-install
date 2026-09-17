"""归档统一处理：zip / tar(.gz) / gzip 单文件。

sql 制品与前端制品实际是 CI 打包产物，格式随流水线而异（zip 或 tar.gz），
按 magic 字节识别而非后缀；所有解包路径做逃逸防护（zip-slip / tar ../）。
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
import zipfile
from pathlib import Path


class ArchiveError(RuntimeError):
    pass


def _magic(path: Path, size: int = 4) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def is_archive(path: Path) -> bool:
    m = _magic(path)
    if m[:2] == b"PK" or m[:2] == b"\x1f\x8b":  # zip / gzip
        return True
    try:  # plain tar：offset 257 处 "ustar"
        with open(path, "rb") as f:
            f.seek(257)
            return f.read(5) == b"ustar"
    except OSError:
        return False


def _checked(dest: Path, name: str) -> Path:
    """成员路径必须落在 dest 内，拒绝 ../ 与绝对路径。"""
    target = (dest / name).resolve()
    root = dest.resolve()
    if target != root and root not in target.parents:
        raise ArchiveError(f"归档含非法路径: {name}")
    return target


def extract_archive(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    m = _magic(path)
    if m[:2] == b"PK":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                _checked(dest, name)
            zf.extractall(dest)
    elif m[:2] == b"\x1f\x8b":
        try:
            with tarfile.open(path, "r:gz") as tf:
                _extract_tar(tf, dest)
        except tarfile.ReadError:
            # 单文件 gzip：解压为去 .gz 后缀的文件
            out = dest / (path.stem if path.stem and path.stem != path.name else "artifact")
            with gzip.open(path, "rb") as src, open(out, "wb") as w:
                shutil.copyfileobj(src, w)
    else:
        try:
            with tarfile.open(path, "r:") as tf:
                _extract_tar(tf, dest)
        except tarfile.ReadError as e:
            raise ArchiveError(f"不支持的归档格式: {path.name}") from e


def _extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    for member in tf.getmembers():
        target = _checked(dest, member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isreg():
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        # 软链接/设备文件等一律跳过（防逃逸）


def flatten_single_root(dest: Path) -> None:
    """全部条目共享单一顶层目录时把内容上提一级（打包习惯差异兜底）。"""
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        staging = dest.parent / f"{dest.name}.moving"
        if staging.exists():
            shutil.rmtree(staging)
        inner.rename(staging)
        try:
            for child in list(staging.iterdir()):
                child.rename(dest / child.name)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
