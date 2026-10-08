#!/usr/bin/env python
"""
通用数据源探测器：路径扫描 + 格式指纹

跨平台 (Windows / macOS / Linux)，不依赖固定工具清单：
1. 路径扫描: 扫描 HOME 下所有点开头目录的 sessions/projects/agent-sessions 子目录
2. 格式指纹: 读文件前几行识别日志格式（与工具名无关）
3. SQLite 指纹: 按表名识别数据库类型

发现的数据源交给 generate_dashboard.py 的对应解析器处理。
"""
import os
import json
import sqlite3
import time

# 扫描预算与限制，防止在大 HOME 下失控
MAX_SCAN_DEPTH = 3          # 相对扫描起点的最大目录深度
MAX_FILES_TOTAL = 3000
TIME_BUDGET_S = 30          # 总时间预算（秒），超时返回已发现内容

SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".cache", "Cache", "cache",
    "Library", "site-packages", "venv", ".venv", "dist", "build",
    ".gradle", ".m2", "Cookies", "Cookie", "Trash", ".Trash",
    "attachments", "files", "memory", "skills", "todos",
    "shell-snapshots", "statsig", "telemetry",
}


# ── 文件头读取 ────────────────────────────────────────
def _read_head(fpath, n_lines=3):
    """读文件前 n 行（限制读取量，避免大文件拖慢）"""
    lines = []
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= n_lines:
                    break
                lines.append(line.strip())
    except (OSError, UnicodeDecodeError):
        pass
    return [l for l in lines if l]


# ── JSONL 格式指纹 ────────────────────────────────────
def fingerprint_jsonl(fpath):
    """识别 JSONL 文件的格式指纹，返回格式名或 None"""
    lines = _read_head(fpath)
    if not lines:
        return None
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not parsed:
        return None

    # 指纹1: Codex rollout — 首条是 session_meta
    if parsed[0].get("type") == "session_meta" and "payload" in parsed[0]:
        return "codex_rollout"

    # 指纹2: Claude Code 系 JSONL — 有 type=user/assistant + message 结构
    types = {d.get("type") for d in parsed}
    if types & {"user", "assistant"}:
        for d in parsed:
            msg = d.get("message")
            if isinstance(msg, dict) and ("usage" in msg or "content" in msg):
                # 区分 Proma（result 行带 _channelModelId）
                if any(d.get("type") == "result" and "_channelModelId" in d for d in parsed):
                    return "proma_jsonl"
                return "claude_jsonl"

    return None


# ── SQLite 格式指纹 ───────────────────────────────────
def fingerprint_sqlite(db_path):
    """识别 SQLite 数据库的格式指纹，返回格式名或 None"""
    # 文件头魔数检查（快速排除非 SQLite）
    try:
        with open(db_path, "rb") as fh:
            magic = fh.read(16)
        if not magic.startswith(b"SQLite format 3"):
            return None
    except OSError:
        return None

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        conn.close()
    except (sqlite3.Error, OSError):
        return None

    # 按特征表名识别（与工具名无关，同结构即命中）
    if "proxy_request_logs" in tables:
        return "cc_switch_db"
    if "invocation_model_calls" in tables:
        return "xiaocaishen_invocations"
    if "llm_calls" in tables:
        return "ironclaw_db"
    if "thread_turns" in tables:
        return "codex_thread_history"
    return None


# ── 目录扫描 ──────────────────────────────────────────
def _walk_jsonl(root_dir, deadline, counter):
    """os.walk 查找 JSONL 文件，限制深度/跳过噪音目录"""
    found = []
    root_depth = root_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if time.time() > deadline or counter["total"] >= MAX_FILES_TOTAL:
            dirnames[:] = []
            break
        depth = dirpath.count(os.sep) - root_depth
        if depth >= MAX_SCAN_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".jsonl"):
                found.append(os.path.join(dirpath, fname))
                counter["total"] += 1
    return found


def _walk_dbs(root_dir, deadline):
    """os.walk 查找 SQLite 数据库文件（限制深度 2）"""
    found = []
    root_depth = root_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if time.time() > deadline:
            break
        depth = dirpath.count(os.sep) - root_depth
        if depth >= 2:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if fname.endswith((".db", ".sqlite")):
                found.append(os.path.join(dirpath, fname))
    return found



def _sessions_container(fpath):
    """JSONL 文件的逻辑容器目录：向上找到 sessions/projects/agent-sessions 目录"""
    parts = fpath.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in ("sessions", "projects", "agent-sessions", "task_history"):
            return "/".join(parts[:i + 1])
    return os.path.dirname(fpath)


