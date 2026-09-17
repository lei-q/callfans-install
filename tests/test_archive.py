"""归档统一处理：zip / tar.gz / gzip 单文件 / 逃逸防护 / 单根目录上提。"""

import gzip
import io
import tarfile
import zipfile

import pytest

from callfans.core.updaters.archive import (
    ArchiveError, extract_archive, flatten_single_root, is_archive,
)


def make_zip(path, entries: dict[str, str]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def make_tar(path, entries: dict[str, str], mode="w:gz"):
    with tarfile.open(path, mode) as tf:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            data = content.encode()
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_is_archive(tmp_path):
    z = tmp_path / "a.zip"; make_zip(z, {"x": "1"})
    t = tmp_path / "a.tgz"; make_tar(t, {"x": "1"})
    plain = tmp_path / "a.sql"; plain.write_text("--", encoding="utf-8")
    assert is_archive(z) and is_archive(t)
    assert not is_archive(plain)


def test_extract_zip(tmp_path):
    z = tmp_path / "a.zip"; make_zip(z, {"index.html": "x", "js/app.js": "y"})
    dest = tmp_path / "out"
    extract_archive(z, dest)
    assert (dest / "index.html").read_text() == "x"
    assert (dest / "js" / "app.js").read_text() == "y"


def test_extract_tar_gz(tmp_path):
    t = tmp_path / "a.tar.gz"; make_tar(t, {"01_a.sql": "SELECT 1;", "02_b.sql": "SELECT 2;"})
    dest = tmp_path / "out"
    extract_archive(t, dest)
    assert (dest / "01_a.sql").read_text() == "SELECT 1;"


def test_extract_gzip_single_file(tmp_path):
    g = tmp_path / "a.sql.gz"
    with gzip.open(g, "wb") as f:
        f.write(b"SELECT 1;")
    dest = tmp_path / "out"
    extract_archive(g, dest)
    assert (dest / "a.sql").read_text() == "SELECT 1;"


def test_unsupported_format_rejected(tmp_path):
    p = tmp_path / "blob.bin"; p.write_bytes(b"\x00\x01\x02notarchive")
    with pytest.raises(ArchiveError):
        extract_archive(p, tmp_path / "out")


def test_zip_slip_rejected(tmp_path):
    z = tmp_path / "evil.zip"; make_zip(z, {"../../evil.txt": "x"})
    with pytest.raises(ArchiveError, match="非法路径"):
        extract_archive(z, tmp_path / "out")


def test_tar_traversal_rejected(tmp_path):
    t = tmp_path / "evil.tar"; make_tar(t, {"../evil.txt": "x"}, mode="w")
    with pytest.raises(ArchiveError, match="非法路径"):
        extract_archive(t, tmp_path / "out")


def test_flatten_single_root(tmp_path):
    dest = tmp_path / "site"
    (dest / "web-admin").mkdir(parents=True)
    (dest / "web-admin" / "index.html").write_text("x", encoding="utf-8")
    flatten_single_root(dest)
    assert (dest / "index.html").exists()
    assert not (dest / "web-admin").exists()
