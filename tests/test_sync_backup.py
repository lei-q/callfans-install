"""备份（G1）：Fake 连接验证 SQL 生成、标识符防护、本地 dump 与恢复。"""

from pathlib import Path

import pytest

from callfans.core.sync.backup import BackupError, BackupManager, new_run_id


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append(sql)

    def fetchone(self):
        return (None, self._conn.create_stmt)

    def fetchall(self):
        return self._conn.rows


class FakeConn:
    def __init__(self, create_stmt="CREATE TABLE t (id int)", rows=None):
        self.create_stmt = create_stmt
        self.rows = rows or []
        self.statements: list[str] = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.statements.append("COMMIT")

    def rollback(self):
        self.statements.append("ROLLBACK")

    def close(self):
        pass


def test_backup_creates_bak_and_dump(tmp_path):
    conn = FakeConn(rows=[(1, "a"), (2, "b"), (3, None)])
    mgr = BackupManager(lambda: conn, tmp_path)
    run_id = new_run_id()

    result = mgr.backup(["sys_config"], run_id=run_id)

    bak = result.tables["sys_config"]
    assert bak.startswith("_cf_bak_") and "sys_config" in bak
    assert len(bak) <= 64
    joined = "\n".join(conn.statements)
    assert f"CREATE TABLE `{bak}` LIKE `sys_config`" in joined
    assert f"INSERT INTO `{bak}` SELECT * FROM `sys_config`" in joined
    assert "COMMIT" in joined
    # 本地 dump：建表语句 + 值转义（NULL / 字符串引号）
    dump = (tmp_path / run_id / "sys_config.sql").read_text(encoding="utf-8")
    assert dump.startswith("-- backup of sys_config")
    assert "CREATE TABLE" in dump
    assert "'a'" in dump and "NULL" in dump


def test_backup_rejects_bad_identifier(tmp_path):
    mgr = BackupManager(lambda: FakeConn(), tmp_path)
    with pytest.raises(BackupError, match="非法表名"):
        mgr.backup(["evil; DROP TABLE x"])


def test_restore_via_rename(tmp_path):
    conn = FakeConn()
    mgr = BackupManager(lambda: conn, tmp_path)
    result = mgr.backup(["sys_config"], run_id="20260920120000")
    conn.statements.clear()
    mgr.restore("sys_config", result)
    joined = "\n".join(conn.statements)
    assert "DROP TABLE IF EXISTS `sys_config`" in joined
    assert f"RENAME TABLE `{result.tables['sys_config']}` TO `sys_config`" in joined


def test_restore_without_bak_raises(tmp_path):
    from callfans.core.sync.backup import BackupResult

    mgr = BackupManager(lambda: FakeConn(), tmp_path)
    result = BackupResult(run_id="x", tables={}, dump_dir=None)
    with pytest.raises(BackupError, match="无 bak 表"):
        mgr.restore("t", result)


def test_literal_escaping(tmp_path):
    from datetime import datetime

    from callfans.core.sync.backup import _literal

    assert _literal(None) == "NULL"
    assert _literal(42) == "42"
    assert _literal(1.5) == "1.5"
    assert _literal("it's") == "'it''s'"
    assert _literal("a\\b") == "'a\\\\b'"
    assert _literal(b"\x00\x01") == "X'0001'"
    assert _literal(datetime(2026, 9, 20, 12, 0, 0)).startswith("'2026-09-20 12:00")
