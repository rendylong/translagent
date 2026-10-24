#!/bin/zsh
# 会议录制与转录工作台 - 桌面客户端启动脚本
# 双击运行:必要时拉起本地转写服务,再启动 Electron 客户端。
cd "$(dirname "$0")" || exit 1

if ! lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "转写服务未运行,正在启动 python3 app.py …"
  nohup python3 app.py > server.log 2>&1 &
  sleep 2
fi

# 客户端自己会轮询等服务就绪(最多 30 秒),这里不必等待
nohup "$(dirname "$(pwd)")/.tools/electron/Electron.app/Contents/MacOS/Electron" "$(pwd)/desktop" >/dev/null 2>&1 &

exit 0
