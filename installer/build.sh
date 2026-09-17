#!/usr/bin/env bash
# Linux 一键构建（应在 Ubuntu 20.04 上执行，保证 glibc 2.31 兼容 20.04+）
# 用法: installer/build.sh   （依赖已 install -e ".[dev,gui]" + pyinstaller 的环境）
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -m PyInstaller --clean --noconfirm installer/service.spec
"$PY" -m PyInstaller --clean --noconfirm installer/gui.spec
PY="$PY" installer/build-deb.sh
