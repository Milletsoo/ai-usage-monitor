#!/usr/bin/env python
"""
价格表自动抓取工具
从 JoySpace Sheets 导出模型价格表，解析并生成 pricing.json

用法:
    python fetch_pricing.py                    # 抓取并更新 pricing.json
    python fetch_pricing.py --dry-run          # 只解析不写入
    python fetch_pricing.py --xlsx PATH        # 使用本地 xlsx 文件（跳过网络抓取）
"""
import json
import os
import re
import sys
import subprocess
import argparse
from datetime import datetime, timezone, timedelta

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
PRICING_FILE = os.path.join(SKILL_DIR, "pricing.json")

# JoySpace 价格表
SHEET_ID = "QTwdCS2v14hHRdQ53lbj"
SHEET_URL = f"https://joyspace.jd.com/sheets/{SHEET_ID}"

# JoySpace 导出脚本路径（复用 joyspace-create-doc-win skill）
def _find_joyspace_export():
    """动态查找 JoySpace 导出脚本"""
    # 1. Proma workspace skills
    ws_base = os.path.join(os.path.expanduser("~"), ".proma", "agent-workspaces")
    if os.path.isdir(ws_base):
        for ws_name in os.listdir(ws_base):
            candidate = os.path.join(ws_base, ws_name, "skills", "joyspace-create-doc-win", "scripts", "export_sheet_native.py")
            if os.path.isfile(candidate):
                return candidate
    # 2. Direct skills path
    candidate = os.path.expanduser("~/.agents/skills/joyspace-create-doc-win/scripts/export_sheet_native.py")
    if os.path.isfile(candidate):
        return candidate
    return None

JOYSPACE_EXPORT = _find_joyspace_export()


