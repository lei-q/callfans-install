"""连接与元数据反射（M1）：云库只读 / B 库最小权限，TLS 依配置。"""

from __future__ import annotations

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine, URL

from .config import DbTarget


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


def reflect_metadata(engine: Engine, only: list[str] | None = None,
                     schema: str | None = None) -> MetaData:
    """反射元数据；only 限定清单表；schema= 库名（多库模式跨库反射）。"""
    md = MetaData()
    md.reflect(bind=engine, only=only if only is not None else None, schema=schema)
    return md


def lookup_table(md: MetaData, db: str | None, name: str):
    """反射后 tables 的键是 'db.name'（带 schema 时）或 'name'。"""
    if db:
        return md.tables.get(f"{db}.{name}")
    return md.tables.get(name)


def table_names(md: MetaData) -> set[str]:
    return {t.split(".", 1)[-1] for t in md.tables.keys()}


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
