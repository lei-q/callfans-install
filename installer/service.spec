# 服务包：不含任何 GUI 依赖（headless 可装）
# 用法: pyinstaller --clean --noconfirm installer/service.spec
import os

hiddenimports = [
    # uvicorn 程序化启动时 PyInstaller 收不全
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [os.path.join(SPECPATH, "entries", "service_entry.py")],
    pathex=[os.path.abspath(os.path.join(SPECPATH, ".."))],
    hiddenimports=hiddenimports,
    excludes=["PySide6", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="callfans-service",
    debug=False,
    console=False,  # 由 UI/系统拉起，日志落文件，不弹控制台窗口
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, name="callfans-service")
