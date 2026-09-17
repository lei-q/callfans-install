"""主窗口：检查更新 / 立即更新 两按钮 + 待更新列表 + changelog 面板 + 进度日志。

数据获取在 QThread 中执行 IPC（检查/更新可能耗时数分钟），结果经信号回主线程。
轮询（默认 5s）拿 status+pending，定时检查命中新版本时通过 new_pending 信号
通知托盘弹气泡。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import client
from .client import ServiceUnavailable

_POLL_SECONDS = 5


class Worker(QObject):
    """daemon 线程执行阻塞 IPC，结果经信号（自动队列投递）回主线程。

    用 daemon 线程而非 QThread：退出程序时线程随进程静默结束，
    不会出现 "QThread: Destroyed while thread is still running"。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    new_pending = Signal(int)  # 通知托盘弹气泡

    def __init__(self, poll_enabled: bool = True):
        super().__init__()
        self.setWindowTitle("callfans 更新器")
        self.resize(760, 520)
        self._workers: list[Worker] = []
        self._busy = False  # 检查/更新进行中（退出确认用）
        self._last_seen_check: str | None = None  # 用于识别"新一轮检查结果"
        self._first_refresh = True

        central = QWidget()
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.btn_check = QPushButton("检查更新")
        self.btn_update = QPushButton("立即更新")
        self.btn_quit = QPushButton("退出")
        self.btn_quit.setToolTip("退出程序（关闭窗口只会最小化到托盘）")
        self.btn_check.clicked.connect(self.on_check_clicked)
        self.btn_update.clicked.connect(self.on_update_clicked)
        self.btn_quit.clicked.connect(self.request_quit)
        QShortcut(QKeySequence.Quit, self, self.request_quit)  # Ctrl+Q / Ctrl+Cmd+Q
        self.label_status = QLabel("初始化…")
        top.addWidget(self.btn_check)
        top.addWidget(self.btn_update)
        top.addStretch(1)
        top.addWidget(self.label_status)
        top.addWidget(self.btn_quit)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "当前版本", "新版本", "alias"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(
            lambda: self._show_changelog(self.table.currentRow())
        )
        splitter.addWidget(self.table)

        self.changelog_view = QPlainTextEdit()
        self.changelog_view.setReadOnly(True)
        self.changelog_view.setPlaceholderText("选中一条待更新项查看 changelog")
        splitter.addWidget(self.changelog_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        self.log_view.setPlaceholderText("进度与结果")
        layout.addWidget(self.log_view)

        self.setCentralWidget(central)

        if poll_enabled:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(_POLL_SECONDS * 1000)
            self.refresh()

    # ---------- 数据 ----------

    def refresh(self) -> None:
        """轮询入口（也可手动调用立即刷新）。"""
        self._run(lambda: (client.status(), client.pending()),
                  self._apply_state, self._refresh_failed)

    def _refresh_failed(self, error: str) -> None:
        """服务不可达：尝试自拉起（Windows 模式 A），并给出状态提示。"""
        from .bootstrap import ensure_service_running

        self.label_status.setText("服务未运行，正在尝试启动…")
        self._spawn_state = getattr(self, "_spawn_state", {})
        ensure_service_running(self._spawn_state)

    def _apply_state(self, result) -> None:
        status, pending = result
        busy = status.get("busy") or status.get("updating")
        updating = status.get("updating")
        state_text = "更新中…" if updating else ("检查中…" if status.get("busy") else "服务正常")
        last = status.get("last_check")
        self.label_status.setText(f"{state_text}｜最近检查: {last or '-'}")
        self.btn_check.setEnabled(not busy)
        self.btn_update.setEnabled(not busy and bool(pending.get("pending")))

        self._fill_table(pending)

        checked_at = pending.get("checked_at")
        count = len(pending.get("pending") or [])
        if checked_at and checked_at != self._last_seen_check:
            if not self._first_refresh and count > 0:
                self.new_pending.emit(count)  # 新一轮检查且有待更新 → 气泡
            self._last_seen_check = checked_at
        self._first_refresh = False

    def _fill_table(self, pending: dict) -> None:
        items = pending.get("pending") or []
        self._current_pending = items
        self.table.setRowCount(len(items))
        for row, p in enumerate(items):
            new = p.get("new")
            new_text = new if isinstance(new, str) else " → ".join(new or [])
            for col, text in enumerate([
                p.get("type", ""), p.get("name", ""), p.get("old") or "未安装",
                new_text, p.get("alias") or "",
            ]):
                self.table.setItem(row, col, QTableWidgetItem(str(text)))
        if self.table.currentRow() >= len(items):
            self.table.clearSelection()
            self.changelog_view.clear()

    def _show_changelog(self, row: int) -> None:
        pending = getattr(self, "_current_pending", [])
        if 0 <= row < len(pending):
            cl = pending[row].get("changelog")
            if isinstance(cl, dict):
                text = "\n\n".join(f"【{tag}】\n{msg or '-'}" for tag, msg in cl.items())
            else:
                text = str(cl or "（无 changelog）")
            self.changelog_view.setPlainText(text)

    # ---------- 动作 ----------

    def on_check_clicked(self) -> None:
        self._busy = True
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.log("开始检查…")
        self._run(client.check, self._check_done, self._action_failed)

    def _check_done(self, plan: dict) -> None:
        self._busy = False
        count = len(plan.get("pending") or [])
        self.log(f"检查完成: {count} 项待更新")
        self.refresh()

    def on_update_clicked(self) -> None:
        self._busy = True
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.log("开始更新（SQL → server → 前端）…")
        self._run(client.update, self._update_done, self._action_failed)

    def _update_done(self, report: dict) -> None:
        self._busy = False
        if report.get("preflight_error"):
            self.log(f"前置检查未通过，未执行更新: {report['preflight_error']}")
            QMessageBox.warning(self, "更新", f"前置检查未通过，未执行任何更新:\n{report['preflight_error']}")
        else:
            for r in report.get("items") or []:
                new = r.get("new")
                new_text = new if isinstance(new, str) else " → ".join(new or [])
                self.log(f"[{r.get('result')}] {r.get('type')}/{r.get('name')}: {new_text}"
                         + (f"（{r['error']}）" if r.get("error") else ""))
            summary = report.get("summary") or {}
            self.log(f"更新汇总: 成功 {summary.get('success', 0)}｜失败 {summary.get('failed', 0)}"
                     f"｜回滚 {summary.get('rolled_back', 0)}")
        self.refresh()

    def _action_failed(self, error: str) -> None:
        self._busy = False
        self.log(f"操作失败: {error}")
        self.refresh()

    # ---------- 退出 ----------

    def request_quit(self) -> None:
        """统一退出入口（窗口按钮 / Ctrl+Q / 托盘菜单）。"""
        if self._busy:
            ret = QMessageBox.question(
                self, "退出", "检查/更新正在进行，退出将中断当前操作。\n确定退出吗？"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()
        QApplication.quit()

    # ---------- 工具 ----------

    def _run(self, fn, on_done, on_fail=None) -> None:
        worker = Worker(fn)
        worker.done.connect(on_done)
        if on_fail is not None:
            worker.failed.connect(on_fail)
        else:
            worker.failed.connect(lambda e: self.label_status.setText(f"服务不可达: {e}"))
        self._workers.append(worker)  # 防 GC
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        worker.start()

    def log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    # 关闭窗口 → 隐藏到托盘
    def closeEvent(self, event):
        event.ignore()
        self.hide()
