"""compare.py：tag 过滤与 push_time 比较器。"""

from datetime import datetime, timezone

from callfans.core.compare import (
    find_leading,
    is_version_tag,
    pick_latest,
    tag_timestamp,
)
from callfans.core.models import ArtifactTag


def mk(tag, pushed=None, digest="sha256:000"):
    return ArtifactTag(tag=tag, digest=digest, push_time=pushed)


def pt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


EXCLUDE = {"latest", "dev"}


class TestTagFilter:
    def test_exclude_latest_dev(self):
        assert not is_version_tag("latest", EXCLUDE)
        assert not is_version_tag("dev", EXCLUDE)

    def test_version_tag_ok(self):
        assert is_version_tag("20260912132921-123a066", EXCLUDE)

    def test_timestamp_only_tag_ok(self):
        # 真实仓库存在纯时间戳 tag（2026-09-17 放宽格式）
        assert is_version_tag("20260910161528", EXCLUDE)

    def test_bad_pattern_skipped(self):
        assert not is_version_tag("v1.2.3", EXCLUDE)
        assert not is_version_tag("release", EXCLUDE)
        assert not is_version_tag("2026091-abc", EXCLUDE)  # 时间戳不足 14 位

    def test_tag_timestamp(self):
        dt = tag_timestamp("20260912132921-123a066")
        assert dt == datetime(2026, 9, 12, 13, 29, 21, tzinfo=timezone.utc)
        assert tag_timestamp("notatag") is None


class TestFindLeading:
    REMOTE = [
        mk("20260901102030-abc1234", pt("2026-09-01T10:20:30Z"), "sha256:a"),
        mk("20260910150000-def5678", pt("2026-09-10T15:00:00Z"), "sha256:b"),
        mk("20260912132921-123a066", pt("2026-09-12T13:29:21Z"), "sha256:c"),
    ]

    def test_leading_sorted_asc(self):
        leading = find_leading(self.REMOTE, "20260901102030-abc1234", "sha256:a")
        assert [t.tag for t in leading] == [
            "20260910150000-def5678",
            "20260912132921-123a066",
        ]

    def test_up_to_date(self):
        assert find_leading(self.REMOTE, "20260912132921-123a066", "sha256:c") == []

    def test_no_current_all_leading(self):
        leading = find_leading(self.REMOTE, None, None)
        assert len(leading) == 3
        assert pick_latest(leading).tag == "20260912132921-123a066"

    def test_same_tag_repush_detected(self):
        leading = find_leading(self.REMOTE, "20260912132921-123a066", "sha256:OLD")
        assert [t.tag for t in leading] == ["20260912132921-123a066"]

    def test_current_pruned_falls_back_to_tag_time(self):
        # 当前 tag 已被 Harbor 清理：用 tag 时间戳做基准
        leading = find_leading(self.REMOTE, "20260905120000-aaaaaaa", "sha256:x")
        assert [t.tag for t in leading] == [
            "20260910150000-def5678",
            "20260912132921-123a066",
        ]

    def test_push_time_missing_falls_back_to_tag_time(self):
        remote = [mk("20260901102030-abc1234", None, "sha256:a"), mk("20260912132921-123a066", None, "sha256:c")]
        leading = find_leading(remote, None, None)
        assert pick_latest(leading).tag == "20260912132921-123a066"
