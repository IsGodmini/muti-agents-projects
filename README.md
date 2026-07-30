# TripOps AI · 多 Agent 文旅产品智能策划平台

面向旅行社产品经理的命令行智能策划工具。通过自然语言交互，自动完成资源检索、行程规划、约束校验、成本核算和质量审核。

## 快速开始

```bash
# 安装依赖
uv sync --extra dev

# 配置环境变量
cp .env.example .env
# 编辑 .env 填写 LLM_API_KEY 和 TAVILY_API_KEY

# 启动
./start.sh
```

## 技术栈

| 分层 | 技术 |
|---|---|
| Agent 编排 | LangGraph（条件路由、interrupt 审批、重试） |
| LLM | 火山引擎 Ark（OpenAI-compatible） |
| 搜索 | Tavily Remote MCP（Streamable HTTP） |
| 路线优化 | Google OR-Tools |
| API | FastAPI + Uvicorn |
| 持久化 | JSON 文件存储（方案版本、审批记录） |
| 基础设施 | Docker Compose（PostgreSQL + PostGIS + Redis + MinIO） |

## 项目结构

```
├── cli.py                  # CLI 入口
├── app/
│   ├── config.py           # 配置
│   ├── main.py             # FastAPI
│   ├── agents/             # LangGraph 工作流
│   │   ├── graph.py        # 12 节点状态图
│   │   ├── state.py        # 状态定义
│   │   └── prompts.py      # LLM 提示词
│   ├── api/routes.py       # API 路由
│   ├── models/schemas.py   # Pydantic Schema
│   ├── services/           # 外部服务适配器
│   ├── skills/loader.py    # Skill 加载器
│   └── tools/              # Tool Registry
├── skills/                 # 7 个业务 Skill 定义
├── tests/                  # 测试
├── infra/                  # PostgreSQL 初始化
└── docs/                   # 设计文档
```

## 测试

```bash
uv run pytest -q
uv run ruff check app tests cli.py
```
