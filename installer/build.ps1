# Windows 一键构建（产物: dist\callfans-service\, dist\callfans-ui\, dist\callfans-setup-x64.exe）
# 依赖已 install -e ".[dev,gui]" + pyinstaller 的环境；Inno Setup（ISCC）可选
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

& $py -m PyInstaller --clean --noconfirm installer/service.spec
& $py -m PyInstaller --clean --noconfirm installer/gui.spec

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source installer\inno\callfans.iss
    Write-Host "OK: dist\callfans-setup-x64.exe"
} else {
    Write-Host "跳过 Inno Setup 打包（未安装 ISCC，仅产出 PyInstaller 目录）"
}
