"""StateStore：读写、原子覆盖、last_plan 持久化。"""

from callfans.core.local import StateStore


def test_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    st = StateStore(p)
    st.set("frontend", {"callfans/web-admin": {"tag": "20260912132921-123a066", "digest": "sha256:c"}})
    st.set_last_plan({"checked_at": "2026-09-17T10:00:00+00:00", "pending": []}, "2026-09-17T10:00:00+00:00")
    st.save()

    st2 = StateStore(p)
    assert st2.section_repo("frontend", "callfans/web-admin")["tag"] == "20260912132921-123a066"
    assert st2.last_plan["checked_at"] == "2026-09-17T10:00:00+00:00"
    assert st2.checked_at == "2026-09-17T10:00:00+00:00"


def test_corrupt_file_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{ not json", encoding="utf-8")
    st = StateStore(p)
    assert st.get("frontend", {}) == {}
    st.set("frontend", {})
    st.save()
    assert p.read_text(encoding="utf-8").startswith("{")


def test_missing_file_empty(tmp_path):
    st = StateStore(tmp_path / "none" / "state.json")
    assert st.last_plan is None
