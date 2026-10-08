#!/usr/bin/env python
"""一次性生成看板、同步副本，并增量上报云端（可选），供计划任务调用"""
import subprocess
import shutil
import os
import sys
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
GEN = os.path.join(SCRIPT_DIR, 'generate_dashboard.py')
DASH = os.path.join(SKILL_DIR, 'dashboard.html')
WS_COPY = os.path.join(os.path.expanduser('~'), '.proma', 'agent-workspaces', 'ai',
                       'workspace-files', 'usage-dashboard.html')

# 云端上报配置（可选）：REPORT_SERVER_URL 为空则不上报
REPORT_SERVER_URL = os.environ.get('AI_USAGE_SERVER', '')   # 如 https://xxx.joyapp.jd.com
REPORT_TOKEN = os.environ.get('AI_USAGE_TOKEN', '')         # 团队 token
ERP = os.environ.get('AI_USAGE_ERP', '')                    # 本机 ERP（也可从 ~/.jd-sso 自动取）
STATE_FILE = os.path.join(SKILL_DIR, 'report_state.json')   # 记录上次上报进度
EXPORT_FILE = os.path.join(SKILL_DIR, 'turns_export.json')

# Redirect output for pythonw
if sys.executable.endswith('pythonw.exe') or sys.executable.endswith('pythonw'):
    log = os.path.join(SKILL_DIR, 'monitor.log')
    sys.stdout = open(log, 'a', encoding='utf-8')
    sys.stderr = sys.stdout

from datetime import datetime, timezone, timedelta
tz = timezone(timedelta(hours=8))
now_str = datetime.now(tz).strftime('%H:%M:%S')


def detect_erp():
    """从 ~/.jd-sso 或环境自动取 ERP"""
    global ERP
    if ERP:
        return ERP
    sso_dir = os.path.join(os.path.expanduser('~'), '.jd-sso')
    if os.path.isdir(sso_dir):
        for name in os.listdir(sso_dir):
            if '.' in name and not name.startswith('.'):
                ERP = name
                return ERP
    return os.environ.get('USERNAME', 'unknown')


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError:
        pass


def report_to_server(export_path):
    """增量上报：只传上次上报之后新增的 turns。失败静默（不影响本地看板）"""
    if not REPORT_SERVER_URL or not REPORT_TOKEN:
        return
    try:
        with open(export_path, 'r', encoding='utf-8') as f:
            all_turns = json.load(f)
        state = load_state()
        last_ts = state.get('last_turn_ts', 0)
        # 增量：只上报新增；首次全量（服务端幂等去重，重复传无副作用）
        new_turns = [t for t in all_turns if t.get('turn_ts', 0) > last_ts]
        if not new_turns:
            print(f'[{now_str}] 云端无新增数据')
            return
        # 分批上报（每批 500）
        import urllib.request
        url = REPORT_SERVER_URL.rstrip('/') + '/api/report'
        erp = detect_erp()
        total_sent = 0
        for i in range(0, len(new_turns), 500):
            batch = new_turns[i:i + 500]
            body = json.dumps({"erp": erp, "turns": batch}).encode()
            req = urllib.request.Request(url, data=body, method='POST', headers={
                'Authorization': f'Bearer {REPORT_TOKEN}',
                'Content-Type': 'application/json',
            })
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            total_sent += result.get('inserted', 0)
        # 更新进度（取本批最大 ts）
        max_ts = max(t.get('turn_ts', 0) for t in new_turns)
        state['last_turn_ts'] = max_ts
        save_state(state)
        print(f'[{now_str}] 云端上报完成: {len(new_turns)} 条新增, 服务端入库 {total_sent}')
    except Exception as e:
        print(f'[{now_str}] 云端上报失败（不影响本地看板）: {e}')


def main():
    dump_arg = ['--dump-turns', EXPORT_FILE] if (REPORT_SERVER_URL and REPORT_TOKEN) else []
    r = subprocess.run([sys.executable, GEN] + dump_arg, capture_output=True, timeout=120,
                       encoding='utf-8', errors='replace')
    if r.returncode == 0 and os.path.exists(DASH):
        try:
            shutil.copy2(DASH, WS_COPY)
        except OSError:
            pass  # 非本机预览目录时跳过
        print(f'[{now_str}] OK')
        if dump_arg:
            report_to_server(EXPORT_FILE)
    else:
        print(f'FAILED: {r.stderr[:200] if r.stderr else "unknown"}')


if __name__ == '__main__':
    main()
