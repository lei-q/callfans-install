# GUI 包：托盘 UI（含 PySide6）
# 用法: pyinstaller --clean --noconfirm installer/gui.spec
import os

a = Analysis(
    [os.path.join(SPECPATH, "entries", "ui_entry.py")],
    pathex=[os.path.abspath(os.path.join(SPECPATH, ".."))],
    hiddenimports=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="callfans-ui",
    debug=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, name="callfans-ui")
