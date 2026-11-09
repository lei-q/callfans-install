"""连接与元数据反射（M1）：云库只读 / B 库最小权限，TLS 依配置。"""

from __future__ import annotations

import logging

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine, URL

from .config import DbTarget

log = logging.getLogger(__name__)


def engine_for(target: DbTarget, pool_pre_ping: bool = True) -> Engine:
    connect_args: dict = {"charset": "utf8mb4"}
    if target.ca:
        connect_args["ssl"] = {"ca": target.ca}
    url = URL.create(
        "mysql+pymysql",
        username=target.user, password=target.password,
        host=target.host, port=target.port, database=target.database,
    )
    return create_engine(url, connect_args=connect_args, pool_pre_ping=pool_pre_ping)


SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys", "callfans_sync"}


def _partitioned_table_names(engine: Engine, schema: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT table_name FROM information_schema.partitions "
            "WHERE table_schema = :d AND partition_name IS NOT NULL"), {"d": schema})
        return {str(r[0]) for r in rows}


def _drop_bogus_partition_options(engine: Engine, md: MetaData,
                                  schema: str | None) -> list[str]:
    """清掉误反射的分区定义。

    SQLAlchemy 的 MySQL 反射用正则从 SHOW CREATE TABLE 里抓 "PARTITION BY"：
    列注释里出现这几个字（如"…PARTITION BY url + ORDER BY collection_time）"）
    就会被当成表分区，编译出的 CREATE TABLE 带上非法分区子句（1064）。
    以 information_schema.partitions 为准：表中确无分区行的一律丢弃。
    """
    tables = [t for t in md.tables.values()
              if t.dialect_options["mysql"].get("partition_by")]
    if not tables:
        return []
    by_schema: dict[str, list] = {}
    for t in tables:
        by_schema.setdefault(t.schema or schema or (engine.url.database or ""), []).append(t)
    fixed: list[str] = []
    for sch, ts in by_schema.items():
        if not sch:
            continue  # 无库名无法核实，保守保留
        partitioned = _partitioned_table_names(engine, sch)
        for t in ts:
            if t.name in partitioned:
                continue
            t.dialect_options["mysql"].pop("partition_by")
            fixed.append(t.name)
    return fixed


def reflect_metadata(engine: Engine, only: list[str] | None = None,
                     schema: str | None = None) -> MetaData:
    """反射元数据；only 限定清单表；schema= 库名（多库模式跨库反射）。"""
    md = MetaData()
    md.reflect(bind=engine, only=only if only is not None else None, schema=schema)
    fixed = _drop_bogus_partition_options(engine, md, schema)
    if fixed:
        log.warning("忽略被注释文本误判的分区定义（表中实际无分区）: %s", ", ".join(fixed))
    return md


def lookup_table(md: MetaData, db: str | None, name: str):
    """反射后 tables 的键是 'db.name'（带 schema 时）或 'name'。"""
    if db:
        return md.tables.get(f"{db}.{name}")
    return md.tables.get(name)


def table_names(md: MetaData) -> set[str]:
    return {t.split(".", 1)[-1] for t in md.tables.keys()}


def database_exists(engine: Engine, db: str) -> bool:
    """配置的目标库是否存在（不存在时回退多库自动发现）。"""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = :db"
        ), {"db": db}).fetchone()
    return row is not None


def list_databases(engine: Engine, extra_exclude: set[str] | None = None) -> list[str]:
    """非系统库列表（多库同步范围）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SHOW DATABASES")).fetchall()
    exclude = SYSTEM_DBS | set(extra_exclude or ())
    return sorted(str(r[0]) for r in rows if str(r[0]) not in exclude)


def tables_in(engine: Engine, db: str) -> set[str]:
    """指定库的表集合（G3/范围判定，information_schema 轻查询）。"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :db"
        ), {"db": db}).fetchall()
    return {str(r[0]) for r in rows}


def row_count(engine: Engine, db: str | None, table: str) -> int:
    fq = f"`{db}`.`{table}`" if db else f"`{table}`"
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {fq}")).scalar() or 0)
