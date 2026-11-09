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
    "CREATE DATABASE IF NOT EXISTS std",
    """CREATE TABLE std.sys_config(
        id INT NOT NULL AUTO_INCREMENT,
        k VARCHAR(64) NOT NULL,
        label VARCHAR(128) NOT NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "CREATE TABLE std.sys_old(id INT NOT NULL PRIMARY KEY)",
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
    local = engine_for(DbTarget("127.0.0.1", local_port, "root", "root", "std"))
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
    "INSERT INTO std.sys_dict (code, value) VALUES ('c1','OLD'),('c3','local-only')",
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
        local=DbTarget("127.0.0.1", local_port, "root", "root", "std"),
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
        local=DbTarget("127.0.0.1", local_port, "root", "root", "std"),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="sys_dict", data=True, pk="code")]

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", database="std", charset="utf8mb4")

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
        local=DbTarget("127.0.0.1", local_port, "root", "root", "std"),
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


def test_multi_db_roundtrip(sandbox):
    """多库模式（不配库名）：自动发现 std/std2；B 缺库 → 建库 + 建表 + 种子。"""
    from sqlalchemy import text

    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std2",
        """CREATE TABLE std2.province(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(32) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        "INSERT INTO std2.province (name) VALUES ('广东'),('浙江')",
    ])

    # 多库模式：cloud.database 留空
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", ""),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [
        TableRule(name="sys_dict", data=True, pk="code"),      # std 里有
        TableRule(name="province", data=True, pk="id"),         # std2 里有（rule.db=None 适用所有库）
    ]

    plan = build_plan(cfg, cloud, local, manifest)
    assert not plan.fatal, plan.report_text()
    assert plan.databases == ["std", "std2"]                    # 自动发现
    kinds = {c.kind for c in plan.schema_changes}
    assert "create_database" in kinds                           # std2 缺库 → 建库
    create_db = next(c for c in plan.schema_changes if c.kind == "create_database")
    assert create_db.table == "std2"
    assert any(c.db == "std2" and c.kind == "add_table" for c in plan.schema_changes)
    prov = next(d for d in plan.data_tables if d.table == "province")
    assert prov.inserts == 2                                    # 种子数据

    with local.begin() as conn:
        for stmt in plan.up_statements():
            conn.execute(text(stmt))

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, f"多库 roundtrip 非空:\n{again.report_text()}"


def test_multi_db_index_change_carries_db_for_backup(sandbox, tmp_path):
    """多库模式：既有表的索引漂移必须带库名。

    2026-11-04 现场：add_index 的 db 为空 → G1 备份按空库执行
    CREATE TABLE ... LIKE，报 (1046, 'No database selected')，整次更新中止。
    """
    from sqlalchemy import text

    from callfans.core.sync.backup import BackupManager
    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.executor import SqlSyncExecutor
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "ALTER TABLE std.sys_dict DROP INDEX idx_value",
        "ALTER TABLE std.sys_dict ADD INDEX idx_code (code(32))",
    ])
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", ""),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="sys_dict", data=True, pk="code")]

    plan = build_plan(cfg, cloud, local, manifest)
    idx = [c for c in plan.schema_changes if c.kind in ("add_index", "drop_index")]
    assert idx, plan.report_text()
    assert all(c.db == "std" for c in idx)          # 备份/DDL 都靠它定位库
    assert all("`std`" in c.ddl for c in idx)       # DDL 自带库名（连接不选库）
    assert ("std", "sys_dict") in plan.backup_targets

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", charset="utf8mb4")  # 故意不选库

    executor = SqlSyncExecutor(cfg, local, BackupManager(conn_factory, tmp_path / "bak"),
                               history_path=tmp_path / "h.jsonl")
    report = executor.apply(plan)
    assert report.status == "success", report.error
    assert "sys_dict" in report.backup_tables

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, again.report_text()


