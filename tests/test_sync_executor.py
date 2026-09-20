"""执行器：护栏执法、G1 强制备份、失败路径、Down 回滚、审计落盘。"""

from callfans.core.local import StateStore
from callfans.core.sync.backup import BackupResult
from callfans.core.sync.data_diff import DataChange, TableDataDiff
from callfans.core.sync.executor import SqlSyncExecutor
from callfans.core.sync.guards import GuardViolation
from callfans.core.sync.plan import SyncPlan
from callfans.core.sync.schema_diff import SchemaChange


class FakeConn:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        self._engine.statements.append(str(stmt))
        if self._engine.fail_on and self._engine.fail_on in str(stmt):
            raise RuntimeError("boom")


class FakeEngine:
    def __init__(self, fail_on=None):
        self.statements: list[str] = []
        self.fail_on = fail_on

    def begin(self):
        return FakeConn(self)


class StubBackup:
    def __init__(self, error=None):
        self.calls: list = []
        self.error = error

    def backup(self, tables, run_id=None):
        self.calls.append(list(tables))
        if self.error:
            raise self.error
        names = [t[1] if isinstance(t, tuple) else t for t in tables]
        return BackupResult(run_id=run_id or "r", tables={n: f"bak_{n}" for n in names})


def _plan(violations=(), schema=(), data_tables=(), checksum="cs1"):
    return SyncPlan(
        run_id="P1", created_at="t", schema_changes=list(schema),
        data_tables=list(data_tables), guard_violations=list(violations),
        checksum_cloud=checksum,
    )


def _make(tmp_path, engine=None, backup=None, violations=(), force=False):
    state = StateStore(tmp_path / "state.json")
    executor = SqlSyncExecutor(
        cfg=None, local_engine=engine or FakeEngine(),
        backup=backup or StubBackup(), state=state,
        history_path=tmp_path / "h.jsonl",
    )
    return executor, state


class TestGuardEnforcement:
    def test_fatal_guard_aborts_without_touching(self, tmp_path):
        engine = FakeEngine()
        backup = StubBackup()
        executor, _ = _make(tmp_path, engine, backup)
        plan = _plan(violations=[GuardViolation("G2", "语句超限", fatal=True)])
        report = executor.apply(plan)
        assert report.status == "aborted_by_guard"
        assert backup.calls == [] and engine.statements == []  # 不备份不执行
        assert "--force" in report.error

    def test_force_overrides_guard_and_audits(self, tmp_path):
        engine = FakeEngine()
        backup = StubBackup()
        executor, _ = _make(tmp_path, engine, backup)
        plan = _plan(violations=[GuardViolation("G2", "语句超限", fatal=True)])
        report = executor.apply(plan, force=True)
        assert report.status == "success"
        assert report.guard_summary and "G2" in report.guard_summary  # 越过行为留痕

    def test_non_fatal_guard_proceeds(self, tmp_path):
        executor, _ = _make(tmp_path)
        plan = _plan(violations=[GuardViolation("G5", "空表跳过", fatal=False)])
        assert executor.apply(plan).status == "success"


class TestApply:
    def _full_plan(self):
        schema = [SchemaChange("add_column", "sys_dict", "ALTER ADD C", down_ddl="ALTER DROP C;")]
        data = [TableDataDiff("sys_dict", "id", changes=[
            DataChange("sys_dict", "insert", "INSERT 1;", "DELETE 1;", 1),
        ], inserts=1)]
        return _plan(schema=schema, data_tables=data)

    def test_empty_plan_success_no_backup(self, tmp_path):
        backup = StubBackup()
        executor, _ = _make(tmp_path, backup=backup)
        report = executor.apply(_plan())
        assert report.status == "success" and backup.calls == []

    def test_success_records_state_and_history(self, tmp_path):
        engine = FakeEngine()
        executor, state = _make(tmp_path, engine=engine)
        report = executor.apply(self._full_plan())
        assert report.status == "success"
        assert report.executed == report.total == 2
        assert engine.statements == ["ALTER ADD C", "INSERT 1;"]  # Up 顺序
        assert report.backup_tables == {"sys_dict": "bak_sys_dict"}
        rec = state.get("sqlsync")
        assert rec["last_status"] == "success" and rec["checksum_cloud"] == "cs1"
        line = (tmp_path / "h.jsonl").read_text(encoding="utf-8")
        assert '"domain": "sqlsync"' in line and '"status": "success"' in line

    def test_failure_midway_reports_statement(self, tmp_path):
        engine = FakeEngine(fail_on="INSERT")
        executor, state = _make(tmp_path, engine=engine)
        report = executor.apply(self._full_plan())
        assert report.status == "failed"
        assert report.executed == 1 and "第 2/2 条失败" in report.error
        assert "INSERT 1;" in report.error and "boom" in report.error
        assert state.get("sqlsync")["last_status"] == "failed"

    def test_backup_failure_never_executes(self, tmp_path):
        engine = FakeEngine()
        executor, _ = _make(tmp_path, engine=engine,
                            backup=StubBackup(error=RuntimeError("disk full")))
        report = executor.apply(self._full_plan())
        assert report.status == "backup_failed"
        assert engine.statements == []
        assert "备份失败" in report.error


class TestRollback:
    def test_down_executed_reversed_skipping_comments(self, tmp_path):
        engine = FakeEngine()
        executor, _ = _make(tmp_path, engine=engine)
        schema = [SchemaChange("add_table", "sys_dict", "CREATE T", down_ddl="DROP T;")]
        data = [TableDataDiff("sys_dict", "id", changes=[
            DataChange("sys_dict", "insert", "INSERT 1;", "-- 依赖备份", 1),
        ], inserts=1)]
        plan = _plan(schema=schema, data_tables=data)
        executor.apply(plan)
        engine.statements.clear()
        rb = executor.rollback(plan, _report())
        assert rb.status == "rolled_back"
        # Down 逆序且注释被跳过：DROP T 先于…（只有一条可执行）
        assert engine.statements == ["DROP T;"]

    def test_rollback_failure_reports_guidance(self, tmp_path):
        engine = FakeEngine(fail_on="DROP")
        executor, _ = _make(tmp_path, engine=engine)
        schema = [SchemaChange("add_table", "t", "CREATE T", down_ddl="DROP T;")]
        plan = _plan(schema=schema)
        rb = executor.rollback(plan, _report())
        assert rb.status == "rolled_back"
        assert rb.error and "备份表手工恢复" in rb.error


def _report():
    from callfans.core.sync.executor import SyncReport

    return SyncReport(run_id="r", plan_id="P1", status="success")
