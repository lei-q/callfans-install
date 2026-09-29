"""UI 入口：callfans-ui。"""

from __future__ import annotations

import sys


def main() -> None:
    import sys as _sys

    if _sys.platform == "win32":
        # 任务栏按 AppUserModelID 分组取 exe 嵌入图标（PyInstaller windowed
        # 程序默认可能归到 Python 组导致显示通用图标）
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("callfans.gui")
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .tray import TrayController, _make_icon

    app = QApplication(sys.argv)
    app.setWindowIcon(_make_icon())  # 全局窗口/任务栏图标（logo）
    app.setQuitOnLastWindowClosed(False)  # 关窗口驻托盘，退出走托盘菜单
    window = MainWindow()
    tray = TrayController(window)  # noqa: F841（需持有引用防 GC）
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