def _tool_root(fpath):
    """数据库文件的工具根目录：向上跳过 backups 等子目录"""
    d = os.path.dirname(fpath)
    parts = d.replace("\\", "/").split("/")
    while parts and parts[-1].lower() in ("backups", "backup", "db-backups"):
        parts.pop()
    return "/".join(parts)


# ── 主入口 ────────────────────────────────────────────
def detect_sources(home=None, verbose=False):
    """
    扫描 HOME，返回发现的格式化数据源
    返回: {fingerprint: {"files": [...], "roots": [...], "dirs": {dir: fp}}}
    """
    if home is None:
        home = os.path.expanduser("~")
    deadline = time.time() + TIME_BUDGET_S

    results = {}   # fingerprint -> {"files": set(), "roots": set()}
    dir_fp = {}    # dir -> fingerprint（同目录格式一致，只采样一次）
    counter = {"total": 0}

    def _add(fp, fpath, root):
        entry = results.setdefault(fp, {"files": set(), "roots": set()})
        entry["files"].add(fpath)
        entry["roots"].add(root)

    try:
        top_names = os.listdir(home)
    except OSError:
        return results

    # 只扫描点开头目录（AI 工具数据惯例位置，跨平台一致）
    candidates = sorted(n for n in top_names if n.startswith(".") and len(n) > 1)

    for cand in candidates:
        if time.time() > deadline:
            break
        cand_path = os.path.join(home, cand)
        if not os.path.isdir(cand_path):
            continue

        # ── 1) JSONL 会话扫描 ──
        # 会话目录可能在候选目录下一层或两层（如 ~/.xiaocaishen/codex/sessions）
        sub_names = ["sessions", "projects", "agent-sessions", "task_history"]
        sub_paths = []
        for sub in sub_names:
            p = os.path.join(cand_path, sub)
            if os.path.isdir(p):
                sub_paths.append(p)
        try:
            for lvl1 in os.listdir(cand_path):
                l1_path = os.path.join(cand_path, lvl1)
                if not os.path.isdir(l1_path) or lvl1 in SKIP_DIRS or lvl1.startswith("."):
                    continue
                for sub in sub_names:
                    p = os.path.join(l1_path, sub)
                    if os.path.isdir(p):
                        sub_paths.append(p)
        except OSError:
            pass

        for sub_path in sub_paths:
            for f in _walk_jsonl(sub_path, deadline, counter):
                d = os.path.dirname(f)
                if d not in dir_fp:
                    dir_fp[d] = fingerprint_jsonl(f)
                fp = dir_fp[d]
                if fp:
                    _add(fp, f, os.path.dirname(os.path.dirname(f)))

        # ── 2) SQLite 扫描（浅层） ──
        for dbf in _walk_dbs(cand_path, deadline):
            fp = fingerprint_sqlite(dbf)
            if fp:
                _add(fp, dbf, os.path.dirname(dbf))

    if verbose:
        for fp, info in results.items():
            print(f"  [{fp}] {len(info['files'])} files, roots: {len(info['roots'])}")

    # ── 去重与优选 ──
    # JSONL: 每个文件是不同会话，全部保留；root 归并到 sessions 容器目录
    # SQLite: 备份数据库会重复统计，每个工具根目录只保留最新一个
    cleaned = {}
    for fp, info in results.items():
        if fp.endswith("_db") or fp == "codex_thread_history":
            best_by_root = {}
            for f in info["files"]:
                tool_root = _tool_root(f)
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    mt = 0
                if tool_root not in best_by_root or mt > best_by_root[tool_root][1]:
                    best_by_root[tool_root] = (f, mt)
            entry = {"files": {v[0] for v in best_by_root.values()},
                     "roots": set(best_by_root.keys())}
            if entry["files"]:
                cleaned[fp] = entry
        else:
            # JSONL: roots 归并到容器目录（sessions/projects/agent-sessions 的父目录）
            roots = {_sessions_container(f) for f in info["files"]}
            cleaned[fp] = {"files": set(info["files"]), "roots": roots}

    results = cleaned

    return results


if __name__ == "__main__":
    import io
    import sys
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("扫描本机 AI 工具数据源...")
    t0 = time.time()
    srcs = detect_sources(verbose=True)
    print(f"耗时 {time.time() - t0:.1f}s, 发现 {len(srcs)} 种格式")
