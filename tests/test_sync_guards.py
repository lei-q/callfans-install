"""护栏 G2–G5：每条场景一测（§1.2 验收）。"""

from callfans.core.sync.config import SyncPolicy, TableRule
from callfans.core.sync.guards import (
    check_cloud_integrity, check_delete_rows, check_empty_cloud_table,
    check_plan_scale, summarize,
)


def _manifest(names):
    return [TableRule(name=n, data=True, pk="id") for n in names]


POLICY = SyncPolicy()  # 200 / 0.30 / 1000（已确认默认）


class TestG2PlanScale:
    def test_statement_count_over_limit(self):
        # 10 张清单、2 张受影响（20% 不超比例），仅语句数超限
        v = check_plan_scale(201, {"a", "b"}, _manifest([f"t{i}" for i in range(10)]), POLICY)
        assert len(v) == 1 and v[0].guard == "G2" and v[0].fatal
        assert "201" in v[0].message

    def test_table_ratio_over_limit(self):
        # 10 张清单，4 张受影响 = 40% > 30%
        v = check_plan_scale(10, {"t1", "t2", "t3", "t4"},
                             _manifest([f"t{i}" for i in range(10)]), POLICY)
        assert any("40%" in x.message or "比例上限" in x.message for x in v)

    def test_within_limits(self):
        assert check_plan_scale(50, {"a"}, _manifest(["a", "b", "c", "d"]), POLICY) == []

    def test_first_sync_downgrades_to_warning(self):
        """首次全量：超限语句/比例存在但为 non-fatal（2026-10-27 确认）。"""
        v = check_plan_scale(528, {f"t{i}" for i in range(20)},
                             _manifest([f"t{i}" for i in range(20)]), POLICY,
                             first_sync=True)
        assert v and all(not x.fatal for x in v)
        assert all("首次全量" in x.message for x in v)

    def test_first_sync_still_clean_when_within_limits(self):
        assert check_plan_scale(3, {"a"}, _manifest(["a", "b"]), POLICY,
                                first_sync=True) == []


class TestG3CloudIntegrity:
    def test_missing_tables_fatal(self):
        # 缺 2/3（67% ≥ 50%）→ 大比例缺失，fatal
        v = check_cloud_integrity({"a"}, _manifest(["a", "b", "c"]))
        assert len(v) == 1 and v[0].guard == "G3" and v[0].fatal
        assert "b" in v[0].tables and "c" in v[0].tables

    def test_all_present(self):
        assert check_cloud_integrity({"a", "b"}, _manifest(["a", "b"])) == []

    def test_whole_db_missing_fatal(self):
        v = check_cloud_integrity(set(), _manifest([f"t{i}" for i in range(5)]),
                                  label="biz")
        assert len(v) == 1 and v[0].fatal
        assert "云库 biz" in v[0].message and "5/5" in v[0].message

    def test_single_missing_table_skipped(self):
        # 10 张里缺 1 张（10% < 50%）→ 清单行过期，跳过告警（2026-10-27 确认）
        names = [f"t{i}" for i in range(10)]
        v = check_cloud_integrity(set(names[1:]), _manifest(names))
        assert len(v) == 1 and v[0].fatal is False
        assert v[0].tables == ["t0"] and "跳过" in v[0].message

    def test_stale_backup_rows_ignored(self):
        """`_cf_bak_*` 清单行云端不存在不触发 G3。"""
        manifest = _manifest(["a", "_cf_bak_20260920_mobile_arm_server"])
        assert check_cloud_integrity({"a"}, manifest) == []
        # 全部是失效备份行 → 同样不拦
        assert check_cloud_integrity(set(), _manifest(["_cf_bak_x"])) == []


class TestG4DeleteRows:
    def test_over_limit(self):
        v = check_delete_rows("sys_config", 1001, POLICY)
        assert len(v) == 1 and v[0].guard == "G4" and v[0].tables == ["sys_config"]

    def test_at_limit_ok(self):
        assert check_delete_rows("sys_config", 1000, POLICY) == []


class TestG5EmptyCloud:
    def test_empty_cloud_non_fatal(self):
        v = check_empty_cloud_table("sys_dict", cloud_rows=0, local_rows=42)
        assert len(v) == 1 and v[0].guard == "G5" and v[0].fatal is False

    def test_both_empty_or_cloud_has_rows(self):
        assert check_empty_cloud_table("t", 0, 0) == []
        assert check_empty_cloud_table("t", 5, 42) == []


def test_summarize_format():
    out = summarize(
        check_delete_rows("t", 2000, POLICY) + check_empty_cloud_table("u", 0, 3)
    )
    assert "[G4]" in out and "[G5]（跳过）" in out
