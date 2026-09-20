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


def _pump(qapp, cond, timeout_s=5.0):
    """泵事件循环直到条件满足（等待 daemon 线程 + 队列投递）。"""
    import time

    deadline = time.monotonic() + timeout_s
    while not cond():
        assert time.monotonic() < deadline, "等待事件超时"
        qapp.processEvents()
        time.sleep(0.01)


def test_worker_lifecycle_success(qapp):
    """回归：v0.1.2 Worker 改为 QObject 后 finished 缺失导致启动即崩。"""
    import time

    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)
    results: list = []
    w._run(lambda: ({"busy": False, "updating": False, "last_check": None},
                    {"checked_at": None, "pending": []}), results.append)
    _pump(qapp, lambda: len(results) == 1)
    assert results[0][0]["busy"] is False
    # finished 信号回收 Worker
    _pump(qapp, lambda: w._workers == [])
    assert w._workers == []


def test_worker_lifecycle_failure(qapp):
    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)

    def boom():
        raise RuntimeError("网络错误")

    got: list = []
    w._run(boom, lambda r: got.append(("done", r)), lambda e: got.append(("err", e)))
    _pump(qapp, lambda: len(got) == 1)
    assert got[0][0] == "err" and "RuntimeError" in got[0][1]
    _pump(qapp, lambda: w._workers == [])


def test_update_events_progress_bar_and_log(qapp):
    """WS 事件驱动：进度条按项推进、阶段日志实时滚动、可清空。"""
    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)
    w.refresh = lambda: None  # 隔离真实 IPC / 服务自拉起

    assert w.progress.isHidden()
    w._on_event({"event": "update_begin", "data": {"total": 2, "items": []}})
    assert not w.progress.isHidden()
    assert w.progress.maximum() == 2 and w.progress.value() == 0

    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/db", "type": "sql", "stage": "start"}})
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/db", "stage": "sql_exec", "tag": "t1"}})
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/db", "stage": "done",
                          "record": {"result": "success"}}})
    assert w.progress.value() == 1

    w._on_event({"event": "update_done", "data": {"summary": {"success": 1}}})
    assert w.progress.value() == w.progress.maximum()

    text = w.log_view.toPlainText()
    assert "▶ [sql] callfans/db" in text
    assert "callfans/db: sql_exec t1" in text
    assert "■ callfans/db → success" in text
    assert "共 2 项" in text

    # 清空日志
    w.btn_clear_log.click()
    assert w.log_view.toPlainText() == ""


def test_stage_indicator_animation(qapp):
    """长阶段体验：pull 进度滚动进日志、舞台指示器带动画与倒计时、完成后归位。"""
    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)
    w.refresh = lambda: None

    w._on_event({"event": "update_begin", "data": {"total": 1, "items": []}})
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/callfans-admin", "type": "server", "stage": "start"}})
    # pull 进度行 → 日志滚动 + 指示器激活
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/callfans-admin", "stage": "pull_progress",
                          "line": "abc123: Downloading [===>  ] 12.3MB/65.5MB"}})
    assert "12.3MB/65.5MB" in w.log_view.toPlainText()
    assert w._spinner.isActive()
    assert "拉取新镜像" in w.stage_label.text()
    # 健康观察倒计时：只动指示器不刷日志
    before = w.log_view.toPlainText()
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/callfans-admin", "stage": "verify", "remaining": 43}})
    assert "剩余 43s" in w.stage_label.text()
    assert w.log_view.toPlainText() == before
    # 单项完成 → 指示器归位、动画停止
    w._on_event({"event": "update_progress",
                 "data": {"item": "callfans/callfans-admin", "stage": "done",
                          "record": {"result": "success"}}})
    assert not w._spinner.isActive()
    assert w.stage_label.text() == ""


def test_sqlsync_events_stage_and_notify(qapp):
    """sqlsync 事件：执行进度驱动指示器，失败触发托盘通知信号。"""
    from callfans.ui.main_window import MainWindow

    w = MainWindow(poll_enabled=False)
    notified: list = []
    w.notify.connect(lambda t, m: notified.append((t, m)))

    w._on_event({"event": "sqlsync_progress", "data": {"stage": "backup", "tables": ["t1"]}})
    assert "备份受影响表" in w.stage_label.text()
    w._on_event({"event": "sqlsync_progress",
                 "data": {"stage": "execute", "index": 3, "total": 12}})
    assert "SQL 同步 3/12" in w.stage_label.text()
    # 成功：日志记录、不通知；带语句数与无差异区分
    w._on_event({"event": "sqlsync_done",
                 "data": {"status": "success", "run_id": "R1", "executed": 3, "total": 3}})
    assert "SQL 同步完成：语句 3/3" in w.log_view.toPlainText()
    w._on_event({"event": "sqlsync_done",
                 "data": {"status": "success", "run_id": "R2", "total": 0}})
    assert "SQL 同步无差异" in w.log_view.toPlainText()
    assert notified == []
    assert w.stage_label.text() == ""
    # 失败：日志 + 托盘通知
    w._on_event({"event": "sqlsync_done",
                 "data": {"status": "aborted_by_guard", "run_id": "R2",
                          "error": "[G2] 语句超限"}})
    assert notified and "语句超限" in notified[0][1]
    assert "aborted_by_guard" in w.log_view.toPlainText()


def test_quit_button_exits_when_idle(qapp, monkeypatch):
    import callfans.ui.main_window as mw

    w = mw.MainWindow(poll_enabled=False)
    assert not w._busy
    quit_calls: list[int] = []

    class FakeQApp:
        @staticmethod
        def quit():
            quit_calls.append(1)

    monkeypatch.setattr(mw, "QApplication", FakeQApp)
    w.btn_quit.click()
    assert quit_calls == [1]


def test_quit_confirms_when_busy(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    import callfans.ui.main_window as mw

    w = mw.MainWindow(poll_enabled=False)
    w._busy = True
    quit_calls: list[int] = []

    class FakeQApp:
        @staticmethod
        def quit():
            quit_calls.append(1)

    monkeypatch.setattr(mw, "QApplication", FakeQApp)
    # 用户取消 → 不退出
    monkeypatch.setattr(
        mw.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    w.request_quit()
    assert quit_calls == []
    # 用户确认 → 退出
    monkeypatch.setattr(
        mw.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    w.request_quit()
    assert quit_calls == [1]
