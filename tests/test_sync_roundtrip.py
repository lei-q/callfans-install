"""沙箱 MySQL roundtrip 集成测试（M2 验收闸门：diff → apply → 再 diff 必须为空）。

需本机 docker：CALLFANS_INTEGRATION=1 .venv/bin/python -m pytest tests/test_sync_roundtrip.py
默认跳过（CI 容器内无 docker）。
"""

import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CALLFANS_INTEGRATION") != "1",
    reason="沙箱 MySQL 集成测试需 CALLFANS_INTEGRATION=1 与本机 docker",
)

CLOUD_DDL = [
    "CREATE DATABASE IF NOT EXISTS std",
    """CREATE TABLE std.sys_config(
        id INT NOT NULL AUTO_INCREMENT,
        k VARCHAR(64) NOT NULL,
        label VARCHAR(64) NOT NULL,
        remark TEXT,
        PRIMARY KEY (id),
        KEY idx_label (label)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE std.sys_dict(
        code VARCHAR(32) NOT NULL,
        value TEXT,
        PRIMARY KEY (code),
        KEY idx_value (value(64))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

# 本地现状：sys_config 缺 remark/idx_label 且 label 是 128；sys_dict 整表缺失；sys_old 多余
LOCAL_DDL = [
    "CREATE DATABASE IF NOT EXISTS biz",
    """CREATE TABLE biz.sys_config(
        id INT NOT NULL AUTO_INCREMENT,
        k VARCHAR(64) NOT NULL,
        label VARCHAR(128) NOT NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "CREATE TABLE biz.sys_old(id INT NOT NULL PRIMARY KEY)",
]


def _sh(*args) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} 失败: {r.stderr.strip()[:200]}")
    return r.stdout


def _start(name: str) -> int:
    _sh("docker", "run", "--rm", "-d", "--name", name, "-P",
        "-e", "MYSQL_ROOT_PASSWORD=root", "mysql:8.0")
    out = _sh("docker", "port", name, "3306")
    return int(out.strip().splitlines()[0].split(":")[-1])


