# 07｜API 接口说明

## 1. 基本信息

- 默认后端地址：`http://localhost:8000`
- API 前缀：`/api/v1`
- 数据格式：`application/json`
- 交互式文档：`http://localhost:8000/docs`

实际字段以 FastAPI 自动生成的 OpenAPI 文档为准，本文用于说明业务语义。

## 2. 接口总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/skills` | 获取技能元数据 |
| GET | `/api/v1/tools` | 获取工具元数据 |
| POST | `/api/v1/plans/run` | 创建并执行策划任务 |
| POST | `/api/v1/plans/{thread_id}/approval` | 提交审批并恢复工作流 |

## 3. 健康检查

请求：

```bash
curl http://localhost:8000/api/v1/health
```

示例响应：

```json
{
  "status": "ok",
  "service": "tripops-api"
}
```

生产环境应进一步区分 liveness 与 readiness，后者检查数据库、Redis 和必要模型服务。

## 4. 获取 Skills

```bash
curl http://localhost:8000/api/v1/skills
```

响应包含 Skill 名称、说明和允许使用的工具，可用于管理页面展示和 Agent 能力审计。

## 5. 获取 Tools

```bash
curl http://localhost:8000/api/v1/tools
```

响应包含 Tool 名称、说明、风险等级和审批要求。前端不应只依赖展示字段做权限控制，服务端必须再次校验。

## 6. 运行策划任务

请求：

```bash
curl -X POST http://localhost:8000/api/v1/plans/run \
  -H "Content-Type: application/json" \
  -d '{
    "destination": "成都",
    "days": 4,
    "travelers": 6,
    "budget_per_person": 3500,
    "preferences": ["文化", "美食", "亲子"],
    "notes": "有老人，节奏不要太紧"
  }'
```

典型响应包含：

```json
{
  "thread_id": "plan_xxx",
  "status": "approval_required",
  "plan": {
    "itinerary": [],
    "cost_summary": {},
    "quality_report": {}
  },
  "approval": {
    "question": "是否批准该方案进入交付阶段？"
  }
}
```

`thread_id` 是后续恢复工作流的关键，调用方必须保存。

## 7. 提交审批

请求：

```bash
curl -X POST http://localhost:8000/api/v1/plans/plan_xxx/approval \
  -H "Content-Type: application/json" \
  -d '{
    "approved": true,
    "comment": "预算和节奏符合要求，可以交付"
  }'
```

批准后的响应会进入交付阶段；驳回时记录原因并按照图中的分支处理。

## 8. 状态语义

建议调用方支持：

| 状态 | 含义 |
| --- | --- |
| `running` | 工作流正在执行 |
| `approval_required` | 已暂停，等待人工决定 |
| `completed` | 工作流已完成 |
| `rejected` | 审批被驳回 |
| `failed` | 节点执行失败 |

当前同步演示接口可能在一次 HTTP 请求内执行到审批点。生产环境可将长任务改为异步任务，并增加查询状态接口。

## 9. 错误处理

建议统一错误结构：

```json
{
  "error": {
    "code": "INVALID_PLAN_REQUEST",
    "message": "days 必须大于 0",
    "details": {},
    "request_id": "req_xxx"
  }
}
```

常见 HTTP 状态：

- `422`：请求 Schema 校验失败。
- `404`：thread_id 不存在。
- `409`：当前线程状态不允许执行该动作。
- `429`：模型或任务配额超限。
- `500`：未处理的内部错误。
- `503`：模型、数据库等必要依赖不可用。

## 10. 生产化接口建议

当前仓库未完整实现以下能力：

- OIDC/JWT 身份认证。
- 租户级 RBAC。
- `Idempotency-Key`。
- 异步任务查询、取消和 SSE/WebSocket 进度流。
- OpenAPI 客户端自动生成。
- API 限流与审计导出。

增加这些能力时，应保持图的 `thread_id` 与外部任务 ID 映射清晰。

