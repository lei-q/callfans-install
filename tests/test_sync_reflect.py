"""反射清洗：SQLAlchemy 会把注释里的 PARTITION BY 误当分区定义。"""

from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.schema import CreateTable

from callfans.core.sync.reflect import _drop_bogus_partition_options
from callfans.core.sync.schema_diff import _DIALECT


class _FakeConn:
    def __init__(self, names):
        self.names = names

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return [(n,) for n in self.names]


class _FakeEngine:
    def __init__(self, partitioned):
        self.partitioned = partitioned
        self.url = type("U", (), {"database": None})()

    def connect(self):
        return _FakeConn(self.partitioned)


def test_bogus_partition_option_dropped_and_real_kept():
    """信息架构无分区行 → 丢弃误反射值；确有分区 → 保留。"""
    md = MetaData()
    bogus = Table("t_idx_comment", md, Column("id", Integer), schema="db",
                  mysql_partition_by="url + ORDER BY collection_time）',")
    real = Table("t_real", md, Column("id", Integer), schema="db",
                 mysql_partition_by="RANGE (id)")
    fixed = _drop_bogus_partition_options(_FakeEngine({"t_real"}), md, "db")
    assert fixed == ["t_idx_comment"]
    assert bogus.dialect_options["mysql"].get("partition_by") is None
    assert real.dialect_options["mysql"]["partition_by"] == "RANGE (id)"
    ddl = str(CreateTable(bogus).compile(dialect=_DIALECT))
    assert "PARTITION" not in ddl


def test_no_partition_options_is_noop():
    md = MetaData()
    Table("t", md, Column("id", Integer))
    assert _drop_bogus_partition_options(_FakeEngine(set()), md, None) == []
