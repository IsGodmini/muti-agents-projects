# TripOps AI · 多 Agent 旅行策划助手

面向个人用户的命令行智能旅行策划工具。和 Agent 聊几句，自动完成资源搜索、路线优化、行程编排、成本估算和质量检查。

## 快速开始

```bash
uv sync --extra dev
cp .env.example .env   # 填写 LLM_API_KEY 和 TAVILY_API_KEY
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
| Agent 编排 | LangGraph（12 节点、条件路由、interrupt 审批） |
| LLM | 火山引擎 Ark（OpenAI-compatible） |
| 搜索 | Tavily Remote MCP（实时网页搜索） |
| 路线优化 | Google OR-Tools |
| API | FastAPI（可选） |
| 持久化 | JSON 文件 |

## 项目结构

```
├── cli.py                  # 对话式 CLI 入口
├── app/
│   ├── agents/graph.py     # LangGraph 工作流（12 节点）
│   ├── agents/prompts.py   # LLM 提示词
│   ├── models/schemas.py   # Pydantic Schema
│   ├── services/           # LLM / MCP / 持久化
│   └── tools/              # Tool Registry（6 个工具）
├── tests/                  # 8 个测试
├── infra/                  # PostgreSQL 初始化（规划）
└── docs/                   # 设计文档
```

## 测试

```bash
uv run pytest -q && uv run ruff check app tests cli.py
```
