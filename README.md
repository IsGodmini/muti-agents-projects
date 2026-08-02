# TripOps AI · 多 Agent 旅行策划助手

面向个人用户的对话式智能旅行策划工具。用户在简洁的 Web 页面中与 Agent 对话；Agent 收集并确认需求后，自动完成资源搜索、路线优化、行程编排、成本估算、质量检查与交付物生成。

## 快速开始

```bash
uv sync --extra dev
cp .env.example .env   # 填写 LLM_API_KEY、TAVILY_API_KEY（可选 AMAP_API_KEY）
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000/`。`./start.sh` 仍保留为 CLI 调试入口。

## 交互示例

```
👤 10 月 1 日一家三口去西安玩 4 天，人均预算 3000 元
🤖 必要信息已经齐全。还有没有其他注意事项或需要避免的情况？
👤 不吃辣，节奏舒缓
🤖 请确认以上需求：……确认后我才会开始生成旅行攻略。
👤 确认
▸ 多 Agent 节点流实时执行 → 待确认方案 → PDF / Markdown 交付
```

## 技术栈

| 分层 | 技术 |
|---|---|
| Agent 编排 | LangGraph（13 节点、4 个条件路由、interrupt 审批） |
| LLM | 火山引擎 Ark（多模型分层：复杂推理 / 简单推理 / 多模态） |
| 搜索 | Tavily Remote MCP + 高德 POI（双路实时检索） |
| 地图与交通 | 高德开放平台（POI、真实交通时间） |
| 天气 | 和风天气（首选）+ 高德天气（真实数据降级） |
| 路线优化 | Google OR-Tools |
| 文生图 | ComfyUI（`MOCK_IMAGEGEN=true` 时使用预处理素材） |
| 交付物 | 带封面和每日配图的精简矢量 PDF + 不嵌图的详细 Markdown 执行报告 |
| API | FastAPI（Web 前端、对话门禁、SSE 节点进度、审批与交付物访问） |
| 持久化 | JSON 文件（方案版本 + 审批记录 + 报告/PDF/海报） |
| 防护与评估 | Prompt Injection Guard、资源评分排序、确定性校验、质量评估框架 |

## 项目结构

```
├── cli.py                  # 对话式 CLI 入口
├── app/
│   ├── static/index.html  # Web 前端（对话收集/自动策划/审批/查看报告）
│   ├── agents/
│   │   ├── graph.py        # 策划工作流（13 节点）
│   │   ├── chat_graph.py   # 对话式需求收集图
│   │   ├── prompts.py      # LLM 提示词
│   │   └── state.py        # PlanningState / ChatState
│   ├── api/routes.py       # FastAPI 路由（health/chat/approval/SSE）
│   ├── models/schemas.py   # Pydantic Schema
│   ├── services/           # guard / ranking / verifier / evaluation /
│   │                       # renderer / pdf_report / amap / tavily_mcp /
│   │                       # model_gateway / plan_store / poster / weather
│   └── tools/              # Tool Registry（7 个工具）
├── scripts/                # e2e 全流程、ComfyUI 连通性脚本
├── tests/                  # 75 个离线测试 + 3 个可选集成测试（ComfyUI）
└── docs/                   # 设计文档
```

## 测试

```bash
UV_CACHE_DIR=/tmp/tripops-uv-cache uv run pytest -q --ignore=tests/test_comfyui.py
UV_CACHE_DIR=/tmp/tripops-uv-cache uv run ruff check app tests scripts cli.py
# 有 ComfyUI 环境时：uv run pytest -q   # 含 3 个集成测试
```

> 单元测试在 `MOCK_MODEL_MODE=true` 下运行（`tests/conftest.py` 自动设置），不消耗真实 API 配额。

## 关键可靠性保证

- 必须收集目的地、出发时间、天数、人数/客群与预算，再单独询问注意事项并展示摘要；只有用户明确确认后才能启动策划。
- 路线优化始终返回请求天数对应的日槽；资源不足时补充自由活动/休整日，约束校验与最终验证都会阻止缺天、重复天或超出天数的方案交付。
- LLM 返回的零金额成本项会被过滤；若全部为零则使用非零的保守估算，节点中的 `0` 通过“免费/已含/自理/待确认”表达，不再显示成误导性的 `¥0`。
- ComfyUI 可按普通电脑的处理能力顺序排队；生成失败时尝试真实资源图或高德静态地图，仍缺少封面或每日图片则阻止交付。
- 工作流失败通过结构化 SSE/JSON 返回可读原因，前端恢复对话输入，允许用户调整预算、天数、目的地或注意事项后重新生成。
