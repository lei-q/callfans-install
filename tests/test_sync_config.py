"""sqlsync 配置：.env 键解析、策略默认与覆盖、元表行解析。"""

import pytest

from callfans.core.sync.config import (
    CloudSyncConfig, SyncConfigError, SyncPolicy, TableRule,
)


def _set_env(monkeypatch, **kv):
    monkeypatch.setenv("CLOUD_DB_NAME", "std")
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_USER", "rw")
    monkeypatch.setenv("MYSQL_PASSWORD", "pw2")
    monkeypatch.setenv("MYSQL_DATABASE", "biz")
    for k, v in kv.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_from_env_defaults(monkeypatch):
    _set_env(monkeypatch)
    cfg = CloudSyncConfig.from_env()
    # 云端连接写死于代码（2026-09-21），仅库名走 .env
    assert cfg.cloud.host == "47.87.66.98"
    assert cfg.cloud.port == 13322
    assert cfg.cloud.user == "client_sync"
    assert cfg.cloud.database == "std" and cfg.cloud.ca is None
    assert cfg.local.database == "biz"
    assert cfg.policy == SyncPolicy()  # 已确认的护栏默认值
    assert cfg.interval_hours == 6.0


def test_from_env_local_account_defaults(monkeypatch):
    """B 库账号可不填：默认 127.0.0.1 / root / callfans@123。"""
    monkeypatch.setenv("CLOUD_DB_NAME", "std")
    monkeypatch.setenv("MYSQL_DATABASE", "biz")
    for k in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_PORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = CloudSyncConfig.from_env()
    assert (cfg.local.host, cfg.local.port, cfg.local.user,
            cfg.local.password) == ("127.0.0.1", 3306, "root", "callfans@123")


def test_from_env_policy_overrides(monkeypatch):
    _set_env(monkeypatch,
             SQLSYNC_MAX_STATEMENTS="50",
             SQLSYNC_MAX_TABLE_RATIO="0.5",
             SQLSYNC_MAX_DELETE_ROWS="10")
    cfg = CloudSyncConfig.from_env()
    assert (cfg.policy.max_statements, cfg.policy.max_table_ratio,
            cfg.policy.max_delete_rows) == (50, 0.5, 10)


def test_from_env_missing_keys(monkeypatch):
    _set_env(monkeypatch, CLOUD_DB_NAME=None, MYSQL_DATABASE=None)
    with pytest.raises(SyncConfigError, match="CLOUD_DB_NAME"):
        CloudSyncConfig.from_env()


def test_from_env_invalid_threshold(monkeypatch):
    _set_env(monkeypatch, SQLSYNC_MAX_STATEMENTS="0")
    with pytest.raises(SyncConfigError, match="阈值非法"):
        CloudSyncConfig.from_env()


def test_table_rule_from_manifest_row():
    rule = TableRule.from_manifest_row(
        {"name": " sys_config ", "data_sync": 1, "pk": "id",
         "ignore_columns": "updated_at, remark"}
    )
    assert rule.name == "sys_config"
    assert rule.data is True and rule.pk == "id"
    assert rule.ignore_columns == ["updated_at", "remark"]
    # data_sync=0 → 仅结构
    assert TableRule.from_manifest_row({"name": "t", "data_sync": 0}).data is False
    # 空 pk / 空 ignore
    rule2 = TableRule.from_manifest_row({"name": "t", "data_sync": 1, "pk": "", "ignore_columns": ""})
    assert rule2.pk is None and rule2.ignore_columns == []
