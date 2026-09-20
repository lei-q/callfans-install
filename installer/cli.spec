# CLI 包：callfans 命令行（status/check/update/sqlsync/service install 等）
# 用法: pyinstaller --clean --noconfirm installer/cli.spec
import os

a = Analysis(
    [os.path.join(SPECPATH, "entries", "cli_entry.py")],
    pathex=[os.path.abspath(os.path.join(SPECPATH, ".."))],
    hiddenimports=[],
    excludes=["PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="callfans",
    debug=False,
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, name="callfans-cli")
