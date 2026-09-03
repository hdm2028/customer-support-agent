# Agent 模块说明

这个目录承载售后客服 Agent 的主逻辑。它不是按“代码文件创建时间”来读，而应该按一次请求的执行链路来读。

## 一次请求怎么走

```text
agent_core.py
-> workflow.py / stream_runner.py
-> orchestrator.py
-> router.py
-> pending_task.py / conversation_context.py / memory.py
-> tool_executor.py
-> tool_registry.py
-> support_tools.py
-> prompt_builder.py / fallback_policy.py
```

简单理解：

```text
入口
-> 加载上下文
-> 路由识别
-> 多 Agent 分派
-> 工具执行
-> 风险和证据校验
-> 回复生成
-> 持久化 trace 与状态
```

## 1. 对外入口层

| 文件 | 作用 |
| --- | --- |
| `agent_core.py` | API 层调用的统一入口。`run_customer_support_agent()` 负责非流式请求，`stream_customer_support_agent()` 负责流式请求。 |
| `workflow.py` | 非流式主工作流。用 LangGraph 串起上下文加载、路由、工具执行、回复生成、持久化。 |
| `stream_runner.py` | 流式主工作流。逻辑和 `workflow.py` 类似，但会按 SSE 事件返回路由、工具结果、token 和完成状态。 |

想看“一个请求完整经过哪些节点”，先看 `workflow.py`。

## 2. 多 Agent 编排层

| 文件 | 作用 |
| --- | --- |
| `orchestrator.py` | Agent Orchestrator。根据路由结果决定本轮需要客服 Agent、售后 Agent、风控 Agent 中的哪些角色参与。 |
| `customer_agent.py` | 客服问答 Agent。负责普通咨询、知识库 RAG、商品推荐、快捷回复。 |
| `after_sales_agent.py` | 售后处理 Agent。负责订单、退款资格、退款申请、工单、人工审核等业务流程判断。 |
| `risk_agent.py` | 风控 Agent。负责大额退款、异常账号、高频退款、恶意投诉、虚假描述等风险判断。 |

想看“三个 Agent 怎么分工”，先看 `orchestrator.py`。

## 3. 路由与意图识别层

| 文件 | 作用 |
| --- | --- |
| `router.py` | 识别订单号、售后意图、是否需要 RAG、是否需要退款申请、是否需要风控、是否需要工单。最终输出 `RouteDecision`。 |
| `pending_task.py` | 处理槽位补全。比如用户说“帮我改地址”但没给订单号或新地址，就保存待补全任务，下一轮继续接上。 |
| `conversation_context.py` | 多轮上下文继承。比如上一轮提到订单号，下一轮用户只说“那可以退款吗”，这里会尝试继承最近订单和问题。 |
| `memory.py` | 会话记忆管理。负责读写聊天历史和 pending task，并接入 Redis/本地 TTL 缓存。 |

想改“什么话触发什么意图”，主要看 `router.py`。
想改“缺什么信息要追问”，主要看 `pending_task.py`。

## 4. 工具执行层

| 文件 | 作用 |
| --- | --- |
| `tool_registry.py` | 工具白名单。所有可调用工具必须在这里注册 schema 和 handler，防止 Agent 越权调用不存在或危险工具。 |
| `tool_executor.py` | 工具链执行器。按照路由结果顺序调用订单查询、RAG、风控、退款申请、工单、人工审核、转人工等工具。 |
| `tool_validation.py` | 工具链校验。执行前后检查工具顺序是否合法，比如退款申请前必须先查订单和做风控。 |
| `tool_results.py` | 工具结果辅助函数。负责按工具名取结果、判断订单查询失败、政策检索失败、系统工具异常等。 |


## 5. 业务规则与安全控制层

| 文件 | 作用 |
| --- | --- |
| `ticket_policy.py` | 工单创建规则。判断保修检测、物流异常、地址修改、支付异常、投诉升级等是否允许创建工单。 |
| `fallback_policy.py` | 离线确定性回复。未开启真实 LLM 或必须走规则兜底时，用这里生成稳定回复。 |
| `guardrails.py` | 输入安全检查。处理提示词注入、高风险动作等基础安全规则。 |
| `evidence_guardrail.py` | RAG 证据校验。判断检索到的知识来源和关键词是否足够支撑后续业务动作。 |

想改“什么情况必须人工审核/不能继续自动处理”，主要看 `ticket_policy.py`、`risk_agent.py`、`evidence_guardrail.py`。

## 6. 回复与模型上下文层

| 文件 | 作用 |
| --- | --- |
| `prompt_builder.py` | 把订单信息、RAG 证据、退款申请、人工审核、工单结果整理成 LLM messages。 |
| `fallback_policy.py` | 不调用 LLM 时生成本地规则回复，也负责工具失败时避免谎称业务已完成。 |
| `response_builder.py` | 预留文件，目前为空。后续如果要把回复拼装从 `fallback_policy.py` 拆出来，可以放到这里。 |

想改“最终怎么回答用户”，优先看 `fallback_policy.py` 和 `prompt_builder.py`。

## 建议阅读顺序

1. `agent_core.py`：看 API 怎么进入 Agent。
2. `workflow.py`：看一次请求的主节点。
3. `orchestrator.py`：看多 Agent 如何分派。
4. `router.py`：看意图和工具计划如何生成。
5. `tool_executor.py`：看工具链如何真正执行。
6. `fallback_policy.py`：看最终规则回复如何生成。

## 常见修改入口

| 需求 | 主要修改位置 |
| --- | --- |
| 新增一个售后意图 | `router.py`、`query_builder.py`、`evidence_guardrail.py` |
| 新增一个业务工具 | `app/tools/support_tools.py`、`tool_registry.py`、`tool_executor.py`、`tool_validation.py` |
| 修改退款判断 | `after_sales_agent.py` |
| 修改风控规则 | `risk_agent.py` |
| 修改工单资格 | `ticket_policy.py` |
| 修改追问槽位 | `pending_task.py` |
| 修改回复文案 | `fallback_policy.py` |
| 修改 LLM Prompt | `prompt_builder.py` |
| 调整流式返回 | `stream_runner.py` |

## 设计边界

- `router.py` 只判断“需要做什么”，不直接执行业务动作。
- `tool_executor.py` 负责“按合法顺序执行工具”，不直接写具体业务 SQL。
- 具体业务动作在 `app/tools/support_tools.py` 和 `app/services/` 中实现。
- 高风险动作必须经过风控、工具链校验和人工审核机制。
- RAG 证据不足时，不能继续创建自动业务动作。
