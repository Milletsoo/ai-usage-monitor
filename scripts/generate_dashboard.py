#!/usr/bin/env python
"""
AI Coding 平台 Token 用量监控看板生成器 v3
支持多工具: Proma + Claude Code + JoyCode
"""
import json
import os
import glob
import sys
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
HOME = os.path.expanduser("~")
DEFAULT_PRICING_FILE = os.path.join(SKILL_DIR, "pricing.json")
DEFAULT_OUTPUT = os.path.join(SKILL_DIR, "dashboard.html")

# ── 数据源路径 ────────────────────────────────────────
PROMA_SESSIONS_DIR = os.path.join(HOME, ".proma", "agent-sessions")
PROMA_SESSIONS_JSON = os.path.join(HOME, ".proma", "agent-sessions.json")
CLAUDE_PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
JOYCODE_TASKS_DIR = os.path.join(HOME, ".joycode", "default-workspace", "joycode.joycoder-editor")


# ── 模型名称匹配 ──────────────────────────────────────
def build_model_matcher(pricing_data):
    models = pricing_data.get("models", [])
    exact = {}
    aliases = {}
    for m in models:
        mid = m["id"].lower()
        exact[mid] = m
        dn = m.get("display_name", "").lower()
        if dn:
            aliases[dn] = m
        if "/" in mid:
            aliases[mid.split("/")[-1]] = m
    return exact, aliases


def match_model(model_id, exact, aliases):
    if not model_id:
        return None
    mid = model_id.lower().strip()
    if mid in exact:
        return exact[mid]
    if mid in aliases:
        return aliases[mid]
    if "/" in mid:
        core = mid.split("/")[-1]
        if core in exact:
            return exact[core]
        if core in aliases:
            return aliases[core]
    for key, val in {**exact, **aliases}.items():
        nk = key.replace(" ", "").replace("-", "").replace(".", "")
        nm = mid.replace(" ", "").replace("-", "").replace(".", "")
        if nk == nm:
            return val
    return None


# ── 价格计算 ──────────────────────────────────────────
def calculate_turn_cost(model_price, usage, created_at_ms):
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    total_input_context = input_tokens + cache_read + cache_create
    total_cost = 0.0
    pricing_type = model_price.get("pricing_type", "flat")

    if pricing_type == "flat":
        p_in = model_price.get("input", 0)
        p_out = model_price.get("output", 0)
        p_cr = model_price.get("cache_read")
        p_cw = model_price.get("cache_write_5min") or model_price.get("cache_write")
        total_cost += input_tokens / 1e6 * p_in + output_tokens / 1e6 * p_out
        if p_cr and cache_read > 0:
            total_cost += cache_read / 1e6 * p_cr
        if p_cw and cache_create > 0:
            total_cost += cache_create / 1e6 * p_cw
    elif pricing_type == "tiered":
        threshold = model_price.get("tier_threshold", 999999999)
        tp = model_price["high_tier" if total_input_context > threshold else "low_tier"]
        total_cost += input_tokens / 1e6 * tp["input"] + output_tokens / 1e6 * tp["output"]
        if tp.get("cache_read") and cache_read > 0:
            total_cost += cache_read / 1e6 * tp["cache_read"]
    elif pricing_type == "peak_offpeak":
        ps, pe = map(int, model_price.get("peak_hours", "8-22").split("-"))
        dt = datetime.fromtimestamp(created_at_ms / 1000, tz=TZ)
        tp = model_price["peak" if ps <= dt.hour < pe else "offpeak"]
        total_cost += input_tokens / 1e6 * tp["input"] + output_tokens / 1e6 * tp["output"]
        if tp.get("cache_read") and cache_read > 0:
            total_cost += cache_read / 1e6 * tp["cache_read"]
    return round(total_cost, 6)


