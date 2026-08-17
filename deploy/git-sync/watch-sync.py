#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地监听守护（方案 ③）：backend/ 文件保存即自动 commit + push 到 prod。

效果：开发机改完代码保存后，8 秒内自动同步到生产并重启后端，零手动操作。
与方案 ② 互补——② 是「手动 git push 即上线」，③ 是「保存即自动 push」。

依赖：仅 Python 标准库。若环境中装了 watchdog 则用事件驱动（更省 CPU），
      否则自动回退为 git status 轮询。

配置（环境变量，均可选）：
  GIT_SYNC_REPO      仓库根目录（默认：本脚本所在 deploy/git-sync 的上一级）
  GIT_SYNC_REMOTE    远程名（默认 prod）
  GIT_SYNC_BRANCH    分支名（默认 main）
  GIT_SYNC_SCOPE     纳入自动同步的相对路径前缀，逗号分隔（默认 backend）
                     不在范围内的改动（如 frontend/、docs/）不会被自动推送
  GIT_SYNC_DEBOUNCE  防抖秒数，合并连击保存（默认 8）
  GIT_SYNC_POLL      轮询间隔秒数（watchdog 不可用时的回退，默认 5）
  GIT_SYNC_LOG       日志文件路径（默认 <repo>/watch-sync.log）
  GIT_SYNC_PID       进程 PID 文件路径（默认 <repo>/watch-sync.pid）
"""
import os
import sys
import time
import subprocess
import threading
import logging
import datetime

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG = True
except ImportError:
    WATCHDOG = False

REPO_ROOT = os.environ.get(
    "GIT_SYNC_REPO",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
)
REMOTE = os.environ.get("GIT_SYNC_REMOTE", "prod")
BRANCH = os.environ.get("GIT_SYNC_BRANCH", "main")
SCOPE = [s.strip() for s in os.environ.get("GIT_SYNC_SCOPE", "backend").split(",") if s.strip()]
DEBOUNCE = float(os.environ.get("GIT_SYNC_DEBOUNCE", "8"))
POLL = float(os.environ.get("GIT_SYNC_POLL", "5"))
LOG_FILE = os.environ.get("GIT_SYNC_LOG", os.path.join(REPO_ROOT, "watch-sync.log"))
PID_FILE = os.environ.get("GIT_SYNC_PID", os.path.join(REPO_ROOT, "watch-sync.pid"))


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [watch-sync] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def git(*args):
    """在仓库根执行 git 命令，返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def changed_files():
    """返回当前工作区变更文件列表（含未跟踪）。"""
    rc, out, _ = git("status", "--porcelain")
    if rc != 0 or not out:
        return []
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # 处理重命名 "a -> b"
        if " -> " in path:
            path = path.split(" -> ")[-1]
        files.append(path)
    return files


def in_scope(files):
    return any(any(f == s or f.startswith(s + "/") for s in SCOPE) for f in files)


def remote_exists():
    rc, _, _ = git("remote", "get-url", REMOTE)
    return rc == 0


def commit_and_push():
    """自动提交并推送。返回 True 表示已推送/提交。"""
    git("add", "-A")
    rc, _, _ = git("diff", "--cached", "--quiet")
    if rc == 0:
        return False  # 暂存区为空，无变化

    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    rc, _, err = git("commit", "-q", "-m", f"auto-sync: {ts}")
    if rc != 0:
        logging.warning("提交失败: %s", err)
        return False

    if not remote_exists():
        logging.info("未配置远程 '%s'，仅本地提交（不推送）", REMOTE)
        return True

    rc, _, err = git("push", REMOTE, BRANCH)
    if rc != 0:
        logging.error("推送失败（prod 可能不可用）：%s", err)
        return True
    logging.info("已自动同步并推送到 %s/%s", REMOTE, BRANCH)
    return True


def write_pid():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def clear_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass


def main():
    setup_logging()
    write_pid()
    mode = "watchdog(事件驱动)" if WATCHDOG else f"轮询(每{POLL}s)"
    logging.info("监听守护启动 | 仓库=%s | 范围=%s | 防抖=%ss | 模式=%s",
                 REPO_ROOT, ",".join(SCOPE), DEBOUNCE, mode)

    stop = threading.Event()

    if WATCHDOG:
        timer = None
        lock = threading.Lock()

        def schedule():
            global timer
            with lock:
                if timer is not None:
                    timer.cancel()
                timer = threading.Timer(DEBOUNCE, fire)
                timer.daemon = True
                timer.start()

        def fire():
            files = changed_files()
            if not files:
                return
            if not in_scope(files):
                logging.info("检测到变更但不在自动同步范围（%s），跳过",
                             ", ".join(files[:3]) + ("..." if len(files) > 3 else ""))
                return
            commit_and_push()

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                # 忽略自身日志/pid 文件
                if getattr(event, "src_path", "").endswith(("watch-sync.log", "watch-sync.pid")):
                    return
                schedule()

        observer = Observer()
        observer.schedule(_Handler(), REPO_ROOT, recursive=True)
        observer.start()
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        observer.stop()
        observer.join()
    else:
        logging.info("未安装 watchdog，使用轮询模式（如需更省资源：pip install watchdog）")
        while not stop.is_set():
            try:
                files = changed_files()
                if files and in_scope(files):
                    commit_and_push()
            except Exception as e:  # 轮询循环不应因单次异常退出
                logging.error("轮询异常: %s", e)
            time.sleep(POLL)

    clear_pid()
    logging.info("监听守护已退出")


if __name__ == "__main__":
    main()
