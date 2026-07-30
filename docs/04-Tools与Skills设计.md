# 04｜Tools 与 Skills 设计

## 1. 为什么同时需要 Tools 和 Skills

Tools 回答“系统能执行什么动作”，Skills 回答“面对某类任务应该怎样做”。

例如：

- `optimize_itinerary` 是 Tool，负责根据输入运行路线优化。
- `route-planning` 是 Skill，描述路线规划需要考虑的时间窗、停留时长、交通成本和失败处理。

把两者分开后，工具可以保持稳定，业务方法可以独立迭代。

## 2. Tool Registry

Tool Registry 统一保存：

- 工具名称和描述。
- 参数 Schema。
- 风险等级。
- 执行函数。
- 是否需要审批。
- 超时、重试和幂等属性。

Agent 不应直接导入任意基础设施客户端，而是通过注册表查找允许调用的工具。这使权限审计和 Mock 测试更简单。

## 3. 当前工具清单

| Tool | 作用 | 风险 | 当前状态 |
| --- | --- | --- | --- |
| `search_attractions` | 通过 Tavily Remote MCP 检索目的地网页资源 | 低 | ✅，无密钥时可降级 |
| `calculate_route_matrix` | 生成候选点之间的距离/耗时矩阵 | 低 | ✅ |
| `optimize_itinerary` | 使用约束求解优化访问顺序 | 中 | ✅ OR-Tools |
| `calculate_product_cost` | 汇总交通、住宿、活动等成本 | 中 | ✅ |
| `save_plan_version` | 保存方案版本 | 中 | 🟡 接口/数据层骨架 |
| `submit_for_approval` | 提交人工审批 | 高 | ✅ 工作流审批关口 |

风险分级建议：

- 低：只读查询或纯计算。
- 中：影响方案或持久化状态，但不直接产生外部商业后果。
- 高：发布、预订、支付或其他外部写操作，默认要求人工确认。

## 4. 工具设计规范

每个工具需要满足：

```python
@tool(
    name="calculate_product_cost",
    risk="medium",
    idempotent=True,
)
def calculate_product_cost(input: CostInput) -> CostResult:
    ...
```

规范包括：

- 使用明确的 Pydantic 输入输出模型。
- 参数不接受未经约束的任意字典。
- 返回业务错误而不是吞掉异常。
- 外部请求必须配置超时。
- 日志不记录密钥和敏感个人信息。
- 写操作必须有幂等策略。

## 5. Skill Registry

Skill Registry 维护可复用业务能力的元数据。一个 Skill 可以包含：

- `name`：稳定标识。
- `description`：适用任务。
- `instructions`：操作流程和约束。
- `allowed_tools`：允许调用的工具白名单。
- `input_schema` / `output_schema`：输入输出约定。
- `version`：技能版本。

Skill 可以用 `SKILL.md` 或结构化 Manifest 表达。运行时由 Agent 选择相关 Skill，将其说明加载到当前任务上下文，而不是把全部业务知识都塞进系统 Prompt。

## 6. 当前技能清单

项目包含七类技能：

1. 需求澄清：区分硬约束、软偏好和待确认信息。
2. 目的地研究：制定资源检索与证据筛选规则。
3. 行程设计：控制每天节奏、活动密度和人群适配。
4. 路线规划：处理地理距离、时间窗和折返问题。
5. 成本核算：统一成本口径、毛利和预算预警。
6. 方案质检：检查事实、冲突、时效性和风险。
7. 营销交付：提炼卖点并为海报生成视觉 Brief。

具体名称和元数据可通过 `GET /api/v1/skills` 查看。

## 7. 权限模型

建议采用三层约束：

```text
平台权限：该租户是否启用某工具
    ↓
Agent 白名单：该 Agent 是否允许调用
    ↓
运行时审批：本次调用是否需要人工确认
```

即使模型在文本中要求调用未授权工具，执行层也必须拒绝。

## 8. Tool 与 Skill 的版本治理

- Tool 的破坏性参数变化需要提升主版本。
- Skill 指令变化需要记录版本和评估结果。
- 每次 Agent 运行保存实际使用的 Tool/Skill 版本。
- 回归评估不过关时可回滚 Skill，而无需回滚整个应用。

## 9. MCP 的位置

项目已经作为 MCP Client 接入 Tavily Remote MCP：

```text
Resource Agent
  → Tool Registry.search_attractions
    → RemoteMCPClient
      → Tavily MCP.tavily_search
        → 带 URL 的实时网页结果
```

`RemoteMCPClient` 使用官方 MCP Python SDK 的 Streamable HTTP Transport。API Key 放在 `Authorization: Bearer` 请求头中，不写入 URL、日志或代码。

当前项目是 MCP Client，而不是自建 MCP Server。后续可以把地图、内部资源库等项目内 Tools 封装成 MCP Server，供多个 Agent 项目复用。详细实现参见 [11-Tavily-MCP真实搜索](./11-Tavily-MCP真实搜索.md)。

## 10. 安全检查清单

- [ ] 工具参数是否经过 Schema 校验？
- [ ] Agent 是否只看到白名单内的工具？
- [ ] 高风险动作是否要求审批？
- [ ] 写操作是否幂等？
- [ ] 是否记录工具耗时、状态和错误码？
- [ ] 返回内容是否可能包含 Prompt Injection？
- [ ] 外部资源是否标记来源和时效？
