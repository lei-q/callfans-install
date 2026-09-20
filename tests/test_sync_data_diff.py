"""数据比对（PK 归并、dict 行模型）与 SQL/Down 生成矩阵。"""

from callfans.core.sync.data_diff import diff_rows, seed_inserts

CLOUD = [
    {"id": 1, "k": "a", "v": "x"},
    {"id": 2, "k": "b", "v": "y"},
    {"id": 4, "k": "d", "v": "z"},
]
LOCAL = [
    {"id": 1, "k": "a", "v": "x"},
    {"id": 2, "k": "b", "v": "OLD"},
    {"id": 3, "k": "c", "v": "w"},
]


def test_merge_insert_update_delete():
    changes = diff_rows("sys_dict", "id", CLOUD, LOCAL)
    kinds = [c.kind for c in changes]
    # id=2 值不同 → update；id=3 本地多 → delete；id=4 云多 → insert；id=1 相同 → 无
    assert kinds == ["update", "delete", "insert"]
    up = next(c for c in changes if c.kind == "update")
    assert up.up_sql == "UPDATE `sys_dict` SET `k` = 'b', `v` = 'y' WHERE `id` = 2;"
    # Down 用本地旧值回写
    assert up.down_sql == "UPDATE `sys_dict` SET `k` = 'b', `v` = 'OLD' WHERE `id` = 2;"
    ins = next(c for c in changes if c.kind == "insert")
    assert ins.up_sql == (
        "INSERT INTO `sys_dict` (`id`, `k`, `v`) VALUES (4, 'd', 'z');"
    )
    assert ins.down_sql == "DELETE FROM `sys_dict` WHERE `id` = 4;"
    dele = next(c for c in changes if c.kind == "delete")
    assert dele.up_sql == "DELETE FROM `sys_dict` WHERE `id` = 3;"
    # Down 把本地旧行插回
    assert dele.down_sql == (
        "INSERT INTO `sys_dict` (`id`, `k`, `v`) VALUES (3, 'c', 'w');"
    )


def test_identical_rows_no_changes():
    assert diff_rows("t", "id", CLOUD, list(CLOUD)) == []


def test_column_set_mismatch_compares_intersection():
    """云库多出新列（本次漂移含加列）：交集列比较，写入含新列。"""
    cloud = [{"id": 1, "k": "a", "v": "x", "note": "new"}]
    local = [{"id": 1, "k": "a", "v": "x"}]  # 无 note 列
    changes = diff_rows("t", "id", cloud, local)
    # 交集列（id/k/v）无差异 → 不产生 update（note 值下一轮收敛）
    assert changes == []
    # 一旦交集列有差异，UPDATE 的 SET 含新列（结构阶段先行，执行时列已存在）
    local2 = [{"id": 1, "k": "a", "v": "OLD"}]
    changes2 = diff_rows("t", "id", cloud, local2)
    upd = next(c for c in changes2 if c.kind == "update")
    assert "`note` = 'new'" in upd.up_sql


def test_ignore_columns():
    # v 列被忽略：id=2 的 v 差异不再触发 update
    changes = diff_rows("t", "id", CLOUD, LOCAL, ignore={"v"})
    kinds = [c.kind for c in changes]
    assert "update" not in kinds
    assert kinds == ["delete", "insert"]
    # insert 仍包含全部非忽略列
    ins = next(c for c in changes if c.kind == "insert")
    assert "`v`" not in ins.up_sql and "`k`" in ins.up_sql


def test_null_and_escaping():
    changes = diff_rows("t", "id",
                        [{"id": 1, "v": None}, {"id": 2, "v": "it's"}],
                        [{"id": 1, "v": "x"}])
    upd = next(c for c in changes if c.kind == "update")
    assert "= NULL" in upd.up_sql
    ins = next(c for c in changes if c.kind == "insert")
    assert "'it''s'" in ins.up_sql


def test_empty_streams():
    assert diff_rows("t", "id", [], []) == []


def test_seed_inserts():
    rows = [{"id": 1, "k": "a"}, {"id": 2, "k": "b"}]
    seeds = seed_inserts("sys_dict", rows)
    assert len(seeds) == 2
    assert seeds[0].up_sql == "INSERT INTO `sys_dict` (`id`, `k`) VALUES (1, 'a');"
    assert "DELETE FROM `sys_dict`" in seeds[0].down_sql
    assert "备份" in seeds[0].down_sql
