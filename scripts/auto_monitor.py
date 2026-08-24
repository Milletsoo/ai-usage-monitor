#!/usr/bin/env python
"""
AI Coding 平台 Token 用量看板 — 自动监控守护进程
监控会话文件变化，自动重新生成 HTML 看板。

Usage:
    python auto_monitor.py              # 前台运行
    python auto_monitor.py --interval 5 # 自定义轮询间隔(秒)
    python auto_monitor.py --once       # 只生成一次，不监控
"""
import json
import os
import sys
import time
import glob
import subprocess
from datetime import datetime, timezone, timedelta

# 常量定义（必须在输出重定向之前）
TZ = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
HOME = os.path.expanduser("~")
GENERATE_SCRIPT = os.path.join(SCRIPT_DIR, "generate_dashboard.py")
OUTPUT_HTML = os.path.join(SKILL_DIR, "dashboard.html")

if sys.platform == "win32":
    # pythonw 无控制台时重定向到日志文件
    if sys.executable.endswith("pythonw.exe") or sys.executable.endswith("pythonw"):
        log_path = os.path.join(SKILL_DIR, "monitor.log")
        sys.stdout = open(log_path, "a", encoding="utf-8")
        sys.stderr = sys.stdout
    else:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 监控路径
WATCH_PATHS = [
    os.path.join(HOME, ".proma", "agent-sessions"),
    os.path.join(HOME, ".claude", "projects"),
    os.path.join(HOME, ".joycode", "default-workspace", "joycode.joycoder-editor", "task_history"),
    os.path.join(HOME, ".proma", "agent-sessions.json"),  # 会话元数据变化
]

PYTHON = sys.executable  # 使用当前 Python 解释器


def get_file_signatures():
    """获取所有监控文件的签名 (路径 -> mtime)"""
    sigs = {}
    for path in WATCH_PATHS:
        if os.path.isfile(path):
            sigs[path] = os.path.getmtime(path)
        elif os.path.isdir(path):
            for pattern in ("*.jsonl", "*.json"):
                for f in glob.glob(os.path.join(path, "**", pattern), recursive=True):
                    try:
                        sigs[f] = os.path.getmtime(f)
                    except OSError:
                        pass
    return sigs


def generate_dashboard():
    """运行看板生成脚本"""
    try:
        result = subprocess.run(
            [PYTHON, GENERATE_SCRIPT],
            capture_output=True, text=True, encoding="utf-8",
            timeout=30
        )
        if result.returncode == 0:
            now = datetime.now(TZ).strftime("%H:%M:%S")
            # 同步到 workspace-files 供浏览器预览
            import shutil
            ws_copy = os.path.join(HOME, ".proma", "agent-workspaces", "ai", "workspace-files", "usage-dashboard.html")
            if os.path.exists(OUTPUT_HTML):
                shutil.copy2(OUTPUT_HTML, ws_copy)
            # 提取关键信息
            for line in result.stdout.split("\n"):
                if "采集完成" in line:
                    print(f"[{now}] ✅ {line.strip()}")
                    return True
            print(f"[{now}] ✅ 看板已更新")
            return True
        else:
            print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] ❌ 生成失败: {result.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] ❌ 生成超时")
        return False
    except Exception as e:
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] ❌ 异常: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Token 用量看板自动监控")
    parser.add_argument("--interval", "-i", type=int, default=5, help="轮询间隔(秒)，默认5秒")
    parser.add_argument("--once", action="store_true", help="只生成一次，不监控")
    args = parser.parse_args()

    print("=" * 50)
    print("  📊 Token 用量看板自动监控守护进程")
    print("=" * 50)
    print(f"  输出: {OUTPUT_HTML}")
    print(f"  轮询间隔: {args.interval}秒")
    print(f"  监控路径:")
    for p in WATCH_PATHS:
        exists = "✅" if os.path.exists(p) else "❌"
        print(f"    {exists} {p}")
    print()

    if args.once:
        print("生成一次后退出...")
        generate_dashboard()
        return

    # 首次生成
    print("首次生成...")
    generate_dashboard()
    print()

    # 记录初始签名
    last_sigs = get_file_signatures()
    print(f"开始监控 (监控 {len(last_sigs)} 个文件)... 按 Ctrl+C 停止\n")

    try:
        while True:
            time.sleep(args.interval)
            try:
                current_sigs = get_file_signatures()

                changed = False
                for f, mtime in current_sigs.items():
                    if f not in last_sigs:
                        changed = True
                        break
                    if mtime > last_sigs[f]:
                        changed = True
                        break

                if changed:
                    generate_dashboard()
                    last_sigs = current_sigs
            except Exception as e:
                # 单次检查出错不影响整个监控
                pass

    except KeyboardInterrupt:
        print("\n\n监控已停止。")
    except Exception as e:
        # 崩溃后自动重启
        print(f"\n⚠️ 监控异常: {e}")
        print("3秒后自动重启...")
        time.sleep(3)
        import subprocess
        subprocess.Popen([sys.executable, __file__], creationflags=0x00000008)


if __name__ == "__main__":
    main()
