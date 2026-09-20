"""云端元表与运行时门面：读取/缓存兜底/工件落盘/回滚。"""

import json

import pytest

from callfans.core.sync.config import SyncPolicy, TableRule
from callfans.core.sync.manifest import (
    ManifestError, fetch_manifest, get_manifest, load_cache, save_cache,
)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return self._rows


class FakeConnCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        return FakeResult(self._rows)


class FakeEngine:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def connect(self):
        if self.error:
            raise self.error
        return FakeConnCtx(self.rows)


MANIFEST_ROWS = [
    {"name": "sys_config", "data_sync": 1, "pk": "id", "ignore_columns": "updated_at"},
    {"name": "sys_dict", "data_sync": 0, "pk": None, "ignore_columns": None},
]


def test_fetch_manifest_parses_rules():
    rules = fetch_manifest(FakeEngine(rows=MANIFEST_ROWS))
    assert [r.name for r in rules] == ["sys_config", "sys_dict"]
    assert rules[0].data is True and rules[0].ignore_columns == ["updated_at"]
    assert rules[1].data is False


def test_fetch_manifest_error_mentions_ddl():
    with pytest.raises(ManifestError, match="建表 DDL"):
        fetch_manifest(FakeEngine(error=RuntimeError("table doesn't exist")))


def test_fetch_manifest_empty_rejected():
    with pytest.raises(ManifestError, match="元表为空"):
        fetch_manifest(FakeEngine(rows=[]))


def test_cache_roundtrip(tmp_path):
    rules = [TableRule(name="t", data=True, pk="id", ignore_columns=["a"])]
    save_cache(tmp_path / "c.json", rules)
    assert load_cache(tmp_path / "c.json") == rules
    assert load_cache(tmp_path / "none.json") is None


def test_get_manifest_cloud_first_cache_fallback(tmp_path):
    cache = tmp_path / "c.json"
    # 云端正常 → 读取并写缓存
    rules = get_manifest(FakeEngine(rows=MANIFEST_ROWS), cache)
    assert len(rules) == 2
    assert cache.exists()
    # 云端故障 → 回退缓存
    broken = FakeEngine(error=RuntimeError("connection refused"))
    rules2 = get_manifest(broken, cache)
    assert len(rules2) == 2
    # 云端故障且无缓存 → 抛
    with pytest.raises(ManifestError):
        get_manifest(broken, tmp_path / "nope.json")


class TestRuntime:
    def _runtime(self, tmp_path, plan_statements=(("ALTER X;", "DOWN X;"),)):
        from callfans.core.local import StateStore
        from callfans.core.sync.config import CloudSyncConfig, DbTarget
        from callfans.core.sync.plan import SyncPlan
        from callfans.core.sync.runtime import SqlSyncRuntime

        cfg = CloudSyncConfig(
            cloud=DbTarget("h", 3306, "u", "p", "std"),
            local=DbTarget("h", 3307, "u", "p", "biz"),
            policy=SyncPolicy(),
        )
        state = StateStore(tmp_path / "state.json")

        from callfans.core.sync.schema_diff import SchemaChange
        schema = [SchemaChange("modify_column", "t", ddl, down_ddl=down)
                  for ddl, down in plan_statements]

        fake_plan = SyncPlan(run_id="20260920120000", created_at="t",
                             schema_changes=schema, data_tables=[],
                             guard_violations=[], checksum_cloud="cs")

        rt = SqlSyncRuntime(cfg, cloud_engine=FakeEngine(), local_engine=_ExecEngine(),
                            state=state, artifacts_root=tmp_path / "art",
                            backup=_StubBackup())
        rt.build = lambda: fake_plan  # 注入计划
        return rt, state

    def test_apply_saves_artifacts_and_records_state(self, tmp_path):
        rt, state = self._runtime(tmp_path)
        report = rt.apply()
        assert report.status == "success"
        rec = state.get("sqlsync")
        assert rec["last_status"] == "success"
        assert rec["checksum_cloud"] == "cs"

    def test_rollback_by_run_id(self, tmp_path):
        rt, _ = self._runtime(tmp_path)
        rt.apply()
        run_dir = tmp_path / "art" / "20260920120000"
        down = json.loads((run_dir / "down.json").read_text(encoding="utf-8"))
        assert down == ["DOWN X;"]

        report = rt.rollback("20260920120000")
        assert report.status == "rolled_back"
        assert report.executed == 1

    def test_rollback_unknown_run(self, tmp_path):
        rt, _ = self._runtime(tmp_path)
        with pytest.raises(FileNotFoundError):
            rt.rollback("nope")


class _StubBackup:
    def backup(self, tables, run_id=None):
        from callfans.core.sync.backup import BackupResult

        return BackupResult(run_id=run_id or "r", tables={t: f"bak_{t}" for t in tables})


class _ExecConn:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._engine.statements.append(str(sql))


class _ExecEngine:
    def __init__(self):
        self.statements: list[str] = []

    def connect(self):
        return _ExecConn(self)

    def begin(self):
        return _ExecConn(self)
