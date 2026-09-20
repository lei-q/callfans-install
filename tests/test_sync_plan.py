"""SyncPlan 编排：Up/Down 有序、影响统计、护栏接入（G2/G4）。"""

from sqlalchemy import Column, Integer, MetaData, String, Table, VARCHAR

from callfans.core.sync.config import SyncPolicy, TableRule
from callfans.core.sync.data_diff import DataChange, TableDataDiff
from callfans.core.sync.guards import GuardViolation
from callfans.core.sync.plan import SyncPlan
from callfans.core.sync.schema_diff import SchemaChange


def _plan(schema_changes, data_tables, violations=(), checksum="abc123"):
    return SyncPlan(
        run_id="20260920120000", created_at="2026-09-20T12:00:00+00:00",
        schema_changes=schema_changes, data_tables=data_tables,
        guard_violations=list(violations), checksum_cloud=checksum,
    )


def _data_table(table, **kw):
    defaults = dict(table=table, rule_pk="id", changes=[], inserts=0, updates=0,
                    deletes=0, cloud_rows=0, local_rows=0, skipped_reason=None)
    defaults.update(kw)
    return TableDataDiff(**defaults)


class TestOrdering:
    def test_up_order_structure_dml_index(self):
        schema = [
            SchemaChange("add_table", "sys_dict", "CREATE TABLE A", down_ddl="DROP A;"),
            SchemaChange("add_index", "sys_dict", "CREATE INDEX I", down_ddl="DROP INDEX I;"),
            SchemaChange("add_column", "sys_config", "ALTER ADD C", down_ddl="ALTER DROP C;"),
        ]
        data = [_data_table("sys_dict", changes=[
            DataChange("sys_dict", "insert", "INSERT 1;", "DELETE 1;", 1),
        ])]
        plan = _plan(schema, data)
        ups = plan.up_statements()
        assert ups == ["CREATE TABLE A", "ALTER ADD C", "INSERT 1;", "CREATE INDEX I"]

    def test_down_is_strict_reverse(self):
        schema = [
            SchemaChange("add_table", "t1", "CREATE t1", down_ddl="DOWN-t1;"),
            SchemaChange("add_index", "t1", "CREATE idx", down_ddl="DOWN-idx;"),
        ]
        data = [_data_table("t1", changes=[
            DataChange("t1", "insert", "INSERT 1;", "DOWN-ins-1;", 1),
        ])]
        plan = _plan(schema, data)
        assert plan.down_statements() == ["DOWN-idx;", "DOWN-ins-1;", "DOWN-t1;"]


class TestImpact:
    def test_counts_and_destructive(self):
        schema = [SchemaChange("drop_table", "sys_old", "DROP TABLE", destructive=True)]
        data = [_data_table("sys_dict", changes=[
            DataChange("sys_dict", "insert", "I1", "D1", 1),
            DataChange("sys_dict", "delete", "DEL", "INS", 9),
        ], inserts=1, deletes=1)]
        plan = _plan(schema, data)
        assert plan.statement_count == 3
        assert plan.affected_tables == {"sys_old", "sys_dict"}
        assert len(plan.destructive) == 2  # drop_table + delete
        text = plan.report_text()
        assert "增 1" in text and "删 1" in text and "破坏性语句 2 条" in text


class TestGuards:
    def test_fatal_flag_and_report(self):
        v = GuardViolation("G2", "变更语句数 999 超过上限 200", fatal=True)
        plan = _plan([], [], violations=[v])
        assert plan.fatal
        assert "[G2] 中止 —" in plan.report_text()

    def test_non_fatal_report(self):
        v = GuardViolation("G5", "云库表 t 为空", fatal=False)
        plan = _plan([], [_data_table("t", skipped_reason="G5：云库表为空，跳过数据同步")],
                     violations=[v])
        assert not plan.fatal
        assert "[G5] 跳过" in plan.report_text()
        assert "G5：云库表为空" in plan.report_text()


def test_checksum_covers_databases():
    """校验和包含库与表集合（多库维度）。"""
    from callfans.core.sync.plan import _checksum

    class _Eng:
        def __init__(self, tables):
            self._tables = tables

        def connect(self):
            class _C:
                def __init__(self, tables):
                    self._tables = tables

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def execute(self, sql, params=None):
                    class _R:
                        def __init__(self, rows):
                            self._rows = rows

                        def fetchall(self):
                            return self._rows

                    db = params.get("db") if params else "std"
                    return _R([(t,) for t in self._tables.get(db, [])])

            return _C(self._tables)
            class _C:
                def __init__(self, tables):
                    self._tables = tables

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def execute(self, sql, params=None):
                    class _R:
                        def __init__(self, rows):
                            self._rows = rows

                        def fetchall(self):
                            return self._rows

                    db = params.get("db") if params else "std"
                    return _R([(t,) for t in self._tables.get(db, [])])

            return _C(self._tables)

    from callfans.core.sync.config import CloudSyncConfig, DbTarget
    cfg = CloudSyncConfig(cloud=DbTarget("c", 1, "u", "p", "std"),
                          local=DbTarget("l", 2, "u", "p", ""))
    e = _Eng({"std": ["t1", "t2"]})
    h1 = _checksum(cfg, ["std"], e, [])
    h2 = _checksum(cfg, ["std"], e, [])
    e2 = _Eng({"std": ["t1", "t2"], "biz2": ["u1"]})
    h3 = _checksum(cfg, ["std", "biz2"], e2, [])
    assert h1 == h2 and h1 != h3
