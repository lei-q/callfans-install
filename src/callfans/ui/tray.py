"""托盘图标与菜单；定时检查命中新版本时弹气泡（需求 4）。"""

from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .main_window import MainWindow


def _make_icon() -> QIcon:
    """程序化生成托盘图标（v1 不随包分发图片资源）。"""
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2d7ff9"))
    painter.setPen(QColor("#1b5bb8"))
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
        self.icon.setToolTip("callfans 更新器")
        menu = QMenu()
        act_show = QAction("打开主窗口")
        act_check = QAction("检查更新")
        act_update = QAction("立即更新")
        act_quit = QAction("退出")
        act_show.triggered.connect(self._show_window)
        act_check.triggered.connect(window.on_check_clicked)
        act_update.triggered.connect(window.on_update_clicked)
        act_quit.triggered.connect(window.request_quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_check)
        menu.addAction(act_update)
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
            "callfans 发现新版本",
            f"检测到 {count} 项待更新，点击打开主窗口查看详情。",
            QSystemTrayIcon.MessageIcon.Information,
        )
        self._show_window()