# ── Proma 会话名映射 ──────────────────────────────────
def load_proma_session_titles():
    """从 agent-sessions.json 加载会话标题"""
    titles = {}
    try:
        with open(PROMA_SESSIONS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s in data.get("sessions", []):
            sid = s.get("id", "")
            sdk_sid = s.get("sdkSessionId", "")
            title = s.get("title", "")
            if title:
                titles[sid] = title
                if sdk_sid:
                    titles[sdk_sid] = title
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return titles


# ── Proma 数据采集 ────────────────────────────────────
def collect_proma(exact, aliases, proma_titles):
    files = glob.glob(os.path.join(PROMA_SESSIONS_DIR, "*.jsonl"))
    sessions = []
    all_turns = []
    unmatched = set()

    for fpath in sorted(files):
        sid_file = os.path.basename(fpath).replace(".jsonl", "")
        session_title = proma_titles.get(sid_file, "")
        session_data = {"file_id": sid_file, "tool": "Proma", "turns": [], "total_input": 0,
            "total_output": 0, "total_cache_read": 0, "total_cache_create": 0, "total_cost_cny": 0.0,
            "models_used": set(), "skills_used": set(), "first_text": "", "first_created": None,
            "last_created": None, "title": session_title, "date": None}

        with open(fpath, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        latest_user_prompt = ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "user":
                parent_id = d.get("parent_tool_use_id")
                msg = d.get("message", {})
                content = msg.get("content", [])
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "")
                            break
                if text and not parent_id:
                    latest_user_prompt = text
                    if not session_data["first_text"]:
                        session_data["first_text"] = text
            elif d.get("type") == "result":
                usage = d.get("usage", {})
                model_id = d.get("_channelModelId", "unknown")
                created_ms = d.get("_createdAt", 0)
                duration_ms = d.get("_durationMs", 0)
                skills = d.get("skill_activations", [])
                skill_names = [s.get("slug", "") for s in skills] if skills else []
                mp = match_model(model_id, exact, aliases)
                if mp:
                    cost_cny = calculate_turn_cost(mp, usage, created_ms)
                    matched_name = mp.get("display_name", model_id)
                else:
                    cost_cny = 0.0
                    matched_name = model_id
                    unmatched.add(model_id)
                turn = {
                    "tool": "Proma", "session_file_id": sid_file, "model_id": model_id,
                    "matched_name": matched_name,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_create_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cost_cny": cost_cny, "created_ms": created_ms,
                    "created_dt": datetime.fromtimestamp(created_ms / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S") if created_ms else "",
                    "duration_ms": duration_ms, "skills": skill_names,
                    "prompt_text": latest_user_prompt,
                    "session_title": session_title, "session_first_text": session_data["first_text"],
                }
                session_data["turns"].append(turn)
                all_turns.append(turn)
                session_data["total_input"] += turn["input_tokens"]
                session_data["total_output"] += turn["output_tokens"]
                session_data["total_cache_read"] += turn["cache_read_tokens"]
                session_data["total_cache_create"] += turn["cache_create_tokens"]
                session_data["total_cost_cny"] += cost_cny
                session_data["models_used"].add(matched_name)
                if skill_names:
                    session_data["skills_used"].update(skill_names)
                if created_ms:
                    if session_data["first_created"] is None or created_ms < session_data["first_created"]:
                        session_data["first_created"] = created_ms
                    if session_data["last_created"] is None or created_ms > session_data["last_created"]:
                        session_data["last_created"] = created_ms

        if session_data["turns"]:
            _finalize_session(session_data)
            sessions.append(session_data)
    return sessions, all_turns, unmatched


# ── Claude Code 数据采集 ──────────────────────────────
def collect_claude_code(exact, aliases):
    files = glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "**/*.jsonl"), recursive=True)
    # Skip subagent files
    files = [f for f in files if "/subagents/" not in f and "\\subagents\\" not in f]
    sessions = []
    all_turns = []
    unmatched = set()

    for fpath in sorted(files):
        sid_file = os.path.basename(fpath).replace(".jsonl", "")
        session_title = ""
        session_first_text = ""
        session_data = {"file_id": sid_file, "tool": "Claude Code", "turns": [],
            "total_input": 0, "total_output": 0, "total_cache_read": 0, "total_cache_create": 0,
            "total_cost_cny": 0.0, "models_used": set(), "skills_used": set(),
            "first_text": "", "first_created": None, "last_created": None,
            "title": "", "date": None}

        with open(fpath, "r", encoding="utf-8") as fh:
            lines = fh.readlines()

        latest_user_prompt = ""
        seen_turns = set()  # 去重: (session_id, ts_second, model, input, output)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type", "")
            if t == "ai-title":
                session_title = d.get("aiTitle", "")
                session_data["title"] = session_title
            elif t == "user":
                msg = d.get("message", {})
                content = msg.get("content", [])
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "")
                            break
                elif isinstance(content, str):
                    text = content
                if text and len(text) > 5:
                    latest_user_prompt = text
                    if not session_first_text:
                        session_first_text = text
                        session_data["first_text"] = text
            elif t == "assistant":
                msg = d.get("message", {})
                usage = msg.get("usage", {})
                inp = usage.get("input_tokens", 0)
                outp = usage.get("output_tokens", 0)
                if inp == 0 and outp == 0:
                    continue
                model_id = msg.get("model", "unknown")
                ts_str = d.get("timestamp", "")
                # Parse ISO timestamp
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    created_ms = int(dt.timestamp() * 1000)
                except (ValueError, AttributeError):
                    created_ms = 0

                # 去重: Claude Code 对同一次 API 调用会记录多个 assistant 条目
                # (text/thinking 交替)，它们共享相同的 usage 数据
                dedup_key = (sid_file, created_ms // 1000, model_id, inp, outp)
                if dedup_key in seen_turns:
                    continue
                seen_turns.add(dedup_key)

                usage_norm = {
                    "input_tokens": inp,
                    "output_tokens": outp,
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                }

                mp = match_model(model_id, exact, aliases)
                if mp:
                    cost_cny = calculate_turn_cost(mp, usage_norm, created_ms)
                    matched_name = mp.get("display_name", model_id)
                else:
                    cost_cny = 0.0
                    matched_name = model_id
                    unmatched.add(model_id)

                turn = {
                    "tool": "Claude Code", "session_file_id": sid_file,
                    "model_id": model_id, "matched_name": matched_name,
                    "input_tokens": inp, "output_tokens": outp,
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_create_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cost_cny": cost_cny, "created_ms": created_ms,
                    "created_dt": datetime.fromtimestamp(created_ms / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S") if created_ms else "",
                    "duration_ms": 0, "skills": [],
                    "prompt_text": latest_user_prompt,
                    "session_title": session_title, "session_first_text": session_first_text,
                }
                session_data["turns"].append(turn)
                all_turns.append(turn)
                session_data["total_input"] += inp
                session_data["total_output"] += outp
                session_data["total_cache_read"] += turn["cache_read_tokens"]
                session_data["total_cache_create"] += turn["cache_create_tokens"]
                session_data["total_cost_cny"] += cost_cny
                session_data["models_used"].add(matched_name)
                if created_ms:
                    if session_data["first_created"] is None or created_ms < session_data["first_created"]:
                        session_data["first_created"] = created_ms
                    if session_data["last_created"] is None or created_ms > session_data["last_created"]:
                        session_data["last_created"] = created_ms

        if session_data["turns"]:
            _finalize_session(session_data)
            sessions.append(session_data)
    return sessions, all_turns, unmatched


# ── JoyCode 数据采集 ──────────────────────────────────
def collect_joycode(exact, aliases):
    task_history_dir = os.path.join(JOYCODE_TASKS_DIR, "task_history")
    if not os.path.isdir(task_history_dir):
        return [], [], set()

    files = glob.glob(os.path.join(task_history_dir, "*.json"))
    sessions = []
    all_turns = []
    unmatched = set()

    for fpath in sorted(files):
        with open(fpath, "r", encoding="utf-8") as fh:
            content = fh.read()
        try:
            d = json.loads(content)
        except json.JSONDecodeError:
            continue

        task_id = d.get("id", os.path.basename(fpath).replace(".json", ""))
        task_text = d.get("task", "")
        created_ms = d.get("ts", 0)
        tokens_in = d.get("tokensIn", 0)
        tokens_out = d.get("tokensOut", 0)
        cache_writes = d.get("cacheWrites", 0)
        cache_reads = d.get("cacheReads", 0)
        total_cost = d.get("totalCost", 0)
        mode_name = d.get("modeName", "")

        # Try to get model from conversation history
        model_id = "unknown"
        conv_path = os.path.join(JOYCODE_TASKS_DIR, "tasks", task_id, "api_conversation_history.json")
        if os.path.isfile(conv_path):
            try:
                with open(conv_path, "r", encoding="utf-8") as cf:
                    conv = json.loads(cf.read())
                if isinstance(conv, list):
                    for m in conv:
                        if isinstance(m, dict) and m.get("role") == "assistant":
                            model_id = m.get("_model_id", "unknown")
                            break
            except (json.JSONDecodeError, FileNotFoundError):
                pass

        mp = match_model(model_id, exact, aliases)
        if mp:
            cost_cny = calculate_turn_cost(mp, {
                "input_tokens": tokens_in, "output_tokens": tokens_out,
                "cache_read_input_tokens": cache_reads,
                "cache_creation_input_tokens": cache_writes,
            }, created_ms)
            matched_name = mp.get("display_name", model_id)
        else:
            cost_cny = 0.0
            matched_name = model_id
            unmatched.add(model_id)

        turn = {
            "tool": "JoyCode", "session_file_id": task_id,
            "model_id": model_id, "matched_name": matched_name,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cache_read_tokens": cache_reads, "cache_create_tokens": cache_writes,
            "cost_cny": cost_cny, "created_ms": created_ms,
            "created_dt": datetime.fromtimestamp(created_ms / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S") if created_ms else "",
            "duration_ms": 0, "skills": [],
            "prompt_text": task_text,
            "session_title": task_text[:80] if task_text else task_id,
            "session_first_text": task_text,
        }

        session_data = {
            "file_id": task_id, "tool": "JoyCode", "turns": [turn],
            "total_input": tokens_in, "total_output": tokens_out,
            "total_cache_read": cache_reads, "total_cache_create": cache_writes,
            "total_cost_cny": cost_cny, "models_used": {matched_name},
            "skills_used": set(), "first_text": task_text,
            "first_created": created_ms, "last_created": created_ms,
            "title": task_text[:80] if task_text else task_id, "date": None,
        }
        _finalize_session(session_data)
        sessions.append(session_data)
        all_turns.append(turn)

    return sessions, all_turns, unmatched


# ── Session 最终化 ────────────────────────────────────
def _finalize_session(s):
    if s["first_created"]:
        s["date"] = datetime.fromtimestamp(s["first_created"] / 1000, tz=TZ).strftime("%Y-%m-%d")
        s["first_created_dt"] = datetime.fromtimestamp(s["first_created"] / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M")
        s["last_created_dt"] = datetime.fromtimestamp(s["last_created"] / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M")
    s["total_cost_cny"] = round(s["total_cost_cny"], 4)
    s["models_used"] = sorted(s["models_used"])
    s["skills_used"] = sorted(s["skills_used"])
    s["turn_count"] = len(s["turns"])


# ── 子 Agent 合并 ────────────────────────────────────
def load_proma_parent_map():
    """构建子会话 -> 父会话的映射，以及父会话标题"""
    child_to_parent = {}
    parent_titles = {}
    try:
        with open(PROMA_SESSIONS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s in data.get("sessions", []):
            sid = s.get("id", "")
            parent = s.get("parentSessionId", "")
            title = s.get("title", "")
            if parent:
                child_to_parent[sid] = parent
            if title:
                parent_titles[sid] = title
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return child_to_parent, parent_titles


def merge_sub_agents(sessions, child_to_parent, parent_titles):
    """将子 Agent 会话的 turns 重定向到父会话并标记，只保留父会话"""
    merged_sessions = []
    for s in sessions:
        sid = s["file_id"]
        parent = child_to_parent.get(sid, "")
        if parent:
            # 子 Agent：把 turns 重定向到父会话并标记为子 Agent
            for t in s["turns"]:
                t["session_file_id"] = parent
                t["session_title"] = parent_titles.get(parent, t.get("session_title", ""))
                t["session_first_text"] = parent_titles.get(parent, t.get("session_first_text", ""))
                t["_is_sub_agent"] = True
            # 子 Agent turns 已重定向，不单独保留 session
        else:
            merged_sessions.append(s)
    return merged_sessions


# ── Prompt 合并 ──────────────────────────────────────
def merge_turns_by_prompt(turns):
    """将同一会话内相同 prompt 的多次 API 调用合并为一条"""
    groups = {}
    order = []
    for t in turns:
        key = (t["session_file_id"], t.get("prompt_text", ""))
        if key not in groups:
            groups[key] = {
                "tool": t["tool"],
                "session_file_id": t["session_file_id"],
                "model_id": t.get("matched_name", "unknown"),
                "matched_name": t.get("matched_name", "unknown"),
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_create_tokens": 0,
                "cost_cny": 0.0,
                "created_ms": t.get("created_ms", 0),
                "created_dt": t.get("created_dt", ""),
                "duration_ms": 0, "skills": t.get("skills", []),
                "prompt_text": t.get("prompt_text", ""),
                "session_title": t.get("session_title", ""),
                "session_first_text": t.get("session_first_text", ""),
                "api_calls": 0,
            }
            order.append(key)
        g = groups[key]
        g["input_tokens"] += t.get("input_tokens", 0)
        g["output_tokens"] += t.get("output_tokens", 0)
        g["cache_read_tokens"] += t.get("cache_read_tokens", 0)
        g["cache_create_tokens"] += t.get("cache_create_tokens", 0)
        g["cost_cny"] += t.get("cost_cny", 0)
        g["duration_ms"] += t.get("duration_ms", 0)
        g["api_calls"] += 1
        # 用最早时间
        if t.get("created_ms", 0) and (g["created_ms"] == 0 or t["created_ms"] < g["created_ms"]):
            g["created_ms"] = t["created_ms"]
            g["created_dt"] = t["created_dt"]
    
    # 保留插入顺序
    result = [groups[k] for k in order]
    for g in result:
        g["cost_cny"] = round(g["cost_cny"], 6)
    return result


# ── 统一采集 ──────────────────────────────────────────
def collect_all(pricing_data):
    exact, aliases = build_model_matcher(pricing_data)
    proma_titles = load_proma_session_titles()
    child_to_parent, parent_titles = load_proma_parent_map()

    all_sessions = []
    all_turns = []
    all_unmatched = set()

    # Proma
    if os.path.isdir(PROMA_SESSIONS_DIR):
        s, t, u = collect_proma(exact, aliases, proma_titles)
        all_sessions.extend(s)
        all_turns.extend(t)
        all_unmatched.update(u)

    # Claude Code
    if os.path.isdir(CLAUDE_PROJECTS_DIR):
        s, t, u = collect_claude_code(exact, aliases)
        all_sessions.extend(s)
        all_turns.extend(t)
        all_unmatched.update(u)

    # JoyCode
    s, t, u = collect_joycode(exact, aliases)
    all_sessions.extend(s)
    all_turns.extend(t)
    all_unmatched.update(u)

    # 合并子 Agent 到父会话
    all_sessions = merge_sub_agents(all_sessions, child_to_parent, parent_titles)
    all_turns = merge_sub_agents_turns(all_turns, child_to_parent, parent_titles)
    
    # 将子 Agent turns 的 token/费用合并到父会话最近的用户 prompt
    all_turns = absorb_sub_agent_turns(all_turns)
    
    # 合并相同 prompt 的多次调用
    all_turns = merge_turns_by_prompt(all_turns)
    
    # 重新计算会话聚合（子 Agent 数据已合并进来）
    all_sessions = recompute_sessions(all_sessions, all_turns)

    all_sessions.sort(key=lambda x: x.get("first_created", 0), reverse=True)
    all_turns.sort(key=lambda x: x.get("created_ms", 0), reverse=True)
    return all_sessions, all_turns, all_unmatched


def merge_sub_agents_turns(turns, child_to_parent, parent_titles):
    """将剩余未标记的子 Agent turns 重定向到父会话"""
    for t in turns:
        if t.get("_is_sub_agent"):
            continue  # 已被 merge_sub_agents 标记
        sid = t.get("session_file_id", "")
        parent = child_to_parent.get(sid, "")
        if parent:
            t["session_file_id"] = parent
            t["session_title"] = parent_titles.get(parent, t.get("session_title", ""))
            t["session_first_text"] = parent_titles.get(parent, t.get("session_first_text", ""))
            t["_is_sub_agent"] = True
        else:
            t["_is_sub_agent"] = False
    return turns


def absorb_sub_agent_turns(turns):
    """将子 Agent turns 的 token/费用合并到同会话中时间最近的前一条父 prompt，然后删除子 Agent 行"""
    # 按时间升序排列
    turns_sorted = sorted(turns, key=lambda x: x.get("created_ms", 0))
    
    parent_turns = [t for t in turns_sorted if not t.get("_is_sub_agent")]
    sub_turns = [t for t in turns_sorted if t.get("_is_sub_agent")]
    
    if not sub_turns:
        return turns
    
    # 为每个子 Agent turn 找同会话中最近的前一条父 turn
    absorbed = []
    orphans = []
    for st in sub_turns:
        sid = st["session_file_id"]
        ts = st.get("created_ms", 0)
        best = None
        for pt in parent_turns:
            if pt["session_file_id"] == sid and pt.get("created_ms", 0) <= ts:
                if best is None or pt["created_ms"] > best.get("created_ms", 0):
                    best = pt
        if best:
            # 合并到父 turn
            best["input_tokens"] += st["input_tokens"]
            best["output_tokens"] += st["output_tokens"]
            best["cache_read_tokens"] += st["cache_read_tokens"]
            best["cache_create_tokens"] += st["cache_create_tokens"]
            best["cost_cny"] += st["cost_cny"]
            best["duration_ms"] += st.get("duration_ms", 0)
            best.setdefault("api_calls", 1)
            best["api_calls"] = best.get("api_calls", 1) + 1
        else:
            # 没找到父 turn，保留但改名为 (子Agent调用)
            st["prompt_text"] = "(子Agent调用)"
            orphans.append(st)
    
    # 返回父 turns + 孤儿子 Agent turns
    result = parent_turns + orphans
    return result


def recompute_sessions(sessions, turns):
    """根据合并后的 turns 重新计算会话聚合数据"""
    turns_by_session = defaultdict(list)
    for t in turns:
        turns_by_session[t["session_file_id"]].append(t)
    
    result = []
    for s in sessions:
        sid = s["file_id"]
        sturns = turns_by_session.get(sid, [])
        if not sturns:
            continue  # 没有数据的会话跳过
        s["turns"] = sturns
        s["total_input"] = sum(t["input_tokens"] for t in sturns)
        s["total_output"] = sum(t["output_tokens"] for t in sturns)
        s["total_cache_read"] = sum(t["cache_read_tokens"] for t in sturns)
        s["total_cache_create"] = sum(t["cache_create_tokens"] for t in sturns)
        s["total_cost_cny"] = round(sum(t["cost_cny"] for t in sturns), 4)
        s["models_used"] = sorted(set(t.get("matched_name", "") for t in sturns))
        s["skills_used"] = sorted(set(sk for t in sturns for sk in t.get("skills", [])))
        s["turn_count"] = len(sturns)
        times = [t["created_ms"] for t in sturns if t.get("created_ms")]
        if times:
            s["first_created"] = min(times)
            s["last_created"] = max(times)
            s["date"] = datetime.fromtimestamp(s["first_created"] / 1000, tz=TZ).strftime("%Y-%m-%d")
            s["first_created_dt"] = datetime.fromtimestamp(s["first_created"] / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M")
            s["last_created_dt"] = datetime.fromtimestamp(s["last_created"] / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M")
        result.append(s)
    return result


# ── HTML 生成 ─────────────────────────────────────────
def esc(text):
    if not text:
        return ""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def generate_html(sessions, turns, unmatched_models, pricing_data):
    now = datetime.now(TZ)
    gen_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # Tool colors
    tool_colors = {"Proma": "#4f9cf9", "Claude Code": "#fb923c", "JoyCode": "#34d399"}

    # 准备数据
    turns_js = []
    for t in turns:
        session_name = t.get("session_title") or t.get("session_first_text", "")[:80] or t["session_file_id"][:8]
        turns_js.append({
            "ts": t["created_ms"],
            "dt": t["created_dt"],
            "tool": t["tool"],
            "model": t["matched_name"],
            "input": t["input_tokens"],
            "output": t["output_tokens"],
            "cache_r": t["cache_read_tokens"],
            "cache_w": t["cache_create_tokens"],
            "total": t["input_tokens"] + t["output_tokens"] + t["cache_read_tokens"] + t["cache_create_tokens"],
            "cost": round(t["cost_cny"], 4),
            "session": t["session_file_id"][:8],
            "session_name": session_name[:80],
            "session_name_full": ((t.get("session_title") or "") + "\n首条消息: " + (t.get("session_first_text") or "")[:300]).strip(),
            "prompt": (t.get("prompt_text") or "(无文本)")[:120],
            "prompt_full": (t.get("prompt_text") or "")[:500],
            "api_calls": t.get("api_calls", 1),
            "skills": ",".join(t.get("skills", [])) if t.get("skills") else "",
        })

    sessions_js = []
    for s in sessions:
        name = s.get("title") or s["first_text"][:80] if s["first_text"] else s["file_id"][:12]
        sessions_js.append({
            "ts": s.get("first_created", 0),
            "date": s["date"] or "",
            "first_dt": s.get("first_created_dt", ""),
            "tool": s["tool"],
            "models": ",".join(s["models_used"]),
            "input": s["total_input"],
            "output": s["total_output"],
            "cache_r": s["total_cache_read"],
            "total": s["total_input"] + s["total_output"] + s["total_cache_read"] + s["total_cache_create"],
            "cost": s["total_cost_cny"],
            "turns": s["turn_count"],
            "name": name[:80] if name else s["file_id"][:12],
            "name_full": ((s.get("title") or "") + "\n首条消息: " + (s["first_text"] or "")[:300]).strip(),
        })

    pricing_js = []
    for m in pricing_data.get("models", []):
        pricing_js.append({
            "name": m["display_name"], "id": m["id"],
            "commercial": m.get("commercial", False),
            "tasks": m.get("tasks", ""),
            "pricing_type": m.get("pricing_type", "flat"),
            "input": m.get("input"), "cache_read": m.get("cache_read"), "output": m.get("output"),
            "low_tier": m.get("low_tier"), "high_tier": m.get("high_tier"),
            "peak": m.get("peak"), "offpeak": m.get("offpeak"),
            "tier_threshold": m.get("tier_threshold"), "peak_hours": m.get("peak_hours"),
        })

    all_data = json.dumps({"turns": turns_js, "sessions": sessions_js, "pricing": pricing_js}, ensure_ascii=False)

    total_sessions = len(sessions)
    total_turns = len(turns)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- 自动刷新由 JS 控制，不使用 meta refresh 以保留用户当前页面状态 -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
<title>AI Coding 平台 Token 用量看板</title>
<style>
:root {{ --bg:#0f1117;--card-bg:#1a1d27;--card-border:#2a2d3a;--text:#e4e6eb;--text-dim:#8b8fa3;--accent:#4f9cf9;--green:#34d399;--orange:#fb923c;--red:#f87171;--purple:#a78bfa;--yellow:#fbbf24;--pink:#f472b6; }}
* {{ margin:0;padding:0;box-sizing:border-box; }}
body {{ font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:20px;min-height:100vh; }}
h1 {{ font-size:24px;margin-bottom:4px; }}
.subtitle {{ color:var(--text-dim);font-size:13px;margin-bottom:16px; }}
.time-filter {{ display:flex;gap:8px;margin-bottom:20px;align-items:center;flex-wrap:wrap; }}
.time-filter-label {{ font-size:13px;color:var(--text-dim);margin-right:4px; }}
.time-btn {{ padding:6px 14px;border:1px solid var(--card-border);background:var(--card-bg);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:13px;transition:all 0.2s; }}
.time-btn:hover {{ border-color:var(--accent);color:var(--text); }}
.time-btn.active {{ background:var(--accent);color:#fff;border-color:var(--accent); }}
.overview-grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px; }}
.stat-card {{ background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;padding:18px;position:relative;overflow:hidden; }}
.stat-card::before {{ content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:12px 12px 0 0; }}
.stat-card.c1::before {{ background:var(--accent); }} .stat-card.c2::before {{ background:var(--green); }}
.stat-card.c3::before {{ background:var(--orange); }} .stat-card.c4::before {{ background:var(--purple); }}
.stat-card.c5::before {{ background:var(--yellow); }} .stat-card.c6::before {{ background:var(--pink); }}
.stat-label {{ font-size:11px;color:var(--text-dim);margin-bottom:6px;text-transform:uppercase;letter-spacing:1px; }}
.stat-value {{ font-size:26px;font-weight:700; }}
.stat-sub {{ font-size:11px;color:var(--text-dim);margin-top:4px;line-height:1.4; }}
.tabs {{ display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap; }}
.tab-btn {{ padding:8px 16px;border:1px solid var(--card-border);background:var(--card-bg);color:var(--text-dim);border-radius:8px;cursor:pointer;font-size:13px;transition:all 0.2s; }}
.tab-btn:hover {{ border-color:var(--accent);color:var(--text); }}
.tab-btn.active {{ background:var(--accent);color:#fff;border-color:var(--accent); }}
.tab-content {{ display:none; }} .tab-content.active {{ display:block; }}
.table-container {{ background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;overflow:hidden;margin-bottom:24px; }}
.table-header {{ padding:14px 18px;border-bottom:1px solid var(--card-border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px; }}
.table-header h3 {{ font-size:15px; }}
.search-box {{ background:var(--bg);border:1px solid var(--card-border);border-radius:6px;padding:6px 12px;color:var(--text);font-size:13px;width:200px; }}
.search-box:focus {{ outline:none;border-color:var(--accent); }}
.table-wrap {{ overflow-x:auto;max-height:600px;overflow-y:auto; }}
table {{ width:100%;border-collapse:collapse; }}
th {{ padding:10px 12px;text-align:left;font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid var(--card-border);cursor:pointer;white-space:nowrap;position:sticky;top:0;background:var(--card-bg);z-index:1; }}
th:hover {{ color:var(--accent); }}
td {{ padding:8px 12px;font-size:13px;border-bottom:1px solid var(--card-border); }}
tr:hover {{ background:rgba(79,156,249,0.05); }}
.num {{ text-align:right;font-variant-numeric:tabular-nums; }}
.text-dim {{ color:var(--text-dim); }} .text-green {{ color:var(--green); }} .text-orange {{ color:var(--orange); }}
.text-red {{ color:var(--red); }} .text-yellow {{ color:var(--yellow); }} .text-purple {{ color:var(--purple); }}
.prompt-cell {{ max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help;color:var(--text-dim); }}
.session-cell {{ max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:help; }}
.tool-badge {{ display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;white-space:nowrap; }}
.tool-Proma {{ background:rgba(79,156,249,0.15);color:#4f9cf9; }}
.tool-ClaudeCode {{ background:rgba(251,146,60,0.15);color:#fb923c; }}
.tool-JoyCode {{ background:rgba(52,211,153,0.15);color:#34d399; }}
.chart-container {{ background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;padding:20px;margin-bottom:24px; }}
.chart-container h3 {{ font-size:15px;margin-bottom:16px; }}
.chart-scroll {{ overflow-x:auto;overflow-y:visible;padding-top:30px; }}
.bar-chart {{ display:flex;align-items:flex-end;gap:3px;height:180px;padding:10px 0;min-width:100%; }}
.bar {{ flex:1;min-width:24px;max-width:60px;border-radius:4px 4px 0 0;position:relative;cursor:pointer;transition:opacity 0.2s; }}
.bar-label {{ position:absolute;bottom:100%;left:50%;transform:translateX(-50%);font-size:11px;font-weight:600;white-space:nowrap;padding-bottom:4px; }}
.bar-tooltip {{ position:absolute;bottom:100%;left:50%;transform:translateX(-50%);background:var(--bg);border:1px solid var(--card-border);border-radius:6px;padding:6px 10px;font-size:11px;white-space:nowrap;display:none;z-index:999;pointer-events:none;margin-top:22px; }}
.bar:hover .bar-tooltip {{ display:block; }}
.chart-labels {{ display:flex;gap:3px;margin-top:6px;min-width:100%; }}
.chart-label {{ flex:1;min-width:24px;max-width:60px;text-align:center;font-size:9px;color:var(--text-dim); }}
.price-tag {{ display:inline-block;padding:2px 6px;border-radius:4px;font-size:11px; }}
.price-input {{ background:rgba(79,156,249,0.1);color:var(--accent); }}
.price-output {{ background:rgba(251,146,60,0.1);color:var(--orange); }}
.price-cache {{ background:rgba(52,211,153,0.1);color:var(--green); }}
.unmatched-warning {{ background:rgba(248,113,113,0.1);border:1px solid var(--red);border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px; }}
.unmatched-warning strong {{ color:var(--red); }}
@media (max-width:768px) {{ .overview-grid {{ grid-template-columns:repeat(2,1fr); }} table {{ font-size:11px; }} th,td {{ padding:6px 8px; }} .prompt-cell {{ max-width:120px; }} }}
</style>
</head>
<body>
<h1>📊 AI Coding 平台 Token 用量看板</h1>
<div class="subtitle">生成时间: {gen_time} | 共 {total_sessions} 个会话 / {total_turns} 次调用</div>
{'<div class="unmatched-warning">⚠️ <strong>以下模型未匹配到价格</strong>，费用按 ¥0 计算: ' + ', '.join(unmatched_models) + '</div>' if unmatched_models else ''}
<div class="time-filter">
  <span class="time-filter-label">📅 时间范围:</span>
  <button class="time-btn" onclick="setTimeRange('today',this)">今天</button>
  <button class="time-btn active" onclick="setTimeRange('week',this)">本周</button>
  <button class="time-btn" onclick="setTimeRange('month',this)">本月</button>
  <button class="time-btn" onclick="setTimeRange('all',this)">全部</button>
  <span class="time-filter-label" id="range-label" style="margin-left:8px;"></span>
</div>
<div class="overview-grid" id="overview-cards"></div>
<div class="chart-container"><h3>📈 每日费用趋势 <span id="chart-total" style="font-size:14px;font-weight:400;color:var(--green);"></span></h3><div class="chart-scroll"><div class="bar-chart" id="date-chart" style="justify-content:center;"></div><div class="chart-labels" id="date-labels" style="justify-content:center;"></div></div></div>
<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('turns',this)">每次任务</button>
  <button class="tab-btn" onclick="switchTab('sessions',this)">按会话统计</button>
  <button class="tab-btn" onclick="switchTab('models',this)">按模型统计</button>
  <button class="tab-btn" onclick="switchTab('pricing',this)">模型价格表</button>
</div>

<!-- 每次任务 -->
<div class="tab-content active" id="tab-turns">
  <div class="table-container"><div class="table-header"><h3>🤖 每次任务用量明细</h3><input class="search-box" type="text" placeholder="搜索模型/内容..." oninput="filterTable('turns-tbody',this.value)"></div>
  <div class="table-wrap"><table><thead><tr>
    <th onclick="sortTable('turns-tbody',0)">时间</th><th>工具</th><th onclick="sortTable('turns-tbody',2)">模型</th>
    <th>所属会话</th><th>用户 Prompt</th>
    <th onclick="sortTable('turns-tbody',5)" class="num">输入</th><th onclick="sortTable('turns-tbody',6)" class="num">输出</th>
    <th onclick="sortTable('turns-tbody',7)" class="num">缓存读</th><th onclick="sortTable('turns-tbody',8)" class="num">总Tokens</th>
    <th onclick="sortTable('turns-tbody',9)" class="num">费用(¥)</th><th onclick="sortTable('turns-tbody',10)" class="num">调用数</th>
  </tr></thead><tbody id="turns-tbody"></tbody></table></div></div>
</div>

<!-- 按会话 -->
<div class="tab-content" id="tab-sessions">
  <div class="table-container"><div class="table-header"><h3>💬 会话用量</h3><input class="search-box" type="text" placeholder="搜索会话..." oninput="filterTable('sessions-tbody',this.value)"></div>
  <div class="table-wrap"><table><thead><tr>
    <th onclick="sortTable('sessions-tbody',0)">日期</th><th>工具</th><th>会话名称</th>
    <th onclick="sortTable('sessions-tbody',3)">模型</th><th onclick="sortTable('sessions-tbody',4)" class="num">轮次</th>
    <th onclick="sortTable('sessions-tbody',5)" class="num">输入</th><th onclick="sortTable('sessions-tbody',6)" class="num">输出</th>
    <th onclick="sortTable('sessions-tbody',7)" class="num">缓存读</th><th onclick="sortTable('sessions-tbody',8)" class="num">总Tokens</th>
    <th onclick="sortTable('sessions-tbody',9)" class="num">费用(¥)</th>
  </tr></thead><tbody id="sessions-tbody"></tbody></table></div></div>
</div>

<!-- 按模型 -->
<div class="tab-content" id="tab-models">
  <div class="table-container"><div class="table-header"><h3>🖥️ 模型用量与费用</h3></div>
  <div class="table-wrap"><table><thead><tr>
    <th onclick="sortTable('models-tbody',0)">模型</th><th onclick="sortTable('models-tbody',1)" class="num">调用次数</th>
    <th onclick="sortTable('models-tbody',2)" class="num">会话数</th><th onclick="sortTable('models-tbody',3)" class="num">输入Tokens</th>
    <th onclick="sortTable('models-tbody',4)" class="num">输出Tokens</th><th onclick="sortTable('models-tbody',5)" class="num">缓存读</th>
    <th onclick="sortTable('models-tbody',6)" class="num">总Tokens</th><th onclick="sortTable('models-tbody',7)" class="num">费用(¥)</th>
    <th onclick="sortTable('models-tbody',8)" class="num">¥/次</th>
  </tr></thead><tbody id="models-tbody"></tbody></table></div></div>
</div>

<!-- 价格表 -->
<div class="tab-content" id="tab-pricing">
  <div class="table-container"><div class="table-header"><h3>💰 模型刊例价 (元 / 1M Tokens)</h3><span class="text-dim">原始元/千Tokens ×1000 换算</span></div>
  <div class="table-wrap"><table><thead><tr>
    <th>模型</th><th>可商用</th><th>任务场景</th><th>计价方式</th><th>输入</th><th>缓存读取</th><th>输出</th>
  </tr></thead><tbody id="pricing-tbody"></tbody></table></div></div>
  <div style="font-size:12px;color:var(--text-dim);padding:12px 0;">💡 分层计价按每轮输入上下文长度判定；峰谷计价按调用时间(8-22点高峰)判定。分层/峰谷显示为 低/高 或 谷/峰。</div>
</div>

<script>
const ALL_DATA = {all_data};
let currentRange = localStorage.getItem('usage_range') || 'week';
let currentTab = localStorage.getItem('usage_tab') || 'turns';

function getRangeTs(range) {{
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const dayOfWeek = now.getDay() || 7;
  const mondayStart = todayStart - (dayOfWeek - 1) * 86400000;
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  switch(range) {{ case 'today': return todayStart; case 'week': return mondayStart; case 'month': return monthStart; case 'all': return 0; }}
}}
function setTimeRange(range, btn) {{
  currentRange = range;
  localStorage.setItem('usage_range', range);
  document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderAll();
}}
function fmt(n) {{ return n.toLocaleString('zh-CN'); }}
function fmt2(n) {{ return n.toFixed(2).replace(/\\B(?=(\\d{{3}})+(?:\\.\\d+)?$)/g, ','); }}
function fmt4(n) {{ return n.toFixed(2); }}
function esc(s) {{ if(!s) return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }}
function toolBadge(tool) {{ const cls = tool.replace(/\\s/g,''); return `<span class="tool-badge tool-${{cls}}">${{tool}}</span>`; }}

function renderOverview() {{
  const startTs = getRangeTs(currentRange);
  const turns = ALL_DATA.turns.filter(t => t.ts >= startTs);
  const totalInput = turns.reduce((s,t) => s+t.input, 0);
  const totalOutput = turns.reduce((s,t) => s+t.output, 0);
  const totalCR = turns.reduce((s,t) => s+t.cache_r, 0);
  const totalTokens = totalInput + totalOutput + totalCR + turns.reduce((s,t)=>s+t.cache_w,0);
  const totalCost = turns.reduce((s,t) => s+t.cost, 0);
  const sessionIds = new Set(turns.map(t => t.session));
  const rangeLabels = {{ today:'今天', week:'本周', month:'本月', all:'全部' }};
  const label = startTs > 0 ? new Date(startTs).toLocaleDateString('zh-CN') : '全部';
  document.getElementById('range-label').textContent = `(${{rangeLabels[currentRange]}}: ${{label}} ~ 现在)`;
  const avgPerTurn = turns.length > 0 ? totalCost / turns.length : 0;
  const tools = new Set(turns.map(t => t.tool));
  const models = new Set(turns.map(t => t.model));
  document.getElementById('overview-cards').innerHTML = `
    <div class="stat-card c1"><div class="stat-label">总 Token</div><div class="stat-value">${{fmt(totalTokens)}}</div><div class="stat-sub">输入 ${{fmt(totalInput)}} · 输出 ${{fmt(totalOutput)}} · 缓存读 ${{fmt(totalCR)}}</div></div>
    <div class="stat-card c2"><div class="stat-label">总费用 (CNY)</div><div class="stat-value text-green">¥${{fmt2(totalCost)}}</div><div class="stat-sub">${{turns.length}} 次调用 · ${{sessionIds.size}} 个会话</div></div>
    <div class="stat-card c3"><div class="stat-label">平均每次费用</div><div class="stat-value text-orange">¥${{fmt4(avgPerTurn)}}</div><div class="stat-sub">¥${{(totalCost/Math.max(sessionIds.size,1)).toFixed(2)}} / 会话</div></div>
    <div class="stat-card c4"><div class="stat-label">调用次数</div><div class="stat-value text-purple">${{turns.length}}</div><div class="stat-sub">${{sessionIds.size}} 个会话</div></div>
    <div class="stat-card c5"><div class="stat-label">工具</div><div class="stat-value text-yellow">${{tools.size}}</div><div class="stat-sub">${{Array.from(tools).join(', ')}}</div></div>
    <div class="stat-card c6"><div class="stat-label">使用模型</div><div class="stat-value" style="color:var(--pink)">${{models.size}}</div><div class="stat-sub">${{Array.from(models).slice(0,3).join(', ')}}${{models.size>3?'...':''}}</div></div>
  `;
}}

function renderChart() {{
  const startTs = getRangeTs(currentRange);
  const turns = ALL_DATA.turns.filter(t => t.ts >= startTs);
  const byDate = {{}};
  turns.forEach(t => {{ const d = t.dt.slice(0,10); if(!byDate[d]) byDate[d] = {{cost:0,tokens:0,turns:0}}; byDate[d].cost+=t.cost; byDate[d].tokens+=t.total; byDate[d].turns+=1; }});
  const dates = Object.keys(byDate).sort();
  if(!dates.length) {{ document.getElementById('chart-total').textContent=''; document.getElementById('date-chart').innerHTML='<div style="color:var(--text-dim);padding:40px;text-align:center;">该时间范围内暂无数据</div>'; document.getElementById('date-labels').innerHTML=''; return; }}
  document.getElementById('chart-total').textContent = `总计 ¥${{fmt2(turns.reduce((s,t)=>s+t.cost,0))}}`;
  const maxCost = Math.max(...dates.map(d=>byDate[d].cost))||1;
  const colors=['var(--accent)','var(--purple)','var(--green)','var(--orange)'];
  document.getElementById('date-chart').innerHTML = dates.map((d,i) => {{ const v=byDate[d]; const h=(v.cost/maxCost)*100; return `<div class="bar" style="height:${{Math.max(h,2)}}%;background:linear-gradient(180deg,${{colors[i%colors.length]}},var(--purple));"><span class="bar-label" style="color:${{colors[i%colors.length]}}">¥${{v.cost.toFixed(0)}}</span><div class="bar-tooltip">${{d}}<br>¥${{v.cost.toFixed(2)}}<br>${{fmt(v.tokens)}} tokens<br>${{v.turns}} 次</div></div>`; }}).join('');
  document.getElementById('date-labels').innerHTML = dates.map(d=>`<div class="chart-label">${{d.slice(5)}}</div>`).join('');
}}

function renderTurns() {{
  const startTs = getRangeTs(currentRange);
  const turns = ALL_DATA.turns.filter(t => t.ts >= startTs);
  document.getElementById('turns-tbody').innerHTML = turns.map(t => {{
    const pe = esc(t.prompt_full||t.prompt||'(无文本)');
    const se = esc(t.session_name_full||t.session_name);
    return `<tr data-ts="${{t.ts}}"><td class="text-dim" style="white-space:nowrap;">${{t.dt}}</td><td>${{toolBadge(t.tool)}}</td><td><strong>${{esc(t.model)}}</strong></td><td class="session-cell" title="${{se}}">${{esc(t.session_name)}}</td><td class="prompt-cell" title="${{pe}}">${{esc(t.prompt)}}</td><td class="num">${{fmt(t.input)}}</td><td class="num">${{fmt(t.output)}}</td><td class="num">${{fmt(t.cache_r)}}</td><td class="num">${{fmt(t.total)}}</td><td class="num text-green">¥${{fmt4(t.cost)}}</td><td class="num text-dim">${{t.api_calls||1}}</td></tr>`;
  }}).join('') || '<tr><td colspan="11" style="text-align:center;color:var(--text-dim);padding:30px;">该时间范围内暂无数据</td></tr>';
}}

function renderSessions() {{
  const startTs = getRangeTs(currentRange);
  // 直接过滤 sessions 列表
  const filtered = ALL_DATA.sessions.filter(s => s.ts >= startTs);
  document.getElementById('sessions-tbody').innerHTML = filtered.map(s => {{
    const ne = esc(s.name_full||s.name);
    return `<tr data-ts="${{s.ts}}"><td class="text-dim" style="white-space:nowrap;">${{s.first_dt}}</td><td>${{toolBadge(s.tool)}}</td><td class="session-cell" title="${{ne}}"><strong>${{esc(s.name)}}</strong></td><td class="text-dim">${{esc(s.models)}}</td><td class="num">${{s.turns}}</td><td class="num">${{fmt(s.input)}}</td><td class="num">${{fmt(s.output)}}</td><td class="num">${{fmt(s.cache_r)}}</td><td class="num">${{fmt(s.total)}}</td><td class="num text-green">¥${{fmt4(s.cost)}}</td></tr>`;
  }}).join('') || '<tr><td colspan="10" style="text-align:center;color:var(--text-dim);padding:30px;">该时间范围内暂无数据</td></tr>';
}}

function renderModels() {{
  const startTs = getRangeTs(currentRange);
  const turns = ALL_DATA.turns.filter(t => t.ts >= startTs);
  const byModel = {{}};
  turns.forEach(t => {{ if(!byModel[t.model]) byModel[t.model] = {{input:0,output:0,cache_r:0,total:0,cost:0,turns:0,sessions:new Set()}}; const m=byModel[t.model]; m.input+=t.input;m.output+=t.output;m.cache_r+=t.cache_r;m.total+=t.total;m.cost+=t.cost;m.turns+=1;m.sessions.add(t.session); }});
  const arr = Object.entries(byModel).map(([name,v])=>({{name,...v,sc:v.sessions.size}})).sort((a,b)=>b.cost-a.cost);
  document.getElementById('models-tbody').innerHTML = arr.map(m => {{
    const cpt = m.turns>0?m.cost/m.turns:0;
    return `<tr><td><strong>${{esc(m.name)}}</strong></td><td class="num">${{m.turns}}</td><td class="num">${{m.sc}}</td><td class="num">${{fmt(m.input)}}</td><td class="num">${{fmt(m.output)}}</td><td class="num">${{fmt(m.cache_r)}}</td><td class="num">${{fmt(m.total)}}</td><td class="num text-green">¥${{fmt2(m.cost)}}</td><td class="num">¥${{fmt4(cpt)}}</td></tr>`;
  }}).join('') || '<tr><td colspan="9" style="text-align:center;color:var(--text-dim);padding:30px;">该时间范围内暂无数据</td></tr>';
}}

function renderPricing() {{
  document.getElementById('pricing-tbody').innerHTML = ALL_DATA.pricing.map(m => {{
    const com = m.commercial?'✅':'❌';
    let pt,ip,cr,op;
    if(m.pricing_type==='flat') {{ pt='统一价'; ip=`<span class="price-tag price-input">¥${{m.input}}/M</span>`; cr=m.cache_read!=null?`<span class="price-tag price-cache">¥${{m.cache_read}}/M</span>`:'<span class="text-dim">-</span>'; op=`<span class="price-tag price-output">¥${{m.output}}/M</span>`; }}
    else if(m.pricing_type==='tiered') {{ const tk=(m.tier_threshold||0)/1000; pt=`分层(≤${{tk}}K/>${{tk}}K)`; ip=`<span class="price-tag price-input">¥${{m.low_tier.input}}/${{m.high_tier.input}}</span>`; cr=m.low_tier.cache_read!=null?`<span class="price-tag price-cache">¥${{m.low_tier.cache_read}}/${{m.high_tier.cache_read}}</span>`:'<span class="text-dim">-</span>'; op=`<span class="price-tag price-output">¥${{m.low_tier.output}}/${{m.high_tier.output}}</span>`; }}
    else if(m.pricing_type==='peak_offpeak') {{ pt=`峰谷(${{m.peak_hours||'8-22'}}h峰)`; ip=`<span class="price-tag price-input">¥${{m.offpeak.input}}/${{m.peak.input}}</span>`; cr=m.peak.cache_read!=null?`<span class="price-tag price-cache">¥${{m.offpeak.cache_read}}/${{m.peak.cache_read}}</span>`:'<span class="text-dim">-</span>'; op=`<span class="price-tag price-output">¥${{m.offpeak.output}}/${{m.peak.output}}</span>`; }}
    else {{ pt='未知';ip=cr=op='<span class="text-dim">-</span>'; }}
    return `<tr><td><strong>${{esc(m.name)}}</strong><br><span class="text-dim" style="font-size:11px">${{esc(m.id)}}</span></td><td>${{com}}</td><td class="text-dim" style="font-size:12px">${{esc(m.tasks)}}</td><td>${{pt}}</td><td>${{ip}}</td><td>${{cr}}</td><td>${{op}}</td></tr>`;
  }}).join('');
}}

function renderAll() {{ renderOverview(); renderChart(); renderTurns(); renderSessions(); renderModels(); renderPricing(); }}
function switchTab(tabId, btn) {{ document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active')); document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active')); btn.classList.add('active'); document.getElementById('tab-'+tabId).classList.add('active'); localStorage.setItem('usage_tab', tabId); }}
function filterTable(tbodyId, query) {{ const tbody=document.getElementById(tbodyId); const q=query.toLowerCase(); tbody.querySelectorAll('tr').forEach(tr=>{{ tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none'; }}); }}
let sortStates={{}};
function sortTable(tbodyId, colIdx) {{ const tbody=document.getElementById(tbodyId); if(!tbody) return; const rows=Array.from(tbody.querySelectorAll('tr')); const key=tbodyId+'-'+colIdx; const asc=sortStates[key]=!sortStates[key]; rows.sort((a,b)=>{{ let va=a.children[colIdx].textContent.replace(/[,¥$%]/g,'').trim(); let vb=b.children[colIdx].textContent.replace(/[,¥$%]/g,'').trim(); const na=parseFloat(va),nb=parseFloat(vb); if(!isNaN(na)&&!isNaN(nb)) return asc?na-nb:nb-na; return asc?va.localeCompare(vb):vb.localeCompare(va); }}); rows.forEach(r=>tbody.appendChild(r)); }}
// 恢复用户上次的 Tab 和时间范围
(function() {
  document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
  const rangeMap = {today:0, week:1, month:2, all:3};
  const btnIdx = rangeMap[currentRange];
  const rangeBtns = document.querySelectorAll('.time-btn');
  if (btnIdx != null && rangeBtns[btnIdx]) rangeBtns[btnIdx].classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  const tabMap = {turns:0, sessions:1, models:2, pricing:3};
  const tabIdx = tabMap[currentTab] != null ? tabMap[currentTab] : 0;
  const tabBtns = document.querySelectorAll('.tab-btn');
  if (tabIdx != null && tabBtns[tabIdx]) tabBtns[tabIdx].classList.add('active');
  const tabEl = document.getElementById('tab-' + currentTab) || document.getElementById('tab-turns');
  if (tabEl) tabEl.classList.add('active');
})();

renderAll();

// JS 静默刷新：每 30 秒重新加载页面，状态通过 localStorage 保留
setTimeout(function() { location.reload(); }, 30000);
</script>
</body></html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="AI Coding 平台 Token 用量监控看板")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT)
    parser.add_argument("--pricing", "-p", default=DEFAULT_PRICING_FILE)
    args = parser.parse_args()

    with open(args.pricing, "r", encoding="utf-8") as f:
        pricing_data = json.load(f)

    print("扫描数据源...")
    if os.path.isdir(PROMA_SESSIONS_DIR):
        print(f"  Proma: {PROMA_SESSIONS_DIR}")
    if os.path.isdir(CLAUDE_PROJECTS_DIR):
        print(f"  Claude Code: {CLAUDE_PROJECTS_DIR}")
    joycode_dir = os.path.join(JOYCODE_TASKS_DIR, "task_history")
    if os.path.isdir(joycode_dir):
        print(f"  JoyCode: {joycode_dir}")

    sessions, turns, unmatched = collect_all(pricing_data)
    print(f"采集完成: {len(sessions)} 个会话, {len(turns)} 次调用")
    if unmatched:
        print(f"未匹配价格的模型: {', '.join(unmatched)}")

    # Tool breakdown
    tool_counts = defaultdict(int)
    for s in sessions:
        tool_counts[s["tool"]] += 1
    for tool, count in sorted(tool_counts.items()):
        print(f"  {tool}: {count} 个会话")

    print("生成 HTML 看板...")
    html = generate_html(sessions, turns, unmatched, pricing_data)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"看板已生成: {args.output}")

    # Summary
    total_cost = sum(t["cost_cny"] for t in turns)
    total_tokens = sum(t["input_tokens"] + t["output_tokens"] + t["cache_read_tokens"] + t["cache_create_tokens"] for t in turns)
    now_dt = datetime.now(TZ)
    monday = now_dt - timedelta(days=now_dt.weekday())
    monday_ts = int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    week_turns = [t for t in turns if t["created_ms"] and t["created_ms"] >= monday_ts]
    week_cost = sum(t["cost_cny"] for t in week_turns)

    print("\n" + "=" * 60)
    print("  用量摘要")
    print("=" * 60)
    print(f"  会话数: {len(sessions)}  |  API 调用: {len(turns)}")
    print(f"  总 Tokens: {total_tokens:,}")
    print(f"  总费用 (CNY): ¥{total_cost:,.2f}")
    print(f"  本周费用: ¥{week_cost:,.2f} ({len(week_turns)} 次调用)")
    if unmatched:
        print(f"\n  未匹配价格的模型: {', '.join(unmatched)}")


if __name__ == "__main__":
    main()
