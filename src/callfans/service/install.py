"""Linux 用户级安装（tech-design §6.2/§6.3）：

- 写 ~/.config/systemd/user/callfans.service 并 enable --now（无需 root）
- headless 场景提示 loginctl enable-linger
- 桌面场景顺带写 ~/.config/autostart/callfans-ui.desktop
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "callfans.service"


class InstallError(RuntimeError):
    pass


def _service_command(env_path: Path) -> str:
    """服务启动命令（单行，供 ExecStart 使用）。"""
    exe = shutil.which("callfans-service")
    if exe:
        return f'"{exe}" --env "{env_path}"'
    return f'"{sys.executable}" -m callfans.service.app --env "{env_path}"'


def _ui_command() -> str:
    exe = shutil.which("callfans-ui")
    if exe:
        return f'"{exe}"'
    return f'"{sys.executable}" -m callfans.ui.app'


def render_user_unit(env_path: Path) -> str:
    exec_start = _service_command(env_path)
    return f"""\
[Unit]
Description=callfans 更新服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def render_ui_autostart() -> str:
    return f"""\
[Desktop Entry]
Type=Application
Name=callfans 更新器
Exec={_ui_command()}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _systemctl(*args: str) -> None:
    if shutil.which("systemctl") is None:
        raise InstallError("未找到 systemctl（该命令仅支持 Linux systemd 环境）")
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise InstallError(f"systemctl --user {' '.join(args)} 失败: {proc.stderr.strip()}")


def install_user_service(env_path: Path, autostart_ui: bool = True) -> str:
    """安装并启动用户级服务；返回 unit 文件路径。"""
    env_path = env_path.resolve()
    if not env_path.exists():
        raise InstallError(f".env 不存在: {env_path}")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / UNIT_NAME
    unit_file.write_text(render_user_unit(env_path), encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT_NAME)
    if autostart_ui:
        autostart_dir = Path.home() / ".config" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        (autostart_dir / "callfans-ui.desktop").write_text(
            render_ui_autostart(), encoding="utf-8"
        )
    return str(unit_file)


def uninstall_user_service() -> None:
    _systemctl("disable", "--now", UNIT_NAME)
    unit_file = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    unit_file.unlink(missing_ok=True)
    (Path.home() / ".config" / "autostart" / "callfans-ui.desktop").unlink(missing_ok=True)
    _systemctl("daemon-reload")
