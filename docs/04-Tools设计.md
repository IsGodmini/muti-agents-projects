# 04｜Tools 设计

## 1. 设计原则

Tool 回答"系统能执行什么动作"。本项目不使用独立的 Skill 调度层，而是让每个 LangGraph 节点直接绑定自己需要的 Tools。这样工具保持稳定，节点职责清晰，权限审计和 Mock 测试都更简单。

Agent 节点不直接导入基础设施客户端，而是通过 Tool Registry 查找允许调用的工具。

## 2. Tool Registry

Tool Registry（`app/tools/registry.py`）统一保存：

- 工具名称和描述。
- 参数 Schema（Pydantic 输入模型）。
- 风险等级。
- 执行函数（同步或异步）。

注册方式：

```python
@tool_registry.register(
    name="optimize_itinerary",
    description="Produce a stable visit order using OR-Tools with real travel times.",
    category="optimization",
    risk_level=ToolRisk.WRITE_INTERNAL,
    input_model=OptimizeRouteInput,
)
def optimize_itinerary(payload: OptimizeRouteInput) -> list[list[str]]:
    ...
```

Registry 同时提供：

- `invoke`：调用同步工具。
- `ainvoke`：调用异步工具（如 MCP 网络调用）。

若错误地用 `invoke` 调用异步工具，Registry 会立即报错，避免未等待的协程进入工作流。`ainvoke` 用于所有异步 I/O 工具（如 MCP 网络调用）。

## 3. 当前工具清单（7 个）

| Tool | 作用 | 风险 | 类型 | 分类 |
| --- | --- | --- | --- | --- |
| `search_attractions` | 通过 Tavily Remote MCP 检索目的地网页资源 | READ_ONLY | 异步 | mcp_search |
| `search_poi_amap` | 高德 POI 结构化检索（坐标、评分、分类） | READ_ONLY | 异步 | geo_search |
| `get_weather_forecast` | 和风天气逐日预报，失败时显式降级到高德天气 | READ_ONLY | 异步 | geo_search |
| `calculate_route_matrix` | 合并高德真实时长与 LLM 估算，构造完整交通矩阵 | READ_ONLY | 异步 | geo_compute |
| `optimize_itinerary` | 使用 OR-Tools 求解访问顺序并返回与请求天数等长的分组 | WRITE_INTERNAL | 同步 | optimization |
| `calculate_product_cost` | 从 LLM 估算的成本明细计算售价与毛利 | READ_ONLY | 同步 | pricing |
| `submit_for_approval` | 持久化人工审批决定 | EXTERNAL_ACTION | 同步 | approval |

风险分级：

- READ_ONLY：只读查询或纯计算。
- WRITE_INTERNAL：影响方案或持久化状态，但不直接产生外部商业后果。
- EXTERNAL_ACTION：发布、预订、支付或其他外部写操作，默认要求人工确认。

## 4. 节点与工具的绑定

| 节点 | 绑定的工具 |
| --- | --- |
| retrieve_resources | search_attractions、search_poi_amap |
| plan_itinerary | get_weather_forecast、calculate_route_matrix（内部调用高德真实时长）、optimize_itinerary |
| repair_plan | search_attractions |
| calculate_quote | calculate_product_cost |
| finalize_delivery | submit_for_approval（版本快照由节点直接写入 PlanStore） |

其余节点（validate_constraints、quality_review、run_verification、review_repair、prepare_poster、approval_gate、mark_failed、mark_rejected）不经过 Tool Registry，分别由确定性规则、适配器或中断完成。

## 5. 工具设计规范

每个工具需要满足：

- 使用明确的 Pydantic 输入模型，参数不接受未经约束的任意字典。
- 返回业务错误而不是吞掉异常。
- 外部请求必须配置超时。
- 日志不记录密钥和敏感个人信息。
- 写操作必须有幂等策略（如版本号自增）。
- 不包含硬编码业务数据，所有输入来自真实搜索结果或 LLM 估算。
- 外部数据（网页搜索）先经过 Guard 注入防护再进入上下文。
- 任何代表“免费/已含/自理/待确认”的零值都不能直接进入 `QuoteItem`；成本工具只接受正金额明细。

## 6. 少资源与外部服务降级

- `retrieve_resources` 并行调用 Tavily 与高德，可容忍单个来源失败；所有来源失败或过滤后无可用景点才终止。
- `get_weather_forecast` 优先调用和风天气，和风未配置或请求失败时尝试高德；结果携带 `provider`，报告会标注真实来源。
- `optimize_itinerary` 对 0 个或 1 个资源的特殊分支同样返回 `days` 个日槽，避免把“四日游”错误压缩成一天。
- `calculate_product_cost` 的输入明细必须 `amount > 0`。LLM 输出层允许暂时出现 0，策划节点先过滤；如果没有任何正金额明细，使用住宿、交通、餐饮和服务的保守估算。

## 7. MCP 的位置

项目作为 MCP Client 接入 Tavily Remote MCP：

```text
retrieve_resources 节点
  → Tool Registry.search_attractions
    → RemoteMCPClient
      → Tavily MCP.tavily_search
        → 带 URL 的实时网页结果
```

`RemoteMCPClient` 使用官方 MCP Python SDK 的 Streamable HTTP Transport。API Key 放在 `Authorization: Bearer` 请求头中，不写入 URL、日志或代码。

当前项目是 MCP Client，而不是自建 MCP Server。后续可以把地图、内部资源库等工具封装成 MCP Server 供复用。详细实现参见 [11-Tavily-MCP真实搜索](./11-Tavily-MCP真实搜索.md)。

## 8. 权限模型

建议采用三层约束：

```text
平台权限：该租户是否启用某工具
    ↓
节点白名单：该节点是否允许调用
    ↓
运行时审批：本次调用是否需要人工确认
```

即使模型在文本中要求调用未授权工具，执行层也必须拒绝。

## 9. 安全检查清单

- [x] 工具参数经过 Schema 校验。
- [x] 节点只调用自己绑定的工具。
- [x] 高风险动作（审批）要求人工确认。
- [x] 写操作幂等（版本自增）。
- [x] 返回内容的 Prompt Injection 检测（`app/services/guard.py`，检索节点实时过滤）。
- [x] 外部资源标记来源和检索时间（provider / source_url / retrieved_at）。
- [x] 行程分组保持请求天数，少资源场景有回归测试。
- [x] 天气和图片降级均保留来源，缺失必需交付资产时阻断。
- [ ] 工具耗时、状态与错误码的结构化日志及审计导出（生产化补充）。
