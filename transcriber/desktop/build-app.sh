#!/bin/zsh
# 生成「会议录制工作台.app」:复制 Electron、注入 desktop 源码、自绘图标、ad-hoc 重签名。
# 幂等,可重复执行。用法:cd transcriber/desktop && ./build-app.sh [A|B|C]
set -euo pipefail

cd "$(dirname "$0")"
VARIANT="${1:-A}"
ROOT="/Users/rendy/财务知识"
SRC_ELECTRON="$ROOT/.tools/electron/Electron.app"
APP="$ROOT/会议录制工作台.app"

[[ -d "$SRC_ELECTRON" ]] || { echo "找不到 Electron: $SRC_ELECTRON"; exit 1; }

echo "[1/5] 渲染图标($VARIANT)…"
python3 make_icon.py --icns . "$VARIANT"
python3 make_icon.py app-icon-128.png 128 "$VARIANT"

echo "[2/5] 复制 Electron → $APP …"
rm -rf "$APP"
ditto "$SRC_ELECTRON" "$APP"

echo "[3/5] 注入应用代码…"
mkdir -p "$APP/Contents/Resources/app"
for f in package.json main.js live-window.js preload.js guide.html app-icon-128.png; do
  [[ -f "$f" ]] && cp "$f" "$APP/Contents/Resources/app/" || true
done
cp AppIcon.icns "$APP/Contents/Resources/"

echo "[4/5] 改写 Info.plist…"
python3 - "$APP/Contents/Info.plist" <<'EOF'
import plistlib, sys
p = sys.argv[1]
with open(p, 'rb') as f:
    pl = plistlib.load(f)
pl['CFBundleName'] = '会议录制工作台'
pl['CFBundleDisplayName'] = '会议录制工作台'
pl['CFBundleIdentifier'] = 'com.rendy.meeting-recorder'
pl['CFBundleIconFile'] = 'AppIcon'
pl['CFBundleShortVersionString'] = '1.1.0'
pl['CFBundleVersion'] = '3'
pl['NSMicrophoneUsageDescription'] = '录制现场音频,用于本地转录。音频与文稿不会离开本机。'
with open(p, 'wb') as f:
    plistlib.dump(pl, f, sort_keys=True)
EOF

echo "[5/5] 签名(稳定自签证书优先,缺失则退 ad-hoc)…"
if security find-identity -v -p codesigning 2>/dev/null | grep -q rendy-local-dev; then
  codesign --force --deep -s "rendy-local-dev" "$APP"
  echo "已用稳定证书 rendy-local-dev 签名(重建不换身份,TCC 授权持续有效)"
else
  codesign --force --deep -s - "$APP"
  echo "警告:无 rendy-local-dev 证书,退回 ad-hoc(重建会使 TCC 授权失效)"
fi
codesign --verify --deep --verbose=1 "$APP" 2>&1 | tail -1
plutil -lint "$APP/Contents/Info.plist"

# 同路径重建后 Dock/ Finder 常回退显示 Electron 默认图标(LaunchServices 缓存未刷新),
# 构建完强制重注册并重启 Dock
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
killall Dock >/dev/null 2>&1 || true

echo "完成:$APP"