def test_colon_and_percent_literals_roundtrip(sandbox, tmp_path):
    """列注释/默认值含 :qty 与 100%：原样执行且再 plan 零变更（2026-11-05 现场）。"""
    from callfans.core.sync.backup import BackupManager
    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.executor import SqlSyncExecutor
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std9",
        """CREATE TABLE std9.pct_demo(
            id INT NOT NULL AUTO_INCREMENT,
            note VARCHAR(100) COMMENT 'JSON:{"topicBizId":qty},100%',
            d VARCHAR(10) NOT NULL DEFAULT '100%',
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ])
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", ""),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="pct_demo")]

    plan = build_plan(cfg, cloud, local, manifest)
    assert any(c.kind == "add_table" for c in plan.schema_changes), plan.report_text()

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", charset="utf8mb4")

    executor = SqlSyncExecutor(cfg, local, BackupManager(conn_factory, tmp_path / "bak"),
                               history_path=tmp_path / "h.jsonl")
    report = executor.apply(plan)
    assert report.status == "success", report.error

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, f"转义漂移:\n{again.report_text()}"


def test_index_comment_partition_text_not_treated_as_partition(sandbox, tmp_path):
    """索引注释里出现 PARTITION BY 不能被当成表分区（1064，2026-11-09 现场）。"""
    from callfans.core.sync.backup import BackupManager
    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.executor import SqlSyncExecutor
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std10",
        """CREATE TABLE std10.part_idx(
            id INT NOT NULL,
            account_id VARCHAR(32),
            PRIMARY KEY (id),
            KEY idx_acc (account_id)
                COMMENT '优化 PARTITION BY account_id + ORDER BY id）'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ])
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", ""),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="part_idx")]

    plan = build_plan(cfg, cloud, local, manifest)
    ddl = next(c.ddl for c in plan.schema_changes if c.kind == "add_table")
    assert "PARTITION" not in ddl, ddl

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", charset="utf8mb4")

    executor = SqlSyncExecutor(cfg, local, BackupManager(conn_factory, tmp_path / "bak"),
                               history_path=tmp_path / "h.jsonl")
    report = executor.apply(plan)
    assert report.status == "success", report.error

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, again.report_text()


def test_first_full_sync_downgrades_g2(sandbox):
    """首次全量/中断重跑 → G2 超限仅告警；已收敛的库有大改时仍 fatal。

    2026-10-27/11-03 确认：首轮同步天然是几十上百张表的变更，失败后重跑
    也必须能继续收敛，不应被 G2 拦截；2026-11-04 补充：重跑计划里夹带的
    个别 modify_column 不能推翻"首次同步"判定（现场 mobile_device 默认值）。
    """
    from sqlalchemy import text

    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std5",
        """CREATE TABLE std5.province(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(32) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE std5.city(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(32) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ])
    manifest = [TableRule(name="province", data=True, pk="id"),
                TableRule(name="city", data=True, pk="id")]
    policy = SyncPolicy(max_statements=0)  # 任何语句都超限，放大判定

    # 1) B 库无 std5 → 首次全量：G2 存在但均为告警
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std5"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=policy,
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    plan = build_plan(cfg, cloud, local, manifest)
    assert plan.statement_count > 0
    assert plan.guard_violations and all(not v.fatal for v in plan.guard_violations)
    assert not plan.fatal, plan.report_text()
    assert any("首次全量" in v.message for v in plan.guard_violations)

    # 1b) 模拟首次同步中断后重跑（已建部分表）→ 仍放行（缺表过半）
    with local.begin() as conn:
        for stmt in plan.up_statements()[:2]:   # CREATE DATABASE + 第一张表
            conn.execute(text(stmt))
    retry = build_plan(cfg, engine_for(cfg.cloud), engine_for(cfg.local), manifest)
    assert retry.statement_count > 0
    assert not retry.fatal, retry.report_text()

    # 2) std6 大部分目标表已存在（仅个别缺表 + 列修改）→ 不属于首次同步，仍 fatal
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std6",
        """CREATE TABLE std6.zzz_base(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(32) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        "CREATE TABLE std6.zzz_e1(id INT NOT NULL PRIMARY KEY)",
        "CREATE TABLE std6.zzz_e2(id INT NOT NULL PRIMARY KEY)",
        "CREATE TABLE std6.zzz_e3(id INT NOT NULL PRIMARY KEY)",
        """CREATE TABLE std6.zzz_new(
            id INT NOT NULL AUTO_INCREMENT,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ])
    _exec_sql(local_port, [
        "CREATE DATABASE IF NOT EXISTS std6",
        """CREATE TABLE std6.zzz_base(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(8) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        "CREATE TABLE std6.zzz_e1(id INT NOT NULL PRIMARY KEY)",
        "CREATE TABLE std6.zzz_e2(id INT NOT NULL PRIMARY KEY)",
        "CREATE TABLE std6.zzz_e3(id INT NOT NULL PRIMARY KEY)",
    ])
    cfg2 = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std6"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=policy,
    )
    manifest6 = [TableRule(name=n) for n in
                 ("zzz_base", "zzz_e1", "zzz_e2", "zzz_e3", "zzz_new")]
    plan2 = build_plan(cfg2, engine_for(cfg2.cloud), engine_for(cfg2.local), manifest6)
    assert any(c.kind == "modify_column" for c in plan2.schema_changes)
    assert plan2.statement_count > 0
    assert plan2.fatal, plan2.report_text()
    assert any(v.guard == "G2" and v.fatal for v in plan2.guard_violations)

    # 3) initial_sync=True（state 无成功记录）→ 同样的计划不再误拦：
    #    首次同步重跑常夹带个别 modify_column（2026-11-04 现场：mobile_device 默认值）
    cfg3 = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std6"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=policy,
    )
    relaxed = build_plan(cfg3, engine_for(cfg3.cloud), engine_for(cfg3.local), manifest6,
                         initial_sync=True)
    assert relaxed.statement_count == plan2.statement_count
    assert not relaxed.fatal, relaxed.report_text()
    assert any("首次全量" in v.message for v in relaxed.guard_violations)

    # 4) 同样 plan 若含破坏性变更（此处删行）→ initial_sync 也不能放行
    _exec_sql(cloud_port, ["INSERT INTO std6.zzz_e1 (id) VALUES (1)"])
    _exec_sql(local_port, ["INSERT INTO std6.zzz_e1 (id) VALUES (1),(2)"])
    p4 = build_plan(cfg3, engine_for(cfg3.cloud), engine_for(cfg3.local),
                    [TableRule(name="zzz_e1", data=True, pk="id")], initial_sync=True)
    assert any(d.deletes == 1 for d in p4.data_tables), p4.report_text()
    assert p4.fatal, p4.report_text()


