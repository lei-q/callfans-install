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


def reflect_metadata(engine: Engine, only: list[str] | None = None) -> MetaData:
    """反射元数据；only 限定清单表（云库侧用，避免拉全库）。"""
    md = MetaData()
    md.reflect(bind=engine, only=only if only is not None else None)
    return md


def table_names(md: MetaData) -> set[str]:
    return set(md.tables.keys())


def visible_tables(engine: Engine) -> set[str]:
    """G3 用：数据库实际可见的表集合（不依赖反射全量开销）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SHOW TABLES")).fetchall()
    return {r[0] for r in rows}


def row_count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0)
