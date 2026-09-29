"""托盘图标与菜单；logo.svg 渲染图标；新版本气泡与菜单项。"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .main_window import MainWindow

# 产品 logo（内嵌 SVG，免资源打包；来源 logo.svg，2026-09-29）
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
    '<path fill="#CE4141" d="M920.5 189.9a126.1 126.1 0 0 0-123.5-5.5l-161.3 80.6a74.9 74.9 0 0 0-7.5 4.3 '
    '64 64 0 0 0-54.8-31.6H448a64 64 0 0 0-54.8 31.6 75.3 75.3 0 0 0-7.5-4.5l-161.5-80.6A128 128 0 0 0 '
    '40.5 298.7v133.5a126.7 126.7 0 0 0 183.7 113.5L362.7 475.7l-134.4 311.3a47.8 47.8 0 0 0 59.9 64L426.7 '
    '800a48 48 0 0 0 29-32v-2.3l48.9-275h12.4l48.2 275.2a23 23 0 0 0 0 2.3 48 48 0 0 0 29 30.9l138.5 51.9a47.6 '
    '47.6 0 0 0 51.8-12.4 47.4 47.4 0 0 0 8.5-52l-114.6-254.9 138.2 69.1a126.9 126.9 0 0 0 144.8-111.7V298.7 '
    'a126.1 126.1 0 0 0-60.8-108.8zM426.7 301.4a21.3 21.3 0 0 1 21.3-21.3h125.9a21.3 21.3 0 0 1 21.3 21.3v125.3 '
    'a21.3 21.3 0 0 1-21.3 21.3H448a21.3 21.3 0 0 1-21.3-21.3V301.4zM205.2 506.7a84.3 84.3 0 0 1-122-75.5V298.7 '
    'a84.1 84.1 0 0 1 122-75.3l161.3 80.6a31.4 31.4 0 0 1 17.5 27.1v66.8a31.4 31.4 0 0 1-17.7 28.8l-161.1 80zM414.5 '
    '757.3a5.1 5.1 0 0 1-2.8 2.6l-138.4 51.8a4.7 4.7 0 0 1-5.3-1 5.1 5.1 0 0 1 0-6.2l141-327.3A64 64 0 0 0 448 '
    '490.7h13.4zM753.5 810.7a4.7 4.7 0 0 1-5.5 1.2l-138.5-51.8a4.9 4.9 0 0 1-2.8-2.6L560.2 490.7h13.4a64 64 0 0 0 '
    '39.7-14.1l141.2 327.7a4.7 4.7 0 0 1-1 6.4zM938.7 431.1a84.3 84.3 0 0 1-122 75.3L654.9 426.7a31.4 31.4 0 0 '
    '1-17.5-28.2v-67.4a31.4 31.4 0 0 1 17.5-28.2l161.3-80.6a84.3 84.3 0 0 1 122 75.3v133.5z"/>'
    '</svg>'
)


def _render_logo(size: int) -> QPixmap | None:
    """SVG → QPixmap；QtSvg 不可用或渲染失败返回 None（走兜底圆形图标）。"""
    try:
        renderer = QSvgRenderer(QByteArray(LOGO_SVG.encode("utf-8")))
        if not renderer.isValid():
            return None
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        renderer.render(painter)
        painter.end()
        return pix
    except Exception:
        return None


def _make_icon() -> QIcon:
    """应用图标：logo.svg 优先，渲染失败兜底圆形。"""
    pix = _render_logo(128)
    if pix is not None and not pix.isNull():
        return QIcon(pix)
    # 兜底：旧圆形图标
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#CE4141"))
    painter.setPen(QColor("#8A2A2A"))
    painter.drawEllipse(6, 6, 52, 52)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(30)
    painter.setFont(font)
    painter.drawText(pix.rect(), 0x84, "C")  # AlignCenter
    painter.end()
    return QIcon(pix)


class TrayController:
    def __init__(self, window: MainWindow):
        self.window = window
        self.icon = QSystemTrayIcon(_make_icon())
        self.icon.setToolTip("助手管家")
        menu = QMenu()
        act_show = QAction("打开主窗口")
        act_check = QAction("检查更新")
        act_update = QAction("立即更新")
        act_checkver = QAction("检查新版本…")
        act_quit = QAction("退出")
        act_show.triggered.connect(self._show_window)
        act_check.triggered.connect(window.on_check_clicked)
        act_update.triggered.connect(window.on_update_clicked)
        act_checkver.triggered.connect(window.on_check_version_clicked)
        act_quit.triggered.connect(window.request_quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_check)
        menu.addAction(act_update)
        menu.addSeparator()
        menu.addAction(act_checkver)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.show()
        window.new_pending.connect(self._notify_new_pending)
        window.notify.connect(lambda title, msg: self.icon.showMessage(
            title, msg, QSystemTrayIcon.MessageIcon.Warning))

    def _show_window(self) -> None:
        self.window.showNormal()
        self.window.activateWindow()

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _notify_new_pending(self, count: int) -> None:
        self.icon.showMessage(
            "发现新版本",
            f"检测到 {count} 项待更新，点击打开主窗口查看详情。",
            QSystemTrayIcon.MessageIcon.Information,
        )
        self._show_window()
