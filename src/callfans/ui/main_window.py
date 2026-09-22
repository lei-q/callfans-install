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
    QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import client
from .client import ServiceUnavailable

_POLL_SECONDS = 5

# 阶段 → 展示文案（舞台指示器与日志）
_STAGE_TEXT = {
    "stop_old": "停止旧容器",
    "pull": "拉取新镜像",
    "pull_progress": "拉取新镜像",
    "up": "启动新容器",
    "up_progress": "启动新容器",
    "verify": "健康观察",
    "unzip": "解压前端包",
    "replace": "替换前端目录",
    "tag_backfill": "补齐 tag 变量",
    "backup": "备份受影响表",
    "execute": "SQL 同步执行",
    "rollback": "SQL 同步回滚",
}
_SPINNER_FRAMES = "|/-\\"
_MAX_SQL_PREVIEW_LINES = 2000  # 详情面板 SQL 预览上限（超出提示导出）


class Worker(QObject):
    """daemon 线程执行阻塞 IPC，结果经信号（自动队列投递）回主线程。

    用 daemon 线程而非 QThread：退出程序时线程随进程静默结束，
    不会出现 "QThread: Destroyed while thread is still running"。
    finished 在 done/failed 之后投递，用于调用方回收 Worker。
    """

    done = Signal(object)
    failed = Signal(str)
    finished = Signal()

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
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    new_pending = Signal(int)  # 通知托盘弹气泡
    notify = Signal(str, str)  # (标题, 内容) 通用托盘通知（如 sqlsync 失败）

    def __init__(self, poll_enabled: bool = True):
        super().__init__()
        self.setWindowTitle("callfans 管家")
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

        # 更新进度条 + 舞台指示器（阶段文字 + 旋转动画，长阶段不"死屏"）
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet("color: #666;")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.stage_label)
        layout.addLayout(progress_row)

        self._stage_text = ""
        self._spinner_idx = 0
        self._spinner = QTimer(self)
        self._spinner.setInterval(120)
        self._spinner.timeout.connect(self._tick_spinner)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["✓", "类型", "名称", "当前版本", "新版本", "alias"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # 可拖动
        header.setSectionsMovable(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(
            lambda: self._show_changelog(self.table.currentRow())
        )
        self.table.itemChanged.connect(self._on_item_changed)
        header.sectionClicked.connect(
            lambda section: self._toggle_check_all() if section == 0 else None)
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
        self.log_view.setMinimumHeight(80)  # 不设上限：由分割条自由调高（v0.4.4 实测修复）
        self.log_view.setMaximumBlockCount(2000)  # pull 进度行多，自动裁剪旧行
        self.log_view.setPlaceholderText("进度与结果（实时）")

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("进度与结果"))
        self.btn_toggle_log = QPushButton("⤢ 展开")
        self.btn_toggle_log.setToolTip("展开/收起进度与结果区域（也可直接拖动上方分割条调高）")
        self._log_expanded = False
        self.btn_toggle_log.clicked.connect(self._toggle_log_size)
        log_header.addWidget(self.btn_toggle_log)
        log_header.addStretch(1)
        self.btn_export_sql = QPushButton("导出 SQL")
        self.btn_export_sql.setToolTip("将选中的数据库变更 SQL 导出为 .sql 文件")
        self.btn_export_sql.clicked.connect(self._export_selected_sql)
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        log_header.addWidget(self.btn_export_sql)
        log_header.addWidget(self.btn_clear_log)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log_view)

        # 垂直分割：上半（表格+详情）/下半（日志），可拖动调高（#5）
        self._vsplit = vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(splitter)
        vsplit.addWidget(log_panel)
        vsplit.setHandleWidth(8)          # 拖动手柄加宽，好抓
        vsplit.setCollapsible(0, False)
        vsplit.setCollapsible(1, False)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([300, 240])
        layout.addWidget(vsplit, 1)

        self.setCentralWidget(central)

        if poll_enabled:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(_POLL_SECONDS * 1000)
            self.refresh()
            from .events import EventListener

            self.listener = EventListener(self)
            self.listener.event_received.connect(self._on_event)
            self.listener.start()

    # ---------- 数据 ----------

    def refresh(self) -> None:
        """轮询入口（也可手动调用立即刷新）。"""
        self._run(lambda: (client.status(), client.pending()),
                  self._apply_state, self._refresh_failed)

    def _refresh_failed(self, error: str) -> None:
        """服务不可达：尝试自拉起（Windows 模式 A），并给出状态提示。

        更新进行中不拉起——重操作期间事件循环响应慢会被误判为服务挂了，
        重复拉起第二个服务反而制造连接混乱（v0.4.3 实测教训）。
        """
        from .bootstrap import ensure_service_running

        if getattr(self, "_busy", False):
            self.label_status.setText("更新进行中（服务响应缓慢，属正常）…")
            return
        self.label_status.setText("服务未运行，正在尝试启动…")
        self._spawn_state = getattr(self, "_spawn_state", {})
        ensure_service_running(self._spawn_state)

    def _apply_state(self, result) -> None:
        status, pending = result
        busy = status.get("busy") or status.get("updating")
        updating = status.get("updating")
        state_text = "更新中…" if updating else ("检查中…" if status.get("busy") else "服务正常")
        last = status.get("last_check")
        sql = status.get("sqlsync") or {}
        sql_text = ""
        if sql:
            mark = "✓" if sql.get("last_status") == "success" else "⚠"
            sql_text = f"｜SQL同步: {mark}{sql.get('last_status')}"
        self.label_status.setText(f"{state_text}｜最近检查: {last or '-'}{sql_text}")
        self.btn_check.setEnabled(not busy)
        self._update_update_button()

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
        plan_id = pending.get("checked_at")
        if plan_id != getattr(self, "_filled_plan_id", None):
            # 新一轮计划：重置勾选状态（默认全选）；旧名称的勾选记忆清除
            self._filled_plan_id = plan_id
            self._checked_state = {p.get("name", ""): True for p in items}
        self._checked_state = getattr(self, "_checked_state", {})
        self.table.blockSignals(True)  # 填充期间不触发 itemChanged
        try:
            self.table.setRowCount(len(items))
            for row, p in enumerate(items):
                new = p.get("new")
                new_text = new if isinstance(new, str) else " → ".join(new or [])
                name = p.get("name", "")
                state = self._checked_state.get(name, True)
                # 勾选列（保持用户勾选状态；新条目默认选）
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if state else Qt.CheckState.Unchecked)
                chk.setToolTip("勾选/取消此项更新；点击表头 ✓ 一键全选/全不选")
                self.table.setItem(row, 0, chk)
                for col, text in enumerate([
                    p.get("type", ""), name, p.get("old") or "未安装",
                    new_text, p.get("alias") or "",
                ], start=1):
                    item = QTableWidgetItem(str(text))
                    item.setToolTip(str(text))  # 内容被遮挡时悬停显示全文（#4）
                    self.table.setItem(row, col, item)
            self.table.resizeRowsToContents()
            if plan_id != getattr(self, "_sized_plan_id", None):
                # 列宽自适应仅在计划变化时执行，不与用户手动拖动打架（#4 修正）
                self._auto_fit_columns()
                self._sized_plan_id = plan_id
        finally:
            self.table.blockSignals(False)
        if self.table.currentRow() >= len(items):
            self.table.clearSelection()
            self.changelog_view.clear()
        self._update_update_button()

    def _auto_fit_columns(self) -> None:
        """列宽自适应：以内容宽为基础，整体等比放大铺满表格显示区（#4 修正方向）。

        - 内容不足一屏 → 按比例拉伸各列填满（勾选列固定 36px 不参与）
        - 内容超出一屏 → 保持内容宽，出横向滚动条
        - 仍可手动拖动；窗口尺寸变化时重新铺满
        """
        header = self.table.horizontalHeader()
        col_count = self.table.columnCount()
        base: list[int] = []
        for col in range(col_count):
            if col == 0:
                base.append(36)
                continue
            w = header.sizeHintForColumn(col)
            header_w = header.fontMetrics().horizontalAdvance(
                self.table.horizontalHeaderItem(col).text() or "") + 34
            base.append(min(max(w, header_w) + 14, 600))
        available = self.table.viewport().width()
        extra = available - 36 - 2  # 边框余量
        scalable = sum(base[1:])
        if scalable > 0 and extra > scalable:
            scale = extra / scalable
            widths = [36] + [int(w * scale) for w in base[1:]]
        else:
            widths = base
        for col, w in enumerate(widths):
            self.table.setColumnWidth(col, w)

    def _toggle_check_all(self) -> None:
        """表头 ✓ 列点击：一键全选/全不选（#3）。"""
        names = [p.get("name", "") for p in getattr(self, "_current_pending", [])]
        if not names:
            return
        all_checked = all(self._checked_state.get(n, True) for n in names)
        target = not all_checked
        self.table.blockSignals(True)
        try:
            for row, name in enumerate(names):
                self._checked_state[name] = target
                chk = self.table.item(row, 0)
                if chk is not None:
                    chk.setCheckState(
                        Qt.CheckState.Checked if target else Qt.CheckState.Unchecked)
        finally:
            self.table.blockSignals(False)
        self.log("✓ 已全选" if target else "✓ 已全不选")
        self._update_update_button()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 窗口尺寸变化 → 列宽重新铺满显示区（不覆盖用户拖动，仅随窗缩放）
        if getattr(self, "_sized_plan_id", None) is not None:
            self._auto_fit_columns()

    def _on_item_changed(self, item) -> None:
        """勾选状态变化 → 记忆状态并更新"立即更新"可用性（#3）。"""
        if item.column() == 0:
            name_item = self.table.item(item.row(), 2)
            if name_item is not None:
                self._checked_state[name_item.text()] = (
                    item.checkState() == Qt.CheckState.Checked)
            self._update_update_button()

    def _checked_names(self) -> list[str]:
        names = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            name_item = self.table.item(row, 2)
            if chk is not None and name_item is not None and \
                    chk.checkState() == Qt.CheckState.Checked:
                names.append(name_item.text())
        return names

    def _update_update_button(self) -> None:
        has_pending = bool(getattr(self, "_current_pending", []))
        self.btn_update.setEnabled(
            not (self._busy) and has_pending and bool(self._checked_names()))

    def _show_changelog(self, row: int) -> None:
        pending = getattr(self, "_current_pending", [])
        if not (0 <= row < len(pending)):
            return
        item = pending[row]
        cl = item.get("changelog")
        if isinstance(cl, dict):
            text = "\n\n".join(f"【{tag}】\n{msg or '-'}" for tag, msg in cl.items())
        else:
            text = str(cl or "（无 changelog）")
        sql = item.get("sql") or []
        if sql:
            preview = sql[:_MAX_SQL_PREVIEW_LINES]
            note = ""
            if len(sql) > _MAX_SQL_PREVIEW_LINES:
                note = f"\n\n（共 {len(sql)} 条，仅预览前 {_MAX_SQL_PREVIEW_LINES} 条，完整内容请点【导出 SQL】）"
            text += "\n\n──── 变更 SQL ────\n" + "\n".join(preview) + note
        self.changelog_view.setPlainText(text)

    def _toggle_log_size(self) -> None:
        """展开/收起进度与结果区域。"""
        total = sum(self._vsplit.sizes()) or 600
        if self._log_expanded:
            self._vsplit.setSizes([int(total * 0.55), int(total * 0.45)])
            self.btn_toggle_log.setText("⤢ 展开")
        else:
            self._vsplit.setSizes([int(total * 0.25), int(total * 0.75)])
            self.btn_toggle_log.setText("⤡ 收起")
        self._log_expanded = not self._log_expanded

    def _export_selected_sql(self) -> None:
        """导出变更 SQL 到 .sql 文件（#2）。每个分支都有日志，绝不静默。"""
        try:
            pending = getattr(self, "_current_pending", []) or []
            row = self.table.currentRow()
            if not (0 <= row < len(pending)):
                # 未选中：若恰有唯一 sqlsync 条目则自动选用
                sqlsync_rows = [i for i, p in enumerate(pending) if p.get("sql")]
                if len(sqlsync_rows) == 1:
                    row = sqlsync_rows[0]
                else:
                    self.log("导出 SQL：请先在列表中选中一条待更新项")
                    return
            sql = pending[row].get("sql") or []
            if not sql:
                self.log("导出 SQL：选中项没有数据库变更 SQL（仅 sqlsync 条目可导出）")
                return
            default = f"callfans-sqlsync-{pending[row].get('name', 'x').strip('()')}.sql"
            path, _ = QFileDialog.getSaveFileName(
                self, "导出变更 SQL", default, "SQL 文件 (*.sql);;所有文件 (*)")
            if not path:
                return
            from pathlib import Path as _P

            _P(path).write_text("\n".join(sql) + "\n", encoding="utf-8")
            self.log(f"已导出 {len(sql)} 条 SQL → {path}")
        except Exception as e:  # 对话框异常也不静默
            self.log(f"导出 SQL 失败: {type(e).__name__}: {e}")

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
        selected = self._checked_names()
        if not selected:
            self.log("未勾选任何条目")
            return
        self._busy = True
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        # 忙碌态进度条（update_begin 事件到达后切换为确切进度）
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.log(f"开始更新（勾选 {len(selected)} 项）…")
        self._awaiting_update_result = True
        self._run(lambda: client.update(selected), self._update_done,
                  self._update_request_failed)

    def _update_done(self, report: dict) -> None:
        self._busy = False
        if report.get("preflight_error"):
            self.progress.setVisible(False)
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
        self.progress.setVisible(False)
        self.log(f"操作失败: {error}")
        self.refresh()

    def _update_request_failed(self, error: str) -> None:
        """更新请求连接中断：服务端仍在执行（结果保证由 update_done 事件送达），
        不按失败处理，等事件收尾。"""
        self.log("⚠ 更新请求连接中断——服务仍在后台执行，结果稍后自动送达…")

    # ---------- 服务 WS 事件（实时进度） ----------

    # ---------- 舞台指示器 ----------

    def _set_stage(self, text: str | None) -> None:
        """当前阶段文字；None 表示空闲（停动画）。"""
        self._stage_text = text or ""
        if self._stage_text:
            if not self._spinner.isActive():
                self._spinner.start()
        else:
            self._spinner.stop()
        self._render_stage()

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
        self._render_stage()

    def _render_stage(self) -> None:
        if self._stage_text:
            frame = _SPINNER_FRAMES[self._spinner_idx]
            self.stage_label.setText(f"{frame} {self._stage_text}")
        else:
            self.stage_label.setText("")

    # ---------- 服务 WS 事件（实时进度） ----------

    def _on_event(self, payload: dict) -> None:
        event = payload.get("event")
        data = payload.get("data") or {}
        if event == "check_done":
            self.refresh()
        elif event == "update_begin":
            total = max(int(data.get("total") or 0), 1)
            self.progress.setRange(0, total)
            self.progress.setValue(0)
            self.progress.setVisible(True)
            self._set_stage(None)
            self.log(f"── 开始更新: 共 {total} 项 ──")
        elif event == "update_progress":
            item = data.get("item", "")
            stage = data.get("stage", "")
            if stage == "start":
                self.log(f"▶ [{data.get('type', '')}] {item}")
            elif stage == "done":
                self._set_stage(None)
                rec = data.get("record") or {}
                self.log(f"■ {item} → {rec.get('result')}"
                         + (f"（{rec.get('error')}）" if rec.get("error") else ""))
                self.progress.setValue(self.progress.value() + 1)
            elif stage in ("pull_progress", "up_progress"):
                line = str(data.get("line", "")).strip()
                if line:
                    mark = "↓" if stage == "pull_progress" else " "
                    self.log(f"   {mark} {line}")
                    self._set_stage(_STAGE_TEXT.get(stage))
            elif stage == "verify" and data.get("remaining") is not None:
                self._set_stage(f"{_STAGE_TEXT['verify']}（剩余 {data['remaining']}s）")
            else:  # stop_old / pull / up / sql_pull / sql_exec / unzip / replace / tag_backfill …
                text = _STAGE_TEXT.get(stage, stage)
                detail = ""
                for k in ("file", "tag", "service"):
                    if data.get(k):
                        detail = f" {data[k]}"
                        break
                if data.get("vars"):
                    detail += " ← " + ",".join(data["vars"])
                self._set_stage(text + detail)
                if stage != "verify":  # verify 走倒计时分支，不刷日志
                    extras = " ".join(str(data[k]) for k in ("tag", "file", "ref") if data.get(k))
                    if data.get("files"):
                        extras += f"（{len(data['files'])} 个文件）"
                    if extras:
                        self.log(f"   · {item}: {stage} {extras}".rstrip())
        elif event == "update_done":
            self._set_stage(None)
            self.progress.setValue(self.progress.maximum())
            if getattr(self, "_awaiting_update_result", False):
                self._awaiting_update_result = False
                self._busy = False
                summary = data.get("summary") or {}
                self.log(f"更新汇总: 成功 {summary.get('success', 0)}｜"
                         f"失败 {summary.get('failed', 0)}｜"
                         f"回滚 {summary.get('rolled_back', 0)}")
            self.refresh()
        elif event == "sqlsync_progress":
            stage = data.get("stage", "")
            if stage == "execute" and data.get("total"):
                self._set_stage(f"SQL 同步 {data.get('index', 0)}/{data['total']}")
            else:
                self._set_stage(_STAGE_TEXT.get(stage, stage) or stage)
        elif event == "sqlsync_done":
            self._set_stage(None)
            status = data.get("status", "")
            if status == "success":
                total = data.get("total") or 0
                if total == 0:
                    self.log(f"◆ SQL 同步无差异（run {data.get('run_id')}）")
                else:
                    self.log(f"◆ SQL 同步完成：语句 {data.get('executed')}/{total}"
                             f"（run {data.get('run_id')}）")
            else:
                msg = data.get("error") or data.get("guard") or status
                self.log(f"◆ SQL 同步未完成 [{status}]: {msg}")
                self.notify.emit("SQL 同步未完成", str(msg)[:160])

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
        listener = getattr(self, "listener", None)
        if listener is not None:
            listener.stop()
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