def test_missing_db_created_with_real_backup(sandbox, tmp_path):
    """B 库缺库：G1 备份跳过未存在的库/表，CREATE DATABASE 能真正执行。"""
    from sqlalchemy import text

    from callfans.core.sync.backup import BackupManager
    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.executor import SqlSyncExecutor
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std3",
        """CREATE TABLE std3.province(
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(32) NOT NULL,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        "INSERT INTO std3.province (name) VALUES ('北京'),('上海')",
    ])

    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", ""),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    manifest = [TableRule(name="province", data=True, pk="id")]

    plan = build_plan(cfg, cloud, local, manifest)
    assert any(c.kind == "create_database" and c.table == "std3"
               for c in plan.schema_changes), plan.report_text()

    def conn_factory():
        import pymysql

        return pymysql.connect(host="127.0.0.1", port=local_port, user="root",
                               password="root", charset="utf8mb4")

    executor = SqlSyncExecutor(
        cfg, local, BackupManager(conn_factory, tmp_path / "bak"),
        history_path=tmp_path / "h.jsonl",
    )
    report = executor.apply(plan)
    assert report.status == "success", report.error
    assert "province" not in report.backup_tables   # 新库/新表无数据可备份

    with local.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM std3.province")).scalar()
    assert rows == 2


def test_fk_parent_created_before_child(sandbox):
    """建表顺序遵循外键依赖：父表先建（1824 Failed to open the referenced table）。"""
    from sqlalchemy import text

    from callfans.core.sync.config import CloudSyncConfig, DbTarget, SyncPolicy, TableRule
    from callfans.core.sync.plan import build_plan
    from callfans.core.sync.reflect import engine_for

    cloud_port, local_port = sandbox
    _exec_sql(cloud_port, [
        "CREATE DATABASE IF NOT EXISTS std7",
        """CREATE TABLE std7.zzz_parent(
            id BIGINT NOT NULL AUTO_INCREMENT,
            PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE std7.aaa_child(
            id BIGINT NOT NULL AUTO_INCREMENT,
            parent_id BIGINT NOT NULL,
            PRIMARY KEY (id),
            KEY fk_child_parent (parent_id),
            CONSTRAINT fk_child_parent FOREIGN KEY (parent_id)
                REFERENCES std7.zzz_parent(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ])
    cfg = CloudSyncConfig(
        cloud=DbTarget("127.0.0.1", cloud_port, "root", "root", "std7"),
        local=DbTarget("127.0.0.1", local_port, "root", "root", ""),
        policy=SyncPolicy(),
    )
    cloud, local = engine_for(cfg.cloud), engine_for(cfg.local)
    # 清单故意子表在前（字母序也如此）——计划必须自行按外键重排
    manifest = [TableRule(name="aaa_child"), TableRule(name="zzz_parent")]
    plan = build_plan(cfg, cloud, local, manifest)
    ups = plan.up_statements()

    def pos(name: str) -> int:
        return next(i for i, s in enumerate(ups) if f"CREATE TABLE `std7`.`{name}`" in s)

    assert pos("zzz_parent") < pos("aaa_child"), "\n".join(ups)

    with local.begin() as conn:
        for stmt in ups:
            conn.execute(text(stmt))

    again = build_plan(cfg, cloud, local, manifest)
    assert again.statement_count == 0, again.report_text()

