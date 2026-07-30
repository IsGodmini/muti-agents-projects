# 07｜接口说明

## 1. 两种使用方式

TripOps AI 的主入口是对话式 CLI，同时提供一个可选的 FastAPI REST 接口用于程序化调用。

| 方式 | 入口 | 适用场景 |
| --- | --- | --- |
| CLI | `./start.sh` 或 `uv run python cli.py` | 个人用户交互式策划 |
| REST API | `uv run uvicorn app.main:app --port 8000` | 程序化集成、调试 |

## 2. CLI 交互流程

```text
👤 暑假想带孩子去北京玩几天
🤖 听起来很棒！孩子多大了？大概玩几天？
👤 10岁，4天
🤖 几个人去？预算大概多少？
👤 一家三口，人均3000
🤖 了解了，开始策划！
▸ 多 Agent 工作流执行 → 行程 / 报价 / 质量报告 → 确认存档
```

- LLM 主导提问，无固定表单。
- 用户随时可输入"开始吧 / 够了 / ok"等关键词立即执行。
- 工作流结束后在 CLI 中确认（回车批准 / `n` 放弃）。

## 3. REST 基本信息

- 默认地址：`http://localhost:8000`
- API 前缀：`/api/v1`
- 数据格式：`application/json`
- 交互式文档：`http://localhost:8000/docs`

## 4. 接口总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/tools` | 获取工具元数据 |
| POST | `/api/v1/plans/run` | 创建并执行策划任务 |
| POST | `/api/v1/plans/{thread_id}/approval` | 提交审批并恢复工作流 |

## 5. 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

示例响应：

```json
{
  "status": "ok",
  "service": "TripOps AI API",
  "environment": "development",
  "mock_model_mode": false,
  "tavily_mcp_enabled": true,
  "tavily_mcp_configured": true
}
```

健康检查只返回 Tavily 是否已配置，不返回密钥。

## 6. 获取 Tools

```bash
curl http://localhost:8000/api/v1/tools
```

响应包含 Tool 名称、描述、风险等级和分类。

## 7. 运行策划任务

请求体为完整的 `PlanRequest`：

```bash
curl -X POST http://localhost:8000/api/v1/plans/run \
  -H "Content-Type: application/json" \
  -d '{
    "title": "北京研学之旅",
    "product_type": "study_tour",
    "destination": "北京",
    "days": 4,
    "nights": 3,
    "group_size": 10,
    "budget_per_person": 3000,
    "target_margin_rate": 0.15,
    "target_audience": "8-12岁儿童及家长",
    "themes": ["传统文化", "博物启蒙"],
    "constraints": ["连续乘车不超过90分钟"]
  }'
```

`product_type` 取值：`family_trip` / `study_tour` / `corporate_team_building` / `senior_friendly`。

响应包含 `thread_id`、状态和完整方案数据（行程、报价、质量报告）。`thread_id` 是后续恢复工作流的关键，调用方必须保存。

## 8. 提交审批

```bash
curl -X POST http://localhost:8000/api/v1/plans/{thread_id}/approval \
  -H "Content-Type: application/json" \
  -d '{
    "approved": true,
    "reviewer_id": "cli-user",
    "comment": "预算和节奏符合要求，可以交付"
  }'
```

批准后进入交付阶段（生成海报 + 存档）；驳回时记录原因并结束。

## 9. 状态语义

| 状态 | 含义 |
| --- | --- |
| `running` | 工作流正在执行 |
| `waiting_approval` | 已暂停，等待人工决定 |
| `delivered` | 工作流已完成交付 |
| `rejected` | 审批被驳回 |
| `failed` | 节点执行失败 |

当前接口在一次 HTTP 请求内同步执行到审批点。生产环境可改为异步任务并增加状态查询接口。

## 10. 错误处理

常见 HTTP 状态：

- `422`：请求 Schema 校验失败。
- `404`：thread_id 不存在。
- `409`：当前线程状态不允许执行该动作。
- `500`：未处理的内部错误。
- `503`：模型、搜索等必要依赖不可用。

## 11. 生产化接口建议

当前仓库未完整实现以下能力：

- OIDC/JWT 身份认证。
- 租户级 RBAC。
- `Idempotency-Key`。
- 异步任务查询、取消和 SSE/WebSocket 进度流。
- API 限流与审计导出。