def export_xlsx(output_path):
    """调用 JoySpace API 导出 xlsx"""
    if not os.path.isfile(JOYSPACE_EXPORT):
        print(f"  JoySpace 导出脚本不存在: {JOYSPACE_EXPORT}")
        return None

    # 获取 token
    token_script = None
    ws_base = os.path.join(os.path.expanduser("~"), ".proma", "agent-workspaces")
    if os.path.isdir(ws_base):
        for ws_name in os.listdir(ws_base):
            candidate = os.path.join(ws_base, ws_name, "skills", "jd-token-fetcher", "scripts", "get_tokens_headless.py")
            if os.path.isfile(candidate):
                token_script = candidate
                break
    if not token_script:
        token_script = os.path.expanduser("~/.agents/skills/jd-token-fetcher/scripts/get_tokens_headless.py")
    if not os.path.isfile(token_script):
        token_script = None
    env = os.environ.copy()
    if os.path.isfile(token_script):
        try:
            result = subprocess.run(
                [sys.executable, token_script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30
            )
            for line in result.stdout.split("\n"):
                if line.startswith("SSO_TOKEN="):
                    env["SSO_TOKEN"] = line.split("=", 1)[1].strip()
                elif line.startswith("ME_TOKEN="):
                    env["ME_TOKEN"] = line.split("=", 1)[1].strip()
        except Exception:
            pass

    if not env.get("SSO_TOKEN"):
        print("  未获取到 SSO_TOKEN，跳过价格表抓取")
        return None

    xlsx_path = output_path.replace(".csv", ".xlsx")
    result = subprocess.run(
        [sys.executable, JOYSPACE_EXPORT, SHEET_ID, "--output", output_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=env
    )

    # export_sheet_native.py 输出的是 xlsx 格式但可能用 .csv 后缀
    if os.path.isfile(output_path):
        return output_path
    if os.path.isfile(xlsx_path):
        return xlsx_path
    print(f"  导出失败: {result.stderr[:200]}")
    return None


def parse_price(val):
    """Parse a price cell, return float or None"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s in ("-", "", "免费", "前三个月免费"):
        return None
    m = re.search(r"[\d.]+", s)
    if m:
        try:
            return float(m.group())
        except ValueError:
            return None
    return None


def parse_xlsx_to_pricing(xlsx_path):
    """解析 xlsx 价格表 → models list"""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    # Prefer JoyMaaS sheet
    ws = None
    for name in wb.sheetnames:
        if "JoyMaaS" in name or "模型广场" in name:
            ws = wb[name]
            break
    if ws is None:
        ws = wb.active

    models = []
    current_provider = ""
    current_platform = ""

    for r in range(4, ws.max_row + 1):
        row = {i: ws.cell(row=r, column=c).value for i, c in enumerate(range(1, 23))}

        if row.get(0):
            current_platform = str(row[0]).strip()[:30]
        if row.get(1):
            current_provider = str(row[1]).strip()[:20]

        name_raw = row.get(2)
        if not name_raw:
            continue
        name = str(name_raw).strip()
        if "下线" in name:
            continue
        name_clean = re.sub(r"\n.*", "", name).strip()

        unit = str(row.get(9, "")).strip()
        if unit != "千tokens":
            continue

        input_raw = row.get(10)
        output_raw = row.get(11)
        cache_create_raw = row.get(12)
        cache_read_raw = row.get(13)

        if input_raw is None or output_raw is None:
            continue
        input_str = str(input_raw).strip()
        if input_str in ("免费", "-", ""):
            continue

        input_price = parse_price(input_raw)
        output_price = parse_price(output_raw)
        cache_read = parse_price(cache_read_raw)
        cache_create = parse_price(cache_create_raw)

        if input_price is None or output_price is None:
            continue

        is_peak = "高峰" in input_str or "低峰" in input_str
        is_tier = "小于" in input_str or "大于" in input_str

        entry = {
            "id": name_clean,
            "display_name": name_clean,
            "platform": current_platform,
            "provider": current_provider,
            "open_source": str(row.get(4, "")).strip() == "是",
            "commercial": str(row.get(5, "")).strip() == "是",
            "category": str(row.get(6, "")).strip()[:20],
            "tasks": str(row.get(7, "")).strip()[:30],
        }

        if is_tier:
            entry["pricing_type"] = "tiered"
            threshold_match = re.search(r"(\d+)k", input_str, re.IGNORECASE)
            threshold = int(threshold_match.group(1)) * 1000 if threshold_match else 272000
            entry["tier_threshold"] = threshold

            in_nums = [float(x) for x in re.findall(r"[\d.]+", input_str)]
            out_nums = [float(x) for x in re.findall(r"[\d.]+", str(output_raw or ""))]
            cr_nums = [float(x) for x in re.findall(r"[\d.]+", str(cache_read_raw or ""))]
            cc_nums = [float(x) for x in re.findall(r"[\d.]+", str(cache_create_raw or ""))]

            low_in = in_nums[0] if len(in_nums) >= 2 else input_price
            high_in = in_nums[-1] if len(in_nums) >= 2 else input_price
            low_out = out_nums[0] if len(out_nums) >= 2 else output_price
            high_out = out_nums[-1] if len(out_nums) >= 2 else output_price

            entry["low_tier"] = {
                "input": low_in * 1000, "output": low_out * 1000,
                "cache_read": cr_nums[0] * 1000 if cr_nums else None,
            }
            entry["high_tier"] = {
                "input": high_in * 1000, "output": high_out * 1000,
                "cache_read": cr_nums[-1] * 1000 if len(cr_nums) >= 2 else (cr_nums[0] * 1000 if cr_nums else None),
            }
            if cc_nums:
                entry["low_tier"]["cache_create"] = cc_nums[0] * 1000
                entry["high_tier"]["cache_create"] = cc_nums[-1] * 1000
        else:
            entry["pricing_type"] = "flat"
            entry["input"] = input_price * 1000
            entry["output"] = output_price * 1000
            entry["cache_read"] = cache_read * 1000 if cache_read else None
            entry["cache_write"] = cache_create * 1000 if cache_create else None

        models.append(entry)

    return models


def main():
    parser = argparse.ArgumentParser(description="价格表自动抓取")
    parser.add_argument("--dry-run", action="store_true", help="只解析不写入")
    parser.add_argument("--xlsx", type=str, help="使用本地 xlsx 文件")
    args = parser.parse_args()

    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    if args.xlsx:
        xlsx_path = args.xlsx
        if not os.path.isfile(xlsx_path):
            print(f"文件不存在: {xlsx_path}")
            return
    else:
        # 网络抓取
        tmp_path = os.path.join(SKILL_DIR, "_pricing_export.xlsx")
        print(f"正在从 JoySpace 抓取价格表: {SHEET_URL}")
        xlsx_path = export_xlsx(tmp_path)
        if not xlsx_path:
            print("抓取失败，保留现有 pricing.json")
            return

    print(f"解析 {xlsx_path}...")
    models = parse_xlsx_to_pricing(xlsx_path)
    print(f"解析到 {len(models)} 个模型")

    if not models:
        print("未解析到任何模型，保留现有 pricing.json")
        return

    # Merge with existing pricing.json (preserve _meta and manually added entries)
    existing = {}
    if os.path.isfile(PRICING_FILE):
        try:
            with open(PRICING_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            existing = {m["id"]: m for m in existing_data.get("models", [])}
        except (json.JSONDecodeError, KeyError):
            pass

    # New models from sheet override existing ones with same ID
    merged = {}
    for m in existing.values():
        merged[m["id"]] = m  # Start with existing
    for m in models:
        merged[m["id"]] = m  # Override with fresh data

    # Also add aliases for date-suffixed model names (e.g. gpt-5.5-2026-04-24 → GPT-5.5)
    # by matching the base name
    for m in list(merged.values()):
        mid = m["id"]
        # Check if any existing model is a date-suffixed version of this model
        for eid, em in list(existing.items()):
            if eid != mid and eid.startswith(mid + "-") and eid[len(mid)+1:].replace("-", "").isdigit():
                # This is a date variant, inherit pricing
                variant = dict(m)
                variant["id"] = eid
                variant["display_name"] = m.get("display_name", mid)
                merged[eid] = variant

    output = {
        "_meta": {
            "description": "AI Coding 平台模型刊例价 — 自动从 JoySpace 价格表抓取",
            "source": SHEET_URL,
            "currency": "CNY",
            "unit": "元/1M tokens",
            "exchange_rate": 7.2,
            "last_updated": now,
            "model_count": len(merged),
        },
        "models": list(merged.values()),
    }

    if args.dry_run:
        print(f"[DRY RUN] 将写入 {len(merged)} 个模型到 {PRICING_FILE}")
        for m in list(merged.values())[:5]:
            print(f"  {m['id']:30s} type={m['pricing_type']}")
        return

    with open(PRICING_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"已更新 {PRICING_FILE} ({len(merged)} 个模型)")

    # 清理临时文件
    if not args.xlsx:
        for tmp in [tmp_path, tmp_path.replace(".xlsx", ".csv")]:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
