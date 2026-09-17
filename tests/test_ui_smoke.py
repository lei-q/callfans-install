"""UI 冒烟（offscreen，无显示环境）：表格填充、changelog 联动、新版本信号、按钮状态。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from callfans.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pending(*items):
    return {"checked_at": "T1", "pending": list(items)}


def test_window_fills_table_and_changelog(qapp):
    w = MainWindow(poll_enabled=False)
    w._apply_state(
        ({"busy": False, "updating": False, "last_check": "T1"},
         _pending(
             {"name": "callfans/api", "type": "server", "old": "a", "new": "b",
              "changelog": "修复登录", "alias": None},
             {"name": "callfans/db", "type": "sql", "old": "a", "new": ["s1", "s2"],
              "changelog": {"s1": "加表", "s2": "加列"}, "alias": None},
         )),
    )
    assert w.table.rowCount() == 2
    assert w.table.item(0, 3).text() == "b"
    assert w.table.item(1, 3).text() == "s1 → s2"  # sql 多版本列表展示
    assert w.btn_update.isEnabled()
    w.table.selectRow(1)
    assert "加表" in w.changelog_view.toPlainText()


def test_new_pending_signal_on_fresh_check(qapp):
    w = MainWindow(poll_enabled=False)
    w._first_refresh = False
    w._last_seen_check = "T0"
    got: list[int] = []
    w.new_pending.connect(got.append)
    w._apply_state(
        ({"busy": False, "updating": False, "last_check": "T1"},
         _pending({"name": "x", "type": "server", "old": None, "new": "t"})),
    )
    assert got == [1]  # 新一轮检查且有待更新 → 通知托盘


def test_no_pending_update_disabled(qapp):
    w = MainWindow(poll_enabled=False)
    w._apply_state(
        ({"busy": True, "updating": False, "last_check": None},
         {"checked_at": None, "pending": []}),
    )
    assert w.table.rowCount() == 0
    assert not w.btn_update.isEnabled()
    assert not w.btn_check.isEnabled()  # 检查中禁用


def test_tray_icon_pixmap(qapp):
    from callfans.ui.tray import _make_icon

    icon = _make_icon()
    assert not icon.pixmap(64, 64).isNull()
