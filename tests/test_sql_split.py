"""SQL 语句切分器与执行（Fake 连接，无真实 MySQL）。"""

import pytest

from callfans.core.local import StateStore
from callfans.core.updaters.sql_updater import SqlError, SqlUpdater, split_sql


class TestSplitSql:
    def test_basic_multiple(self):
        stmts = split_sql("CREATE TABLE a(id INT);\nINSERT INTO a VALUES (1);\n")
        assert stmts == ["CREATE TABLE a(id INT)", "INSERT INTO a VALUES (1)"]

    def test_semicolon_inside_string(self):
        stmts = split_sql("INSERT INTO t VALUES ('a;b;c');\nUPDATE t SET v='x';")
        assert len(stmts) == 2
        assert "'a;b;c'" in stmts[0]

    def test_escaped_quote(self):
        stmts = split_sql("INSERT INTO t VALUES ('it''s;fine');")
        assert len(stmts) == 1

    def test_double_quote_and_backtick(self):
        stmts = split_sql('INSERT INTO t VALUES ("a;b"); UPDATE `we;ird` SET x=1;')
        assert len(stmts) == 2

    def test_line_and_block_comments(self):
        text = "-- 建表\nCREATE TABLE a(id INT); # trailing\n/* block ; */ INSERT INTO a VALUES (1);"
        stmts = split_sql(text)
        assert len(stmts) == 2
        assert stmts[1].startswith("INSERT")

    def test_comment_to_eof(self):
        assert split_sql("SELECT 1; -- tail comment") == ["SELECT 1"]

    def test_no_trailing_semicolon(self):
        assert split_sql("SELECT 1") == ["SELECT 1"]

    def test_empty(self):
        assert split_sql("") == []
        assert split_sql("-- only comment\n") == []


class FakeCursor:
    def __init__(self, fail_on: int | None = None):
        self.executed: list[str] = []
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        self.executed.append(stmt)
        if self.fail_on is not None and len(self.executed) == self.fail_on:
            raise RuntimeError("syntax error near ...")


class FakeConn:
    def __init__(self, fail_on=None):
        self.cursor_obj = FakeCursor(fail_on)
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class FakePuller:
    """把 sql 文本写进目标目录模拟 oras 拉取。"""

    def __init__(self, contents: dict[str, str]):
        self.contents = contents  # {tag: sql_text}
        self.calls: list[str] = []

    def pull(self, repo, tag, dest_dir):
        from pathlib import Path

        self.calls.append(tag)
        f = Path(dest_dir) / "update.sql"
        f.write_text(self.contents[tag], encoding="utf-8")
        return [f]


def make_updater(tmp_path, contents, fail_on=None):
    from tests.test_checker import make_cfg
    from callfans.core.local import StateStore

    cfg = make_cfg(mysql_host="h", mysql_user="u", mysql_password="p", mysql_database="d")
    conn = FakeConn(fail_on)
    state = StateStore(tmp_path / "state.json")
    upd = SqlUpdater(cfg, state=state, puller=FakePuller(contents), mysql_connect=lambda: conn)
    return upd, state, conn