def _wait_ready(name: str) -> None:
    for _ in range(90):
        r = subprocess.run(
            ["docker", "exec", name, "mysqladmin", "ping", "-h127.0.0.1", "-uroot", "-proot"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and "alive" in r.stdout:
            return
        time.sleep(2)
    raise RuntimeError(f"{name} 未就绪")


def _exec_sql(port: int, statements: list[str]) -> None:
    import pymysql

    conn = pymysql.connect(host="127.0.0.1", port=port, user="root",
                           password="root", charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def sandbox():
    import random

    suffix = f"{random.randint(1000, 9999)}"
    cloud_name, local_name = f"cf-sync-c{suffix}", f"cf-sync-l{suffix}"
    cloud_port = local_port = None
    try:
        cloud_port = _start(cloud_name)
        _wait_ready(cloud_name)
        local_port = _start(local_name)
        _wait_ready(local_name)
        _exec_sql(cloud_port, CLOUD_DDL)
        _exec_sql(local_port, LOCAL_DDL)
        yield cloud_port, local_port
    finally:
        for n in (cloud_name, local_name):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def test_roundtrip_diff_apply_rediff_empty(sandbox):
    from sqlalchemy import text

    from callfans.core.sync.config import DbTarget, TableRule
    from callfans.core.sync.reflect import engine_for, reflect_metadata
    from callfans.core.sync.schema_diff import diff_schemas

    cloud_port, local_port = sandbox
    cloud = engine_for(DbTarget("127.0.0.1", cloud_port, "root", "root", "std"))
    local = engine_for(DbTarget("127.0.0.1", local_port, "root", "root", "biz"))
    manifest = [
        TableRule(name="sys_config", data=True, pk="id"),
        TableRule(name="sys_dict", data=True, pk="code"),
        TableRule(name="sys_old", data=False),
    ]

    # 第一轮 diff：应检出全部构造漂移
    result = diff_schemas(reflect_metadata(cloud), reflect_metadata(local), manifest)
    kinds = {c.kind for c in result.changes}
    assert {"add_table", "add_column", "modify_column", "add_index",
            "drop_table"} <= kinds, result.summary()
    assert any(c.destructive and c.kind == "drop_table" for c in result.changes)

    # 执行全部变更
    with local.begin() as conn:
        for c in result.changes:
            conn.execute(text(c.ddl))

    # roundtrip 验收：再 diff 必须为空
    after = diff_schemas(reflect_metadata(cloud), reflect_metadata(local), manifest)
    assert after.changes == [], f"roundtrip 非空: {after.summary()}"


CLOUD_DATA = [
    "INSERT INTO std.sys_dict (code, value) VALUES ('c1','v1'),('c2','v2'),('c4','v4')",
    "INSERT INTO std.sys_config (k, label, remark) VALUES ('a','L1','r1'),('b','L2',NULL)",
]
# 本地现状：c1 旧值、c3 多余行；sys_config 已随结构同步建好（空表 → 种子）
LOCAL_DATA = [
    "INSERT INTO biz.sys_dict (code, value) VALUES ('c1','OLD'),('c3','local-only')",
]


def test_data_roundtrip_plan_apply_replan_empty(sandbox):
    """M3 闸门：数据层 roundtrip——plan → apply → 再 plan 零变更。"""
    from sqlalchemy import text

    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    # 依赖上一测试的结构同步结果（模块级 fixture 内顺序执行）
    _exec_sql(cloud_port, CLOUD_DATA)
    _exec_sql(local_port, LOCAL_DATA)

    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", "biz"),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [
        TableRule(name="sys_config", data=True, pk="id"),
        TableRule(name="sys_dict", data=True, pk="code"),
    ]
    plan = build_plan(cfg, cloud, local, manifest)
    assert not plan.fatal, plan.report_text()
    # 检出的数据漂移：c1 旧值→update；c3 本地多余→delete；
    # c2/c4 云库新增→insert ×2；sys_config 种子 2 行
    dict_t = next(d for d in plan.data_tables if d.table == "sys_dict")
    assert (dict_t.inserts, dict_t.updates, dict_t.deletes) == (2, 1, 1)
    config_t = next(d for d in plan.data_tables if d.table == "sys_config")
    assert config_t.inserts == 2

    with local.begin() as conn:
        for stmt in plan.up_statements():
            conn.execute(text(stmt))

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, f"数据 roundtrip 非空:\n{again.report_text()}"
    assert again.checksum_cloud == plan.checksum_cloud  # 校验和稳定


def test_executor_with_real_backup_and_state(sandbox, tmp_path):
    """M4 闸门：真实执行器——G1 备份落库、state 记账、apply 后收敛。"""
    from sqlalchemy import text

    from callfans.core.local import StateStore
    from callfans.core.sync.backup import BackupManager
    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.executor import SqlSyncExecutor
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    # 制造新漂移：云库加列 + 插行
    _exec_sql(cloud_port, [
        "ALTER TABLE std.sys_dict ADD COLUMN note VARCHAR(64)",
        "INSERT INTO std.sys_dict (code, value, note) VALUES ('c5','v5','n5')",
    ])

    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", "biz"),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="sys_dict", data=True, pk="code")]

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", database="biz", charset="utf8mb4")

    state = StateStore(tmp_path / "state.json")
    executor = SqlSyncExecutor(cfg, local, BackupManager(conn_factory, tmp_path / "bak"),
                               state=state, history_path=tmp_path / "h.jsonl")
    plan = build_plan(cfg, cloud, local, manifest)
    assert not plan.fatal and plan.statement_count >= 2  # 加列 + 插行

    report = executor.apply(plan)
    assert report.status == "success", report.error
    assert report.executed == report.total
    assert "sys_dict" in report.backup_tables  # G1：bak 表已建

    with local.connect() as conn:
        baks = [r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()
                if str(r[0]).startswith("_cf_bak_")]
    assert baks, "备份表未落库"

    rec = state.get("sqlsync")
    assert rec["last_status"] == "success" and rec["checksum_cloud"] == plan.checksum_cloud
    assert '"domain": "sqlsync"' in (tmp_path / "h.jsonl").read_text(encoding="utf-8")

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, again.report_text()


def test_comment_only_change_roundtrip(sandbox):
    """列注释差异的端到端验证（2026-09-20 实测盲区）。"""
    from sqlalchemy import text

    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    # 云库仅改列注释
    _exec_sql(cloud_port, [
        "ALTER TABLE std.sys_dict MODIFY COLUMN value TEXT COMMENT '标记值'",
    ])
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", "biz"),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="sys_dict", data=True, pk="code")]

    plan = build_plan(cfg, cloud, local, manifest)
    mods = [c for c in plan.schema_changes if c.kind == "modify_column"]
    assert len(mods) == 1, plan.report_text()
    assert "COMMENT '标记值'" in mods[0].ddl
    assert "注释" in mods[0].detail

    with local.begin() as conn:
        for stmt in plan.up_statements():
            conn.execute(text(stmt))

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, f"注释 roundtrip 非空: {again.report_text()}"

