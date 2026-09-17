"""UI 入口：callfans-ui。"""

from __future__ import annotations

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .tray import TrayController

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关窗口驻托盘，退出走托盘菜单
    window = MainWindow()
    tray = TrayController(window)  # noqa: F841（需持有引用防 GC）
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
