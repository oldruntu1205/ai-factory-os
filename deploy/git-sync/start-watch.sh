#!/bin/bash
# ============================================================
# 方案 ③ 启动器：本地监听守护（保存即自动同步到 prod）
#
# 用法：
#   bash deploy/git-sync/start-watch.sh start    # 后台启动守护
#   bash deploy/git-sync/start-watch.sh stop     # 停止守护
#   bash deploy/git-sync/start-watch.sh status   # 查看状态
#   bash deploy/git-sync/start-watch.sh restart  # 重启
#
# 说明：
#   - 守护进程日志：<仓库根>/watch-sync.log
#   - 进程 PID 文件：<仓库根>/watch-sync.pid
#   - 未配置 prod 远程时，守护仅做本地自动提交，不推送（不会报错退出）
# ============================================================
set -uo pipefail

# Windows(Git Bash/MSYS) 下，路径需转为 Windows 风格才能被原生 python 识别
to_win() { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) cygpath -w "$1";; *) echo "$1";; esac; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR_W="$(to_win "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# 注意：只有传给原生 python 的参数才用 Windows 路径（WATCHER）；
# bash 自己的重定向/读文件（LOGFILE/PIDFILE）必须用 MSYS 路径，反斜杠会被 bash 当转义符
WATCHER="$SCRIPT_DIR_W/watch-sync.py"
PIDFILE="$REPO_ROOT/watch-sync.pid"
LOGFILE="$REPO_ROOT/watch-sync.log"

# 找一个真正可用的 Python（防止命中微软商店占位符，它启动即退出且无解释器）
find_python() {
  for c in python3 py python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c "print('ok')" >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}
if ! PY="$(find_python)"; then
  echo "❌ 未找到可用的 Python 解释器（python3 / py / python 均不可用或是商店占位符）"
  echo "   请安装 Python 3.10+，或用 Windows 自带的 launcher 手动启动："
  echo "   py -3 deploy/git-sync/watch-sync.py"
  exit 1
fi
echo "  使用解释器: $PY"

# 判断 PID 是否存活：Windows 用 tasklist，Unix 用 kill -0
pid_alive() {
  [ -n "${1:-}" ] || return 1
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      tasklist -FI "PID eq $1" 2>/dev/null | grep -qi "$1" ;;
    *)
      kill -0 "$1" 2>/dev/null ;;
  esac
}

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && pid_alive "$pid"
}

do_kill() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) taskkill -F -PID "$1" >/dev/null 2>&1 ;;
    *) kill "$1" 2>/dev/null ;;
  esac
}

launch() {
  echo "启动监听守护 → 日志: $LOGFILE"
  nohup "$PY" "$WATCHER" >> "$LOGFILE" 2>&1 &
  # 等待守护写入原生 PID 并存活（最多 4s）
  for _ in $(seq 1 4); do
    sleep 1
    if is_running; then break; fi
  done
  if is_running; then
    echo "已启动 PID $(cat "$PIDFILE")"
    echo "停止： bash deploy/git-sync/start-watch.sh stop"
  else
    echo "❌ 启动失败（未检测到存活进程），最近日志："
    tail -n 8 "$LOGFILE" 2>/dev/null || true
    rm -f "$PIDFILE"
    exit 1
  fi
}

case "${1:-start}" in
  start)
    if is_running; then
      echo "已在运行 (PID $(cat "$PIDFILE"))"
      exit 0
    fi
    launch
    ;;
  stop)
    if is_running; then
      do_kill "$(cat "$PIDFILE")" && echo "已停止"
    else
      echo "未运行"
    fi
    rm -f "$PIDFILE"
    ;;
  restart)
    if is_running; then do_kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; fi
    sleep 1
    launch
    ;;
  status)
    if is_running; then echo "运行中 (PID $(cat "$PIDFILE"))"; else echo "未运行"; fi
    ;;
  *)
    echo "用法: $0 [start|stop|status|restart]"
    exit 2
    ;;
esac
