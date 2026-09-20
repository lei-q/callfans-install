"""sqlsync 配置：.env（连接与策略）+ 云端元表（表清单，Q3）。

.env 追加键（凭据沿用既有惯例，不落代码库）：
  CLOUD_DB_HOST/PORT/USER/PASSWORD/NAME/CA   云库（只读账号）
  MYSQL_*                                    B 库（最小权限账号）
  SQLSYNC_INTERVAL_HOURS / SQLSYNC_MAX_STATEMENTS / SQLSYNC_MAX_TABLE_RATIO /
  SQLSYNC_MAX_DELETE_ROWS                    策略（护栏阈值可调，护栏本身不可关）

云端元表 DDL（管理员在建标库时执行一次）：
  CREATE DATABASE IF NOT EXISTS callfans_sync;
  CREATE TABLE callfans_sync.tables(
    name VARCHAR(128) PRIMARY KEY,     -- 表名
    data_sync TINYINT(1) NOT NULL DEFAULT 1,  -- 是否同步数据（1=结构+数据）
    pk VARCHAR(64),                    -- 主键/唯一键列（数据同步必需）
    ignore_columns VARCHAR(512)        -- 忽略列，逗号分隔
  );
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    return val.strip()


class SyncConfigError(Exception):
    """sqlsync 配置缺失或非法。"""


@dataclass
class DbTarget:
    host: str
    port: int
    user: str
    password: str
    database: str
    ca: str | None = None  # TLS CA 文件路径（Q8）

    def missing_keys(self) -> list[str]:
        return [k for k, v in {
            "host": self.host, "user": self.user,
            "password": self.password, "database": self.database,
        }.items() if not v]


@dataclass
class SyncPolicy:
    # 护栏阈值（§1.2，2026-09-20 已确认默认值；阈值可调，护栏不可禁用）
    max_statements: int = 200      # G2：单次语句数上限
    max_table_ratio: float = 0.30  # G2：涉及表占清单比例上限
    max_delete_rows: int = 1000    # G4：单表 DELETE 行数上限
    max_rows_per_table: int = 1_000_000  # 超限仅比结构不比数据
    no_pk_tables: str = "skip_warn"      # error | skip_warn
    exclude_dbs: list[str] = field(default_factory=list)  # 多库排除名单


def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


@dataclass
class TableRule:
    name: str
    data: bool = True
    pk: str | None = None
    ignore_columns: list[str] = field(default_factory=list)
    db: str | None = None  # 限定库；None = 适用所有库（表存在才生效）

    @classmethod
    def from_manifest_row(cls, row: dict) -> "TableRule":
        def _csv(v) -> list[str]:
            if not v:
                return []
            return [x.strip() for x in str(v).split(",") if x.strip()]

        return cls(
            name=str(row["name"]).strip(),
            data=bool(row.get("data_sync", 1)),
            pk=(str(row["pk"]).strip() or None) if row.get("pk") else None,
            ignore_columns=_csv(row.get("ignore_columns")),
            db=(str(row["db"]).strip() or None) if row.get("db") else None,
        )


# 云端标准库连接（2026-09-21 决策：写死在代码，不维护在配置文件；
# 仅库名 CLOUD_DB_NAME 仍从 .env 读取，TLS CA 可选）
_CLOUD_DEFAULT = DbTarget(
    host="47.87.66.98", port=13322,
    user="client_sync", password="Admin@123", database="",
)

# B 库账号默认值（与主配置一致，.env 可不填）
_DEFAULT_MYSQL_HOST = "127.0.0.1"
_DEFAULT_MYSQL_USER = "root"
_DEFAULT_MYSQL_PASSWORD = "callfans@123"


@dataclass
class CloudSyncConfig:
    cloud: DbTarget
    local: DbTarget
    policy: SyncPolicy = field(default_factory=SyncPolicy)
    interval_hours: float = 6.0

    @classmethod
    def from_env(cls) -> "CloudSyncConfig":
        cloud = DbTarget(
            host=_CLOUD_DEFAULT.host, port=_CLOUD_DEFAULT.port,
            user=_CLOUD_DEFAULT.user, password=_CLOUD_DEFAULT.password,
            database=_env("CLOUD_DB_NAME", ""),  # 空 = 多库自动发现（2026-09-21）
            ca=_env("CLOUD_DB_CA"),
        )
        local = DbTarget(
            host=_env("MYSQL_HOST", _DEFAULT_MYSQL_HOST),
            port=int(_env("MYSQL_PORT", "3306")),
            user=_env("MYSQL_USER", _DEFAULT_MYSQL_USER),
            password=_env("MYSQL_PASSWORD", _DEFAULT_MYSQL_PASSWORD),
            database="",  # B 库名与云库同名，不再单独配置
            ca=_env("MYSQL_CA"),
        )
        policy = SyncPolicy(
            max_statements=int(_env("SQLSYNC_MAX_STATEMENTS", "200")),
            max_table_ratio=float(_env("SQLSYNC_MAX_TABLE_RATIO", "0.30")),
            max_delete_rows=int(_env("SQLSYNC_MAX_DELETE_ROWS", "1000")),
            max_rows_per_table=int(_env("SQLSYNC_MAX_ROWS_PER_TABLE", "1000000")),
            no_pk_tables=_env("SQLSYNC_NO_PK_TABLES", "skip_warn"),
            exclude_dbs=_csv(_env("SQLSYNC_EXCLUDE_DBS", "")),  # 多库模式排除名单
        )
        if policy.max_statements < 1 or not (0 < policy.max_table_ratio <= 1):
            raise SyncConfigError("SQLSYNC_* 护栏阈值非法")
        return cls(
            cloud=cloud, local=local, policy=policy,
            interval_hours=float(_env("SQLSYNC_INTERVAL_HOURS", "6")),
        )
