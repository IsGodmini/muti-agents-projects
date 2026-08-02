# 07｜接口说明

## 1. 使用方式

TripOps AI 的主入口是 FastAPI 托管的 Web 对话页面。CLI 只保留为本地调试入口；REST API 不再提供绕过对话的直接策划接口。

| 方式 | 入口 | 适用场景 |
| --- | --- | --- |
| Web | `uv run uvicorn app.main:app --port 8000` 后访问 `/` | 推荐的完整用户流程 |
| CLI | `./start.sh` 或 `uv run python cli.py` | 开发调试 |
| REST API | `/api/v1/*` | 前端调用与程序化集成 |

## 2. Web 对话流程

```text
👤 10 月 1 日一家三口去西安 4 天，人均 3000 元
🤖 还有没有其他注意事项或需要避免的情况？
👤 不吃辣，节奏舒缓
🤖 请确认以上需求：……
👤 确认
▸ SSE 节点流 → 自动审核 → 用户批准 → PDF / Markdown
```

- 无固定策划表单，也没有“直接生成”按钮。
- 出发时间是必要信息；必要信息齐全后必须询问注意事项。
- 用户看到摘要并明确确认后，前端才自动调用策划流。
- 页面只展示当前节点的活动；结束后隐藏当前节点，失败时恢复聊天并给出调整建议。

## 3. REST 基本信息

- 默认地址：`http://localhost:8000`
- API 前缀：`/api/v1`
- 数据格式：`application/json`
- 交互式文档：`http://localhost:8000/docs`
- Web 前端：`http://localhost:8000/`（单一对话入口、自动策划、SSE 进度、审批、查看报告）
- 交付物文件：`http://localhost:8000/files/plans/{plan_id}/report.pdf|report.md`

## 4. 接口总览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/chat` | 对话式需求收集（多轮，thread_id 续聊） |
| POST | `/api/v1/chat/{chat_thread_id}/plan/stream` | 从信息已充分的对话启动 SSE 策划 |
| POST | `/api/v1/plans/{thread_id}/approval` | 提交审批并恢复工作流 |

## 5. 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

示例响应：

```json
{
  "status": "ok",
  "mock_model_mode": false
}
```

健康检查只返回前端需要的服务状态和演示模式标记。工具注册表不再通过公开 API 暴露。

## 6. 对话式需求收集

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "暑假想带孩子去北京玩几天"}'
```

响应示例：

```json
{
  "thread_id": "9f8e...",
  "reply": "听起来很棒！孩子多大了？大概玩几天？",
  "ready": false,
  "stage": "collecting",
  "plan_request": null
}
```

- `thread_id` 留空则新建会话；带上已返回的 `thread_id` 可继续多轮对话。
- `stage` 按 `collecting` → `notes` → `confirming` → `ready` 推进；服务端会归一化模型返回的中英文阶段别名。
- 必要信息包含目的地、出发时间、天数、人数/人群和预算。
- `notes` 阶段会单独询问其他注意事项；`confirming` 阶段返回 `plan_request` 供用户核对。
- 只有用户在看到摘要后明确确认，才会返回 `ready=true`。
- 前端会在 `ready=true` 后自动启动策划，用户无需再填写或提交表单。

## 7. 从对话启动策划

策划接口不接受 `PlanRequest` 请求体，只使用对话图已保存的结构化需求：

```bash
curl -N -X POST \
  http://localhost:8000/api/v1/chat/{chat_thread_id}/plan/stream
```

- `chat_thread_id` 必须来自 `/chat` 返回的会话，且该会话必须已由模型判定 `ready=true`。
- 信息不完整或会话不存在时返回 `409`，不会执行工作流。
- 旧的 `/plans/run` 和 `/plans/run/stream` 已删除，无法绕过对话直接传入策划参数。

## 8. SSE 流式执行

事件格式（`text/event-stream`）：

```text
event: started
data: {"thread_id":"...","chat_thread_id":"...","plan_id":"...","workflow":[...]}

event: node_start
data: {"id":"retrieve_resources","label":"搜索资源","activities":["搜索景点与真实 POI","..."]}

event: node_end
data: {"id":"retrieve_resources"}

event: completed
data: {"thread_id":"...","plan_id":"...","current_stage":"poster_generated","requires_approval":true,"recoverable":false}
```

事件类型：`started`、`node_start`、`node_end`、`completed`、`error`。`started.workflow` 提供完整节点流；`node_start.activities` 只描述当前节点。若自动策划进入 `failed`，`completed` 会带 `recoverable=true` 和 `failure_reasons`；未捕获异常则通过 `error` 事件返回同类字段。

## 9. 提交审批

```bash
curl -X POST http://localhost:8000/api/v1/plans/{thread_id}/approval \
  -H "Content-Type: application/json" \
  -d '{
    "approved": true,
    "reviewer_id": "cli-user",
    "comment": "预算和节奏符合要求，可以交付"
  }'
```

- 图片在审批前准备；批准后恢复工作流进入 `finalize_delivery`（生成详细 Markdown、带图 PDF、版本快照与审批记录），响应状态 `delivered`。
- 驳回时记录审批决定并结束，响应状态 `draft`。

## 10. 状态语义

`PlanStatus` 枚举定义在 `app/models/schemas.py`：

| 状态 | 含义 |
| --- | --- |
| `draft` | 草稿（审批被驳回） |
| `running` | 工作流正在执行 |
| `waiting_approval` | 已暂停，等待人工决定 |
| `approved` | 已批准 |
| `delivered` | 工作流已完成交付 |
| `failed` | 节点执行失败 |

当前接口在一次 HTTP 请求内同步执行到审批点。生产环境可改为异步任务并增加状态查询接口。

## 11. 错误处理

常见 HTTP 状态：

- `422`：请求 Schema 校验失败。
- `404`：thread_id 不存在。
- `409`：当前线程状态不允许执行该动作。
- `502`：对话模型调用或结构化输出失败，响应体仍为 JSON `detail`。
- `500`：审批恢复等未处理的内部错误。

SSE 策划在建立连接后发生的错误不会改写 HTTP 状态，而是发送 `event: error`。前端先读取文本再安全解析 JSON，因此代理返回纯文本时也不会再出现 `Unexpected token 'I'`。

## 12. 生产化接口建议

当前仓库未完整实现以下能力：

- OIDC/JWT 身份认证。
- 租户级 RBAC。
- `Idempotency-Key`。
- 异步任务查询、取消（SSE 进度流已实现）。
- API 限流与审计导出。
