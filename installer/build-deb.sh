#!/usr/bin/env bash
# 组装 .deb（需先跑完 service.spec / gui.spec，产物在 dist/）
# 用法: installer/build-deb.sh   （版本可经 VERSION 环境变量覆盖）
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python}"
VERSION="${VERSION:-$($PY - <<'EOF'
import re
print(re.search(r'version\s*=\s*"([^"]+)"', open("pyproject.toml").read()).group(1))
EOF
)}"

command -v dpkg-deb >/dev/null || { echo "缺少 dpkg-deb（需 Debian/Ubuntu 环境）"; exit 1; }
[ -d dist/callfans-service ] || { echo "缺少 dist/callfans-service（先跑 pyinstaller installer/service.spec）"; exit 1; }
[ -d dist/callfans-ui ] || { echo "缺少 dist/callfans-ui（先跑 pyinstaller installer/gui.spec）"; exit 1; }

# dpkg-deb --rootuid 是 dpkg 1.20+（22.04+）选项，20.04 不支持；
# root 构建直接打包；非 root 用 fakeroot 让归档记录 root:root 属主
build_deb() {
  local dir="$1" out="$2"
  if [ "$(id -u)" = "0" ]; then
    dpkg-deb --build "$dir" "$out"
  elif command -v fakeroot >/dev/null; then
    fakeroot -- dpkg-deb --build "$dir" "$out"
  else
    echo "警告: 非 root 且无 fakeroot，包内属主将记录为当前用户" >&2
    dpkg-deb --build "$dir" "$out"
  fi
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# ---- callfans-service（无 GUI，服务器可用）----
rm -rf "$STAGE/callfans-service"
cp -r installer/deb/callfans-service "$STAGE/callfans-service"
sed -i "s/__VERSION__/$VERSION/g" "$STAGE/callfans-service/DEBIAN/control"
mkdir -p "$STAGE/callfans-service/opt/callfans/service"
cp -r dist/callfans-service/. "$STAGE/callfans-service/opt/callfans/service/"
chmod 755 "$STAGE/callfans-service/DEBIAN/postinst" "$STAGE/callfans-service/DEBIAN/prerm"
mkdir -p dist
build_deb "$STAGE/callfans-service" "dist/callfans-service_${VERSION}_amd64.deb"

# ---- callfans（GUI，依赖服务包）----
rm -rf "$STAGE/callfans"
cp -r installer/deb/callfans "$STAGE/callfans"
sed -i "s/__VERSION__/$VERSION/g" "$STAGE/callfans/DEBIAN/control"
mkdir -p "$STAGE/callfans/opt/callfans/gui"
cp -r dist/callfans-ui/. "$STAGE/callfans/opt/callfans/gui/"
build_deb "$STAGE/callfans" "dist/callfans_${VERSION}_amd64.deb"

echo "OK:"
ls -1 "dist/callfans-service_${VERSION}_amd64.deb" "dist/callfans_${VERSION}_amd64.deb"
