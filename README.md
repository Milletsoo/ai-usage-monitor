# 📊 AI Coding 平台 Token 用量监控

一个本地运行的 Token 用量监控与费用看板工具，支持 **Proma**、**Claude Code**、**JoyCode** 三个 AI Coding 工具的会话日志解析。

## ✨ 功能

- 🔍 **自动扫描**三个工具的会话日志，无需手动配置
- 💰 **精确费用计算**：匹配模型刊例价，支持统一价/分层计价/峰谷计价
- 📋 **每次任务粒度**：精确到每个 prompt 的 token 消耗和费用
- 🗂️ **多维度统计**：按会话/模型/Skill/日期聚合
- 📅 **时间段筛选**：今天/本周/本月/全部，一键切换
- 📈 **可视化看板**：每日费用趋势柱状图，交互式 HTML
- 🔗 **子 Agent 合并**：自动识别并合并子 Agent 数据到父会话
- 📝 **智能去重**：合并同一次 prompt 的多次 API 调用
- 💲 **模型价格表**：内置 23+ 模型刊例价（元/1M tokens），可自定义

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/Milletsoo/ai-usage-monitor.git
cd ai-usage-monitor
```

### 运行

```bash
python scripts/generate_dashboard.py
```

脚本会自动扫描以下路径：

| 工具 | 数据源 |
|------|--------|
| **Proma** | `~/.proma/agent-sessions/*.jsonl` + `~/.proma/agent-sessions.json` |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` |
| **JoyCode** | `~/.joycode/default-workspace/joycode.joycoder-editor/task_history/*.json` |

生成的 HTML 看板默认输出到 `dashboard.html`，用浏览器打开即可。

### 可选参数

```
python scripts/generate_dashboard.py [--output PATH] [--pricing PATH]
```

## 📁 文件结构

```
ai-usage-monitor/
├── README.md
├── SKILL.md              # Proma Skill 描述文件
├── pricing.json          # 模型刊例价表 (元/1M tokens)
├── scripts/
│   └── generate_dashboard.py   # 看板生成脚本
└── dashboard.html        # 生成的看板 (运行后出现)
```

## 💲 自定义价格表

编辑 `pricing.json` 添加或修改模型价格。价格单位为 **元/1M tokens**（原始元/千tokens × 1000）。

支持三种计价模式：

| 类型 | 说明 | 示例模型 |
|------|------|---------|
| `flat` | 统一价格 | GLM-5.2, GPT-5.5 |
| `tiered` | 分层计价（按上下文长度） | GLM-5, Gemini 3.1 Pro |
| `peak_offpeak` | 峰谷计价（按时间段） | DeepSeek-V4-Pro |

## 📊 看板预览

看板包含 4 个 Tab：

1. **每次任务** — 每个 prompt 的 token 和费用，含工具/模型/会话名/prompt 内容
2. **按会话统计** — 会话级聚合，显示会话名称
3. **按模型统计** — 模型级聚合
4. **模型价格表** — 所有模型的刊例价展示

## 🔧 作为 Proma Skill 使用

如果你使用 [Proma](https://proma.cool)，可以将本目录放入 skills 文件夹，之后在任何会话中说 **"看用量"** 即可自动生成报告。

## 📄 License

MIT
