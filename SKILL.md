---
name: ai-usage-monitor
description: >-
  AI Coding 平台 Token 用量监控与费用看板。当用户说"看用量"、"用量报告"、"token 消耗"、
  "花了多少钱"、"费用看板"、"用量监控"、"usage report"、"token usage"、
  "看看 token"、"用了多少"、"费用统计"等表达时触发。
  解析 Proma、Claude Code、JoyCode 三个工具的会话日志，
  按模型/会话/工具/日期维度统计 token 用量，
  匹配模型刊例价计算 CNY 费用，生成交互式 HTML 看板并打开预览。
  也适用于用户询问某个模型贵不贵、对比模型价格、查看价格表等场景。
---

# AI Coding 平台 Token 用量监控

## 支持的工具

| 工具 | 数据源 |
|------|--------|
| **Proma** | `~/.proma/agent-sessions/*.jsonl` + `~/.proma/agent-sessions.json` (会话标题) |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` (含 ai-title 会话标题) |
| **JoyCode** | `~/.joycode/default-workspace/joycode.joycoder-editor/task_history/*.json` |

## 执行步骤

### 1. 运行看板生成脚本

```bash
python "<SKILL_DIR>/scripts/generate_dashboard.py"
```

脚本会自动扫描上述三个工具的数据源，解析 token 用量，匹配 `pricing.json` 中的模型刊例价，生成 HTML 看板到 `<SKILL_DIR>/dashboard.html`。

### 2. 打开 HTML 看板预览

将 `dashboard.html` 复制到项目根目录后用 `BrowserPreviewOpen` 打开。

### 3. 输出文本摘要

包括总 token、总费用 CNY、本周费用、各工具会话数、未匹配模型提示。

## 看板功能

- **时间段筛选**：今天 / 本周(默认) / 本月 / 全部，所有表格联动
- **每次任务 Tab**：精确到每次 prompt 的 token 和费用，含工具/模型/会话名/prompt 内容(tooltip)
- **按会话统计 Tab**：会话级聚合，显示会话名称（来自工具自身命名），tooltip 显示完整名称
- **按模型统计 Tab**：模型级聚合
- **模型价格表 Tab**：精简 7 列（模型/可商用/任务场景/计价方式/输入/缓存读取/输出）

## 文件结构

```
ai-usage-monitor/
├── SKILL.md
├── pricing.json          # 模型刊例价 (元/1M tokens)
├── dashboard.html        # 生成的看板
└── scripts/
    └── generate_dashboard.py
```

## 价格表维护

当出现未匹配模型时，看板顶部会显示警告。需要用户补充价格信息后编辑 `pricing.json`。
价格来源必须是用户明确提供的数据，不得自行搜索或猜测。

## 当前未匹配模型

以下模型出现在会话中但价格表中缺失，需要补充：

1. **gpt-5-2025-08-07** — Claude Code 中使用的 GPT-5 版本，可能与 `GPT-5` 是同一模型
2. **JoyAI-Code-1.5** — JoyCode 的默认模型，需用户提供价格
