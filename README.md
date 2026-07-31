# TripOps AI · 多 Agent 旅行策划助手

面向个人用户的命令行智能旅行策划工具。和 Agent 聊几句，自动完成资源搜索、路线优化、行程编排、成本估算、质量检查与交付物生成。

## 快速开始

```bash
uv sync --extra dev
cp .env.example .env   # 填写 LLM_API_KEY、TAVILY_API_KEY（可选 AMAP_API_KEY）
./start.sh
```

## 交互示例

```
👤 暑假想带孩子去北京玩几天
🤖 听起来很棒！孩子多大了？大概玩几天？
👤 10岁，4天
🤖 几个人去？预算大概多少？
👤 一家三口，人均3000
🤖 了解了，开始策划！
▸ 多 Agent 工作流执行中 → 完整方案 → 确认存档
```

## 技术栈

| 分层 | 技术 |
|---|---|
| Agent 编排 | LangGraph（14 节点、3 个条件路由、interrupt 审批） |
| LLM | 火山引擎 Ark（多模型分层：复杂推理 / 简单推理 / 多模态） |
| 搜索 | Tavily Remote MCP + 高德 POI（双路实时检索） |
| 地图与交通 | 高德开放平台（POI、真实交通时间） |
| 天气 | 和风天气（逐日预报，行程编排参考） |
| 路线优化 | Google OR-Tools |
| 文生图 | ComfyUI（`MOCK_IMAGEGEN=true` 时使用预处理素材） |
| 交付物 | Markdown 报告 + Pillow 合成 PDF（封面 + 分日插图页） |
| API | FastAPI（可选，含 SSE 流式进度） |
| 持久化 | JSON 文件（方案版本 + 审批记录 + 报告/PDF/海报） |
| 防护与评估 | Prompt Injection Guard、资源评分排序、确定性校验、质量评估框架 |

## 项目结构

```
├── cli.py                  # 对话式 CLI 入口
├── app/
│   ├── agents/
│   │   ├── graph.py        # 策划工作流（14 节点）
│   │   ├── chat_graph.py   # 对话式需求收集图
│   │   ├── prompts.py      # LLM 提示词
│   │   └── state.py        # PlanningState / ChatState
│   ├── api/routes.py       # FastAPI 路由（health/tools/chat/plans/approval/SSE）
│   ├── models/schemas.py   # Pydantic Schema
│   ├── services/           # guard / ranking / verifier / evaluation /
│   │                       # renderer / pdf_report / amap / tavily_mcp /
│   │                       # model_gateway / plan_store / poster / weather
│   └── tools/              # Tool Registry（7 个工具）
├── scripts/                # e2e 全流程、ComfyUI 连通性脚本
├── tests/                  # 19 个单元测试 + 3 个可选集成测试（ComfyUI）
└── docs/                   # 设计文档
```

## 测试

```bash
uv run pytest -q -m "not integration"   # 19 个单元测试，无需外部服务
uv run ruff check app tests cli.py      # 静态检查
# 有 ComfyUI 环境时：uv run pytest -q   # 含 3 个集成测试
```

> 单元测试在 `MOCK_MODEL_MODE=true` 下运行（`tests/conftest.py` 自动设置），不消耗真实 API 配额。