class TestSqlUpdater:
    def test_success_records_state_per_tag(self, tmp_path):
        from callfans.core.models import PendingItem

        item = PendingItem(
            name="callfans/db", type="sql", old="t1",
            new=["20260910150000-def5678", "20260912132921-123a066"],
        )
        upd, state, conn = make_updater(tmp_path, {
            "20260910150000-def5678": "CREATE TABLE a(id INT);",
            "20260912132921-123a066": "INSERT INTO a VALUES (1);",
        })
        record = upd.update(item)
        assert record["result"] == "success"
        assert record["executed"] == ["20260910150000-def5678", "20260912132921-123a066"]
        applied = [a["tag"] for a in state.get("sql")["callfans/db"]["applied"]]
        assert applied == ["20260910150000-def5678", "20260912132921-123a066"]
        assert conn.committed == 2 and conn.closed

    def test_failure_rolls_back_and_no_state(self, tmp_path):
        from callfans.core.models import PendingItem

        item = PendingItem(name="callfans/db", type="sql", old=None, new=["20260910150000-def5678"])
        upd, state, conn = make_updater(
            tmp_path,
            {"20260910150000-def5678": "SELECT 1;\nSELECT broken;\nSELECT 2;"},
            fail_on=2,
        )
        record = upd.update(item)
        assert record["result"] == "failed"
        assert "第 2/3 条语句失败" in record["error"]
        assert conn.rolled_back == 1
        assert state.get("sql") is None or "callfans/db" not in (state.get("sql") or {})

    def test_missing_mysql_config(self, tmp_path):
        from tests.test_checker import make_cfg
        from callfans.core.models import PendingItem

        cfg = make_cfg()  # 未配 mysql
        upd = SqlUpdater(cfg, puller=FakePuller({"t": "SELECT 1;"}), mysql_connect=lambda: FakeConn())
        record = upd.update(PendingItem(name="callfans/db", type="sql", old=None, new="t"))
        assert record["result"] == "failed"
        assert "MySQL 配置缺失" in record["error"]


class TestSqlArchiveArtifact:
    """实际制品是打包文件（zip/tar.gz 内含多个 .sql）：解包后按序执行。"""

    def _make_artifact(self, tmp_path, kind: str):
        import zipfile

        from callfans.core.updaters.archive import extract_archive  # noqa: F401 确认可导入
        import tarfile, io

        entries = {"01_create.sql": "CREATE TABLE a(id INT);", "02_insert.sql": "INSERT INTO a VALUES (1);"}
        if kind == "zip":
            p = tmp_path / "diff.zip"
            with zipfile.ZipFile(p, "w") as zf:
                for n, c in entries.items():
                    zf.writestr(n, c)
        else:
            p = tmp_path / "diff.tar.gz"
            with tarfile.open(p, "w:gz") as tf:
                for n, c in entries.items():
                    info = tarfile.TarInfo(n)
                    data = c.encode()
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        return p

    def _run(self, tmp_path, kind, fail_on=None):
        import shutil

        from callfans.core.models import PendingItem
        from tests.test_checker import make_cfg

        artifact = self._make_artifact(tmp_path, kind)

        class ZipPuller:
            def pull(self, repo, tag, dest):
                dst = dest / artifact.name
                shutil.copy(artifact, dst)
                return [dst]

        cfg = make_cfg(mysql_host="h", mysql_user="u", mysql_password="p", mysql_database="d")
        conn = FakeConn(fail_on=fail_on)
        state = StateStore(tmp_path / "state.json")
        upd = SqlUpdater(cfg, state=state, puller=ZipPuller(), mysql_connect=lambda: conn)
        record = upd.update(PendingItem(name="callfans/db", type="sql", old=None, new="T"))
        return record, state, conn

    @pytest.mark.parametrize("kind", ["zip", "tar.gz"])
    def test_archive_executes_all_sql_in_order(self, tmp_path, kind):
        record, state, conn = self._run(tmp_path, kind)
        assert record["result"] == "success", record["error"]
        # 两个文件全部执行，且按文件名顺序（切分器会去掉结尾分号）
        stmts = conn.cursor_obj.executed
        assert stmts == ["CREATE TABLE a(id INT)", "INSERT INTO a VALUES (1)"]
        assert conn.committed == 2  # 每文件独立事务
        assert [a["tag"] for a in state.get("sql")["callfans/db"]["applied"]] == ["T"]

    def test_archive_failure_reports_filename(self, tmp_path):
        record, _, conn = self._run(tmp_path, "zip", fail_on=2)
        assert record["result"] == "failed"
        assert "02_insert.sql" in record["error"]  # 指明出错文件
        assert conn.rolled_back == 1
        # FakeCursor 先记录后抛错：01 的语句 + 02 的失败语句都在 executed 里
        assert conn.cursor_obj.executed == ["CREATE TABLE a(id INT)", "INSERT INTO a VALUES (1)"]
