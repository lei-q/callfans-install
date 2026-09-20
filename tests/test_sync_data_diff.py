"""数据比对（PK 归并）与 SQL/Down 生成矩阵。"""

from callfans.core.sync.data_diff import diff_rows, seed_inserts

COLS = ["id", "k", "v"]
CLOUD = [(1, "a", "x"), (2, "b", "y"), (4, "d", "z")]
LOCAL = [(1, "a", "x"), (2, "b", "OLD"), (3, "c", "w")]


def test_merge_insert_update_delete():
    changes = diff_rows("sys_dict", COLS, "id", CLOUD, LOCAL)
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
    assert diff_rows("t", COLS, "id", CLOUD, list(CLOUD)) == []


def test_ignore_columns():
    # v 列被忽略：id=2 的 v 差异不再触发 update
    changes = diff_rows("t", COLS, "id", CLOUD, LOCAL, ignore={"v"})
    kinds = [c.kind for c in changes]
    assert "update" not in kinds
    assert kinds == ["delete", "insert"]
    # insert 仍包含全部列
    ins = next(c for c in changes if c.kind == "insert")
    assert "`v`" in ins.up_sql


def test_null_and_escaping():
    changes = diff_rows("t", ["id", "v"], "id",
                        [(1, None), (2, "it's")], [(1, "x")])
    upd = next(c for c in changes if c.kind == "update")
    assert "= NULL" in upd.up_sql
    ins = next(c for c in changes if c.kind == "insert")
    assert "'it''s'" in ins.up_sql


def test_empty_streams():
    assert diff_rows("t", COLS, "id", [], []) == []


def test_seed_inserts():
    rows = [(1, "a"), (2, "b")]
    seeds = seed_inserts("sys_dict", ["id", "k"], rows)
    assert len(seeds) == 2
    assert seeds[0].up_sql == "INSERT INTO `sys_dict` (`id`, `k`) VALUES (1, 'a');"
    assert "DELETE FROM `sys_dict`" in seeds[0].down_sql
    assert "备份" in seeds[0].down_sql
