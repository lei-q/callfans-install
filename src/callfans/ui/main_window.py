"""主窗口：左侧导航 + 两页（首页 / 更新管理）。

首页：品牌主视觉 + 「打开平台」（浏览器打开 https://本机IP:443/social_web）+
待安装/待更新/最近检查/SQL 同步 数据卡片 + 快捷操作。
更新管理：检查更新 / 立即更新 + 待更新列表 + changelog 面板 + 进度日志。

数据获取在 daemon 线程中执行 IPC（检查/更新可能耗时数分钟），结果经信号回主线程。
轮询（默认 5s）拿 status+pending，定时检查命中新版本时通过 new_pending 信号
通知托盘弹气泡。
"""

from __future__ import annotations

import threading
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import client, theme
from .client import ServiceUnavailable

_POLL_SECONDS = 5
_PLATFORM_PORT = 443
_PLATFORM_PATH = "/social_web"


def platform_url(ip: str) -> str:
    """平台入口地址（本机 IP + 443 + /social_web）。"""
    return f"https://{ip}:{_PLATFORM_PORT}{_PLATFORM_PATH}"


def _fmt_time(iso: str | None) -> str:
    """ISO 时间（UTC）→ 本地「MM-DD HH:MM」；无值返回「—」。"""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(iso)

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
    log_line = Signal(str)     # 工作线程安全写日志（下载进度等）
    busy_changed = Signal(bool)  # 检查/更新忙碌态变化（托盘菜单项跟随禁用）

    def __init__(self, poll_enabled: bool = True):
        super().__init__()
        from .. import __version__
        # 不设窗口标题（2026-10-16）：侧栏已有品牌与版本号，标题栏留空
        from .tray import _make_icon
        self.setWindowIcon(_make_icon())  # 任务栏/窗口图标（logo.svg）
        self.setStyleSheet(theme.STYLESHEET)
        self.resize(1080, 700)
        self.setMinimumSize(900, 580)
        self._workers: list[Worker] = []
        self._busy = False  # 检查/更新进行中（退出确认用）
        self._last_seen_check: str | None = None  # 用于识别"新一轮检查结果"
        self._first_refresh = True
        self._prompted_versions: set[str] = set()
        self._host_ip: str | None = None  # 部署 .env 的 HOST_IP（status 轮询带回）
        self.log_line.connect(self.log)

        # ---- 外壳：左侧导航 + 右侧分页 ----
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._build_sidebar(__version__))
        content = QFrame()
        content.setObjectName("contentArea")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 20)
        self.pages = QStackedWidget()
        content_layout.addWidget(self.pages)
        shell_layout.addWidget(content, 1)

        # ---- 更新管理页 ----
        self.page_updates = QWidget()
        layout = QVBoxLayout(self.page_updates)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("更新管理")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.label_status = QLabel("初始化…")
        self.label_status.setObjectName("muted")
        head.addWidget(self.label_status)
        layout.addLayout(head)

        top = QHBoxLayout()
        self.btn_check = QPushButton(theme.icon("refresh"), "检查更新")
        self.btn_update = QPushButton(theme.icon("download", "#FFFFFF"), "立即更新")
        self.btn_update.setObjectName("primary")
        self.btn_hostip = QPushButton(theme.icon("network"), "刷新 IP")
        self.btn_hostip.setToolTip("探测当前宿主机 IP，写入部署 .env 并重建 xray-rest 容器")
        self.btn_hostip.clicked.connect(self.on_refresh_host_ip)
        # 检查新版 / 退出 移至托盘右键菜单（tray.py），主窗口不再放按钮
        self.btn_check.clicked.connect(self.on_check_clicked)
        self.btn_update.clicked.connect(self.on_update_clicked)
        for b in (self.btn_check, self.btn_update, self.btn_hostip):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        QShortcut(QKeySequence.Quit, self, self.request_quit)  # Ctrl+Q / Ctrl+Cmd+Q
        top.addWidget(self.btn_check)
        top.addWidget(self.btn_update)
        top.addStretch(1)

        # 舞台指示器（阶段文字 + 旋转动画，长阶段不"死屏"）放工具栏右侧；
        # 进度条单独一行，仅更新时显示（隐藏时不占位）
        self.stage_label = QLabel("")
        self.stage_label.setObjectName("muted")
        top.addWidget(self.stage_label)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self._stage_text = ""
        self._spinner_idx = 0
        self._spinner = QTimer(self)
        self._spinner.setInterval(120)
        self._spinner.timeout.connect(self._tick_spinner)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["✓", "类型", "名称", "当前版本", "新版本", "alias"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)  # 长名称/tag 单行省略，悬停 tooltip 看全文
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

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(80)  # 不设上限：由分割条自由调高（v0.4.4 实测修复）
        self.log_view.setMaximumBlockCount(2000)  # pull 进度行多，自动裁剪旧行
        self.log_view.setPlaceholderText("进度与结果（实时）")

        log_header = QHBoxLayout()
        log_title = QLabel("进度与结果")
        log_title.setObjectName("sectionTitle")
        log_header.addWidget(log_title)
        self.btn_toggle_log = QPushButton("⤢ 展开")
        self.btn_toggle_log.setObjectName("ghost")
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
        for b in (self.btn_toggle_log, self.btn_export_sql, self.btn_clear_log):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        log_header.addWidget(self.btn_export_sql)
        log_header.addWidget(self.btn_clear_log)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 8, 0, 0)
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

        # ---- 首页 / 功能组件页 ----
        self.page_home = self._build_home_page()
        self.page_components = self._build_components_page()
        self.pages.addWidget(self.page_home)
        self.pages.addWidget(self.page_updates)
        self.pages.addWidget(self.page_components)
        self.setCentralWidget(shell)
        self.show_page("home")

        if poll_enabled:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(_POLL_SECONDS * 1000)
            self.refresh()
            from .events import EventListener

            self.listener = EventListener(self)
            self.listener.event_received.connect(self._on_event)
            self.listener.start()

    # ---------- 布局构建 ----------

    def _build_sidebar(self, version: str) -> QFrame:
        from .tray import LOGO_SVG

        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(200)
        v = QVBoxLayout(bar)
        v.setContentsMargins(14, 20, 14, 16)
        v.setSpacing(6)

        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.render_svg(LOGO_SVG, 30))
        brand.addWidget(logo)
        name = QLabel("助手管家")
        name.setObjectName("brandTitle")
        brand.addWidget(name)
        brand.addStretch(1)
        v.addLayout(brand)
        v.addSpacing(22)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        nav_items = (
            ("home", "首页", "home"),
            ("updates", "更新管理", "layers"),
            ("components", "功能组件", "package"),
        )
        for key, text, icon_name in nav_items:
            b = QPushButton(theme.icon(icon_name, "#94A3B8", checked_color="#FFFFFF"), f"  {text}")
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self._nav_group.addButton(b)
            self.nav_buttons[key] = b
            v.addWidget(b)
        v.addStretch(1)

        ver = QLabel(f"v{version}")
        ver.setObjectName("brandVersion")
        v.addWidget(ver)
        return bar

    def _build_home_page(self) -> QWidget:
        from .tray import LOGO_SVG

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)

        # 主视觉：品牌 + 服务状态 + 「打开平台」
        hero = QFrame()
        hero.setObjectName("hero")
        h = QHBoxLayout(hero)
        h.setContentsMargins(32, 28, 32, 28)
        h.setSpacing(20)
        logo = QLabel()
        logo.setPixmap(theme.render_svg(LOGO_SVG.replace("#CE4141", "#FFFFFF"), 72))
        h.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        left = QVBoxLayout()
        left.setSpacing(6)
        title = QLabel("助手管家")
        title.setObjectName("heroTitle")
        sub = QLabel("Callfans 平台的部署、检查与一键更新")
        sub.setObjectName("heroSubtitle")
        self.status_pill = QLabel("连接服务中…")
        self.status_pill.setObjectName("pill")
        pill_row = QHBoxLayout()
        pill_row.addWidget(self.status_pill)
        pill_row.addStretch(1)
        left.addWidget(title)
        left.addWidget(sub)
        left.addSpacing(8)
        left.addLayout(pill_row)
        h.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addStretch(1)
        self.btn_open_platform = QPushButton(
            theme.icon("external", theme.PRIMARY, 20), "  打开平台")
        self.btn_open_platform.setObjectName("heroButton")
        self.btn_open_platform.setIconSize(QSize(20, 20))
        self.btn_open_platform.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_platform.setToolTip("在默认浏览器打开平台")
        self.btn_open_platform.clicked.connect(self.open_platform)
        self.label_platform_url = QLabel(platform_url("本机IP"))
        self.label_platform_url.setObjectName("heroHint")
        self.label_platform_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.btn_open_platform, 0, Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.label_platform_url, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        h.addLayout(right)
        v.addWidget(hero)

        # 组件异常提示条（默认隐藏；点按跳转功能组件页）
        self.comp_banner = QPushButton()
        self.comp_banner.setObjectName("bannerWarn")
        self.comp_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.comp_banner.clicked.connect(lambda: self.show_page("components"))
        self.comp_banner.setVisible(False)
        v.addWidget(self.comp_banner)

        # 数据卡片
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_install = self._stat_card(stats, "package", "待安装", "#2563EB")
        self.stat_update = self._stat_card(stats, "arrow-up", "待更新", theme.PRIMARY)
        self.stat_last_check = self._stat_card(stats, "clock", "最近检查", theme.MUTED)
        self.stat_sql = self._stat_card(stats, "database", "SQL 同步", theme.SUCCESS)
        v.addLayout(stats)

        # 快捷操作
        card = QFrame()
        card.setObjectName("card")
        c = QVBoxLayout(card)
        c.setContentsMargins(20, 16, 20, 18)
        c.setSpacing(12)
        cap = QLabel("快捷操作")
        cap.setObjectName("sectionTitle")
        c.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_home_check = QPushButton(theme.icon("refresh"), "检查更新")
        self.btn_home_check.clicked.connect(self.on_check_clicked)
        self.btn_go_updates = QPushButton(theme.icon("layers"), "查看待更新")
        self.btn_go_updates.clicked.connect(lambda: self.show_page("updates"))
        for b in (self.btn_home_check, self.btn_go_updates):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        row.addWidget(self.btn_hostip)
        row.addStretch(1)
        c.addLayout(row)
        v.addWidget(card)
        v.addStretch(1)
        return page

    def _stat_card(self, row: QHBoxLayout, icon_name: str, caption: str, color: str) -> QLabel:
        card = QFrame()
        card.setObjectName("statCard")
        g = QVBoxLayout(card)
        g.setContentsMargins(18, 16, 18, 16)
        g.setSpacing(4)
        top = QHBoxLayout()
        ico = QLabel()
        ico.setPixmap(theme.icon_pixmap(icon_name, color, 20))
        top.addWidget(ico)
        top.addStretch(1)
        g.addLayout(top)
        value = QLabel("—")
        value.setObjectName("statValue")
        g.addWidget(value)
        cap = QLabel(caption)
        cap.setObjectName("statCaption")
        g.addWidget(cap)
        row.addWidget(card, 1)
        return value

    # ---------- 功能组件页 ----------

    def _build_components_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("功能组件")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_recover = QPushButton(theme.icon("refresh", "#FFFFFF"), "  一键恢复")
        self.btn_recover.setObjectName("primary")
        self.btn_recover.setIconSize(QSize(16, 16))
        self.btn_recover.setToolTip("对异常/未创建的组件执行 compose up 拉起")
        self.btn_recover.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recover.clicked.connect(self.on_recover_clicked)
        head.addWidget(self.btn_recover)
        v.addLayout(head)

        self.comp_table = QTableWidget(0, 4)
        self.comp_table.setHorizontalHeaderLabels(["组件", "状态", "镜像", "端口"])
        self.comp_table.setAlternatingRowColors(True)
        self.comp_table.verticalHeader().setVisible(False)
        self.comp_table.setShowGrid(False)
        self.comp_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.comp_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.comp_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        v.addWidget(self.comp_table, 1)

        hint = QLabel("「一键恢复」对状态异常的组件执行 docker compose up -d 拉起；"
                      "已正常运行的组件不受影响。")
        hint.setObjectName("statCaption")
        v.addWidget(hint)
        return page

    def refresh_components(self) -> None:
        """拉取组件列表并填表；只显示「一键恢复」可用性。"""
        self._run(client.components, self._fill_components,
                  lambda e: self.label_status.setText(f"组件状态获取失败: {e}"))

    def _fill_components(self, result: dict) -> None:
        comps = result.get("components") or []
        self._components = comps
        table = self.comp_table
        table.setRowCount(len(comps))
        for row, c in enumerate(comps):
            for col, text in enumerate([
                c.get("service", ""), c.get("status_text", c.get("status", "")),
                c.get("image", ""), " ".join(c.get("ports") or []),
            ]):
                item = QTableWidgetItem(str(text))
                item.setToolTip(str(text))
                table.setItem(row, col, item)
            # 状态列着色（色觉无障碍：文字本身已含语义）
            status_item = table.item(row, 1)
            color = (theme.DANGER if c.get("status") in ("unhealthy", "restarting", "exited")
                     else (theme.WARNING if c.get("status") == "missing"
                           else theme.SUCCESS))
            status_item.setForeground(QColor(color))
        table.resizeColumnsToContents()
        abnormal = [c for c in comps if c.get("abnormal")]
        if abnormal:
            self.btn_recover.setText(f"  一键恢复（{len(abnormal)} 项异常）")
            self.btn_recover.setEnabled(True)
        else:
            self.btn_recover.setText("  一键恢复")
            self.btn_recover.setEnabled(bool(comps))  # 列表为空（不可用）时禁用

    def on_recover_clicked(self) -> None:
        """一键恢复：全部异常组件 compose up；进行中按钮禁用防重入。"""
        comps = getattr(self, "_components", [])
        abnormal = [c["service"] for c in comps if c.get("abnormal")]
        if not abnormal:
            self.log("组件全部正常，无需恢复")
            return
        ret = QMessageBox.question(
            self, "一键恢复",
            f"将对 {len(abnormal)} 个异常组件执行 docker compose up -d 拉起：\n"
            f"{', '.join(abnormal)}\n\n是否继续？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.btn_recover.setEnabled(False)
        self._busy = True
        self.busy_changed.emit(True)
        self.log(f"开始恢复组件（{len(abnormal)} 项）…")
        self._run(lambda: client.components_recover(abnormal),
                  self._recover_done, self._recover_failed)

    def _recover_done(self, report: dict) -> None:
        self._busy = False
        self.busy_changed.emit(False)
        recovered = report.get("recovered") or []
        failed = report.get("failed") or []
        errors = report.get("errors") or {}
        for svc in recovered:
            self.log(f"✓ {svc} 已恢复")
        for svc in failed:
            self.log(f"✗ {svc} 恢复失败: {errors.get(svc, '')}")
        self.log(f"恢复完成: 成功 {len(recovered)}｜失败 {len(failed)}")
        self.refresh_components()

    def _recover_failed(self, error: str) -> None:
        self._busy = False
        self.busy_changed.emit(False)
        self.log(f"恢复请求失败: {error}")
        self.btn_recover.setEnabled(True)

    def _update_component_banner(self, abnormal: list[str]) -> None:
        """首页异常提示条：有异常组件时显示并跳转组件页。"""
        banner = getattr(self, "comp_banner", None)
        if banner is None:
            return
        if abnormal:
            names = "、".join(abnormal[:5]) + ("…" if len(abnormal) > 5 else "")
            banner.setText(f"⚠ {len(abnormal)} 个组件运行异常（{names}）— 点击查看")
            banner.setVisible(True)
        else:
            banner.setVisible(False)

    def show_page(self, key: str) -> None:
        pages = {
            "home": self.page_home,
            "updates": self.page_updates,
            "components": self.page_components,
        }
        page = pages.get(key, self.page_home)
        self.pages.setCurrentWidget(page)
        self.nav_buttons[key].setChecked(True)
        if key == "components":
            self.refresh_components()
        elif key == "updates" and getattr(self, "_sized_plan_id", None) is not None:
            # 表格可能在首页可见时填充（更新页隐藏、宽度未知），切页后按真实宽度铺满
            QTimer.singleShot(0, self._auto_fit_columns)

    def _set_pill(self, text: str, tone: str) -> None:
        self.status_pill.setText(text)
        self.status_pill.setProperty("tone", tone)
        theme.repolish(self.status_pill)

    # ---------- 打开平台 ----------

    def open_platform(self) -> None:
        """默认浏览器打开 https://本机IP:443/social_web。

        IP 优先用部署 .env 的 HOST_IP（与 xray-rest 一致），取不到再本机探测。
        """
        ip = self._host_ip
        if not ip:
            from ..core.host_ip import detect_host_ip

            ip = detect_host_ip()
        if not ip:
            self.log("打开平台失败：无法获取本机 IP（可先点「刷新 IP」）")
            QMessageBox.warning(self, "打开平台", "无法获取本机 IP，请先点击「刷新 IP」。")
            return
        url = platform_url(ip)
        self.label_platform_url.setText(url)
        if not QDesktopServices.openUrl(QUrl(url)):
            self.log(f"打开浏览器失败，请手动访问: {url}")
            return
        self.log(f"已在浏览器打开平台: {url}")

    def _update_home(self, status: dict, pending: dict) -> None:
        items = pending.get("pending") or []
        not_installed = [p for p in items if p.get("old") is None and p.get("type") != "sqlsync"]
        self.stat_install.setText(str(len(not_installed)))
        self.stat_update.setText(str(len(items) - len(not_installed)))
        self.stat_last_check.setText(_fmt_time(status.get("last_check")))
        sql = status.get("sqlsync") or {}
        last_sql = sql.get("last_status")
        self.stat_sql.setText(
            "—" if not last_sql else ("正常" if last_sql == "success" else "异常"))
        if status.get("updating"):
            self._set_pill("更新中", "busy")
        elif status.get("busy"):
            self._set_pill("检查中", "busy")
        else:
            self._set_pill("服务运行中", "ok")
        if self._host_ip:
            self.label_platform_url.setText(platform_url(self._host_ip))
        self._update_component_banner(status.get("components_abnormal") or [])

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
        self._set_pill("服务未运行", "down")
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
        host_ip = status.get("host_ip")
        self._host_ip = host_ip or None
        ip_text = f"｜IP: {host_ip}" if host_ip else ""
        tz = status.get("timezone")
        tz_text = f"｜时区: {tz}" if tz else ""
        self.label_status.setText(
            f"{state_text}{ip_text}{tz_text}｜最近检查: {_fmt_time(last)}{sql_text}")
        self.btn_check.setEnabled(not busy)
        self.btn_home_check.setEnabled(not busy)
        self._update_update_button()
        self.busy_changed.emit(busy)  # 托盘菜单"检查更新/立即更新"跟随禁用

        self._fill_table(pending)
        self._update_home(status, pending)

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

    def on_refresh_host_ip(self) -> None:
        """刷新宿主机 IP：探测 → 写部署 .env → 重建 xray-rest。"""
        self.log("探测宿主机 IP…")
        self._set_stage("探测宿主机 IP")
        self._run(client.refresh_host_ip, self._host_ip_done,
                  lambda e: (self._set_stage(None), self.log(f"刷新 IP 失败: {e}"))[0])

    def _host_ip_done(self, result: dict) -> None:
        self._set_stage(None)
        ip = result.get("ip")
        self.log(f"宿主机 IP: {ip}（已写入部署 .env）")
        if result.get("recreated"):
            self.log("✓ xray-rest 容器已按新 HOST_IP 重建")
        elif result.get("error"):
            self.log(f"⚠ {result['error']}")
        self.refresh()

    def on_check_version_clicked(self) -> None:
        """手动检查本程序新版本（托盘菜单/顶栏按钮共用）。"""
        self.log("检查本程序新版本…")
        self._set_stage("检查新版本")
        from ..core.selfupdate import fetch_latest

        self._run(fetch_latest, self._manual_version_done,
                  lambda e: (self._set_stage(None), self.log(f"版本检查失败: {e}"))[0])

    def _manual_version_done(self, latest: dict | None) -> None:
        self._set_stage(None)
        from ..core.selfupdate import is_newer

        if latest is None:
            self.log("版本检查失败：网络不可达（GitHub）")
            return
        if not is_newer(latest["version"]):
            from .. import __version__

            self.log(f"✓ 已是最新版本 v{__version__}")
            return
        self._prompted_versions.discard(latest["version"])  # 手动检查允许再次弹框
        self._prompt_self_update(latest)

    def _prompt_self_update(self, data: dict) -> None:
        """新版本弹框提示（前往下载页 / 稍后）；每版本每次会话只提示一次。"""
        latest = data.get("version") or ""
        if not latest or latest in getattr(self, "_prompted_versions", set()):
            return
        getattr(self, "_prompted_versions", set()).add(latest)
        self.log(f"◆ 发现新版本 v{latest}（当前 v{data.get('current')}）")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("发现新版本")
        box.setText(f"助手管家有新版本 v{latest}（当前 v{data.get('current')}）。\n\n"
                    "建议升级以获得最新功能与修复。")
        btn_install = box.addButton("下载并安装", QMessageBox.ButtonRole.AcceptRole)
        btn_open = box.addButton("前往下载页", QMessageBox.ButtonRole.ActionRole)
        box.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_install:
            self._download_and_install(data)
        elif clicked is btn_open:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl

            QDesktopServices.openUrl(QUrl(str(data.get("url") or "")))
            self.log(f"已打开下载页: {data.get('url')}")

    def _download_and_install(self, data: dict) -> None:
        """下载新版安装包 → 确认后退出程序并静默运行安装器（安装器自带停旧进程）。"""
        from ..core.selfupdate import download_setup

        self._busy = True
        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.log(f"开始下载 v{data.get('version')} 安装包（下载完成后将询问安装）…")

        def _dl():
            return download_setup(
                data,
                on_progress=lambda done, total: self.log_line.emit(
                    f"  下载 {done // 1048576}MB"
                    + (f"/{total // 1048576}MB" if total else "")))

        self._run(_dl, self._installer_ready, self._installer_download_failed)

    def _installer_ready(self, path) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self._update_update_button()
        from pathlib import Path

        size_mb = Path(path).stat().st_size // 1048576
        self.log(f"安装包下载完成: {path}（{size_mb} MB）")
        ret = QMessageBox.question(
            self, "安装新版本",
            f"安装包已就绪（{size_mb} MB）。\n\n立即安装将退出程序并自动升级"
            "（升级完成后自动重启）。是否继续？")
        if ret != QMessageBox.StandardButton.Yes:
            self.log("已保留安装包，未安装（可手动运行）")
            return
        import subprocess

        self.log("退出程序并启动安装器…")
        subprocess.Popen([str(path), "/SILENT"], close_fds=True)
        self._busy = False  # 跳过退出确认
        QApplication.quit()

    def _installer_download_failed(self, error: str) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self._update_update_button()
        self.log(f"安装包下载失败: {error}（可改用【前往下载页】手动下载）")
        self.notify.emit("客户端升级下载失败", str(error)[:160])

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
        self.btn_home_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.log("开始检查…")
        self._run(client.check, self._check_done, self._check_failed)

    def _check_done(self, plan: dict) -> None:
        self._busy = False
        count = len(plan.get("pending") or [])
        self.log(f"检查完成: {count} 项待更新")
        self.refresh()

    def _check_failed(self, error: str) -> None:
        """检查失败兜底；409（更新进行中）是正常互斥，提示等待而非报错。

        检查并发已在服务端合并，409 只剩更新竞态一种来源。
        """
        self._busy = False
        self.progress.setVisible(False)
        if "409" in error:
            self.log("更新正在进行中，请等更新完成后再检查")
        else:
            self.log(f"操作失败: {error}")
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

    # ---------- pull 超时确认（继续/中断） ----------

    def _ask_pull_confirm(self, item: str, elapsed: int) -> None:
        """镜像拉取超过确认阈值 → 弹模态框：继续等待 / 中断并回滚。

        模态框在嵌套事件循环中运行，期间 WS 事件（done/update_done）仍能进来，
        由 _close_pull_confirm 关闭本框（此时不发送决策，服务端已自行收尾）。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("拉取耗时较长")
        box.setText(
            f"镜像 {item} 拉取已耗时 {elapsed // 60} 分钟仍未完成。\n\n"
            "「继续等待」：继续拉取直到完成或达到超时上限；\n"
            "「中断并回滚」：终止本次拉取并回滚到旧版本。"
        )
        btn_cont = box.addButton("继续等待", QMessageBox.ButtonRole.AcceptRole)
        btn_abort = box.addButton("中断并回滚", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(btn_cont)
        self._pull_confirm_box = box
        box.exec()
        self._pull_confirm_box = None
        clicked = box.clickedButton()
        if clicked is None:  # 被 update_done/done 自动关闭（拉取已结束），不发决策
            return
        action = "continue" if clicked is btn_cont else "abort"
        self.log(f"{'继续等待' if action == 'continue' else '已请求中断并回滚'}：{item}")
        self._run(lambda: client.pull_decision(action))

    def _close_pull_confirm(self) -> None:
        box = getattr(self, "_pull_confirm_box", None)
        if box is not None:
            self._pull_confirm_box = None
            box.close()

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
                self._close_pull_confirm()
                rec = data.get("record") or {}
                self.log(f"■ {item} → {rec.get('result')}"
                         + (f"（{rec.get('error')}）" if rec.get("error") else ""))
                self.progress.setValue(self.progress.value() + 1)
            elif stage == "pull_confirm":
                self._ask_pull_confirm(item, int(data.get("elapsed") or 0))
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
            self._close_pull_confirm()
            self.progress.setValue(self.progress.maximum())
            if getattr(self, "_awaiting_update_result", False):
                self._awaiting_update_result = False
                self._busy = False
                summary = data.get("summary") or {}
                self.log(f"更新汇总: 成功 {summary.get('success', 0)}｜"
                         f"失败 {summary.get('failed', 0)}｜"
                         f"回滚 {summary.get('rolled_back', 0)}")
            self.refresh()
        elif event == "self_update_available":
            self._prompt_self_update(data)
        elif event == "recover_progress":
            stage = data.get("stage", "")
            if stage == "tag_backfill":
                self.log(f"· 已补齐 compose tag 变量: {', '.join(data.get('vars') or [])}")
            elif stage == "mount_fix":
                if data.get("fixed"):
                    self.log(f"· 已修复挂载占位目录并拷回文件: {', '.join(data['fixed'])}")
                for b in data.get("blocked") or []:
                    self.log(f"⚠ 挂载占位未能自动修复 {b.get('path')}: {b.get('reason')}")
            elif stage == "start":
                self._set_stage(f"恢复 {data.get('service', '')}")
            elif stage == "done":
                self._set_stage(None)
                ok = data.get("ok")
                svc = data.get("service", "")
                if ok:
                    self.log(f"✓ {svc} 恢复完成")
                else:
                    self.log(f"✗ {svc} 恢复失败: {data.get('error', '')}")
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
