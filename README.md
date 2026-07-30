# TripOps AI · 多 Agent 旅行策划助手

面向个人用户的命令行智能旅行策划工具。回答几个问题，自动完成资源搜索、路线优化、行程编排、成本估算和质量检查。

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

## 使用示例

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  TripOps AI · 文旅产品智能策划
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▸ 基础信息
  目的地 [杭州]: 成都
  产品类型: 1.亲子旅行 2.研学旅行 3.企业团建 4.银龄慢游
  请选择 [1]: 1
  ...

▸ 补充信息
  还有其他需求或特殊情况吗？: 孩子对熊猫很感兴趣，一定要去大熊猫基地

▸ 需求确认 → 多 Agent 工作流自动执行 → 输出完整方案 → 交互审批
```

## 技术栈

| 分层 | 技术 |
|---|---|
| Agent 编排 | LangGraph（条件路由、interrupt 审批、重试） |
| LLM | 火山引擎 Ark（OpenAI-compatible） |
| 搜索 | Tavily Remote MCP（实时网页搜索） |
| 路线优化 | Google OR-Tools |
| API | FastAPI（可选启动） |
| 持久化 | JSON 文件存储 |

## 项目结构

```
├── cli.py                  # CLI 入口
├── app/
│   ├── agents/             # LangGraph 工作流 + 提示词
│   ├── models/schemas.py   # Pydantic Schema
│   ├── services/           # LLM / MCP / 持久化适配器
│   ├── skills/loader.py    # Skill 加载器
│   └── tools/              # Tool Registry + 领域工具
├── skills/                 # 7 个业务 Skill（含质量门禁）
├── tests/                  # 9 个测试
├── infra/                  # PostgreSQL 初始化（规划）
└── docs/                   # 设计文档
```

## 测试

```bash
uv run pytest -q
uv run ruff check app tests cli.py
```
