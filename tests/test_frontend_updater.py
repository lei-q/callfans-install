"""前端更新器：解压、顶层目录剥除、原子替换、zip-slip 防护、state 记账。"""

import zipfile

from callfans.core.local import StateStore
from callfans.core.models import PendingItem, TYPE_FRONTEND
from callfans.core.updaters.frontend_updater import FrontendUpdater, safe_unzip
from tests.test_checker import make_cfg


def make_zip(tmp_path, entries: dict[str, str]) -> object:
    p = tmp_path / "artifact.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return p


class FakePuller:
    def __init__(self, zip_path):
        self.zip_path = zip_path

    def pull(self, repo, tag, dest_dir):
        import shutil

        dest = dest_dir / self.zip_path.name
        shutil.copy(self.zip_path, dest)
        return [dest]


def make_item(alias="admin", new="20260912132921-123a066"):
    return PendingItem(name="callfans/web-admin", type=TYPE_FRONTEND,
                       old="t-old", new=new, alias=alias, digest_new="sha256:new")


def test_success_replaces_and_records(tmp_path):
    out = tmp_path / "webroot"
    out.mkdir()
    (out / "admin").mkdir()
    (out / "admin" / "old.html").write_text("old", encoding="utf-8")
    cfg = make_cfg(frontend_output_dir=out)
    state = StateStore(tmp_path / "state.json")
    z = make_zip(tmp_path, {"index.html": "new", "assets/app.js": "js"})
    record = FrontendUpdater(cfg, state, FakePuller(z)).update(make_item())
    assert record["result"] == "success", record["error"]
    assert (out / "admin" / "index.html").read_text(encoding="utf-8") == "new"
    assert not (out / "admin" / "old.html").exists()
    assert not (out / "admin.bak").exists()  # 成功后清理
    assert not (out / "admin.new").exists()
    assert state.get("frontend")["callfans/web-admin"]["tag"] == "20260912132921-123a066"


def test_single_top_level_dir_stripped(tmp_path):
    out = tmp_path / "webroot"
    cfg = make_cfg(frontend_output_dir=out)
    z = make_zip(tmp_path, {"web-admin/index.html": "x", "web-admin/a.js": "y"})
    record = FrontendUpdater(cfg, None, FakePuller(z)).update(make_item())
    assert record["result"] == "success"
    assert (out / "admin" / "index.html").exists()
    assert not (out / "admin" / "web-admin").exists()


def test_zip_slip_rejected_target_untouched(tmp_path):
    out = tmp_path / "webroot"
    out.mkdir()
    (out / "admin").mkdir()
    (out / "admin" / "keep.txt").write_text("keep", encoding="utf-8")
    cfg = make_cfg(frontend_output_dir=out)
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("../../evil.txt", "boom")
    record = FrontendUpdater(cfg, None, FakePuller(p)).update(make_item())
    assert record["result"] == "failed"
    assert "非法路径" in record["error"]
    assert (out / "admin" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "evil.txt").exists()


def test_missing_output_dir_config(tmp_path):
    cfg = make_cfg()  # 未配置 FRONTEND_OUTPUT_DIR
    z = make_zip(tmp_path, {"index.html": "x"})
    record = FrontendUpdater(cfg, None, FakePuller(z)).update(make_item())
    assert record["result"] == "failed"
    assert "FRONTEND_OUTPUT_DIR" in record["error"]


def test_alias_empty_uses_repo_name(tmp_path):
    out = tmp_path / "webroot"
    cfg = make_cfg(frontend_output_dir=out)
    z = make_zip(tmp_path, {"index.html": "x"})
    record = FrontendUpdater(cfg, None, FakePuller(z)).update(
        make_item(alias=None))  # runner/checker 保证 alias 非空，这里兜底
    assert record["result"] == "success"
    assert (out / "web-admin" / "index.html").exists()
