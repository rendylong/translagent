#!/bin/zsh
# 双击启动本地会议转录工作台(若已在运行则直接打开页面)
cd "$(dirname "$0")"
if lsof -nP -i :8765 >/dev/null 2>&1; then
  echo "服务已在运行"
else
  echo "启动转录服务…"
  nohup python3 app.py > server.log 2>&1 &
  sleep 2
fi
open "http://127.0.0.1:8765/"
