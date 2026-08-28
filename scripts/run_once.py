#!/usr/bin/env python
"""一次性生成看板并同步，供计划任务调用"""
import subprocess, shutil, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(SCRIPT_DIR, 'generate_dashboard.py')
DASH = os.path.join(os.path.dirname(SCRIPT_DIR), 'dashboard.html')
WS_COPY = os.path.join(os.path.expanduser('~'), '.proma', 'agent-workspaces', 'ai', 'workspace-files', 'usage-dashboard.html')

# Redirect output for pythonw
if sys.executable.endswith('pythonw.exe') or sys.executable.endswith('pythonw'):
    log = os.path.join(os.path.dirname(SCRIPT_DIR), 'monitor.log')
    sys.stdout = open(log, 'a', encoding='utf-8')
    sys.stderr = sys.stdout

r = subprocess.run([sys.executable, GEN], capture_output=True, text=True, timeout=60)
if r.returncode == 0 and os.path.exists(DASH):
    shutil.copy2(DASH, WS_COPY)
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    print(f'[{datetime.now(tz).strftime("%H:%M:%S")}] OK')
else:
    print(f'FAILED: {r.stderr[:200]}')
