# 简易 Agent Harness：任务拆解、执行与反馈

2026-09-11，已接入普通请求和现有 SSE 的共享业务编排入口。

## 借鉴与取舍

借鉴 Deep Agents 的任务清单、中间件和共享上下文设计：[官方概览](https://docs.langchain.com/oss/python/deepagents/overview)、[规划中间件说明](https://docs.langchain.com/oss/python/deepagents/customization)。本项目实现的是本地轻量 harness，**没有安装或调用 `create_deep_agent`**，也没有给客服 Agent 增加 shell、任意文件操作或动态创建子 Agent 的权限。

现有语义 Router 已能给出结构化意图，项目也已有业务 Agent、工具中间件、会话存储和 LangGraph。方案因此采用受约束的任务分解：由结构化路由生成任务，而非增加一次自由规划模型调用。任务范围是当前系统支持的售后业务，不是任意长任务规划器。

## 调用与实现

```text
用户输入
 → 原有会话窗口、待补槽任务、语义路由
 → AgentOrchestrator.run_agent_loop
 → AgentHarness：检查输入与计划，拆成有依赖的任务
 → 选择依赖已完成的任务，限定本任务可调用的工具
 → 业务 Agent → 工具中间件 → 数据授权与业务工具
 → 结果更新 AgentState；应用原有风控、证据和退款前置约束
 → 记录任务反馈，调整后续任务，继续或停止
 → 原有回答生成、会话保存、Trace
```

核心代码是 `app/agent/harness.py`。原来 Orchestrator 根据大量分支逐轮挑选 Agent，现在由 harness 的任务依赖决定下一个负责人。Orchestrator 继续负责调用业务 Agent、合并结果和应用业务规则，避免重新实现一套退款规则。

每个任务包含 `id`、业务 `objective`、`agent`、`depends_on`、`status`、`result_refs` 和 `reason`。结果引用指向当前请求的 `tool_results` 索引，计划不复制完整订单或政策正文。状态包括 pending、running、completed、failed、blocked、skipped。

上下文继续使用现有 `AgentState`：当前订单、政策、风控、审核、工具结果和历史消息；会话窗口仍由原有 8 条消息、30 分钟、8000 字符机制控制。新增 `harness` 字段保存任务状态、12 步预算和上下文摘要，摘要写 Trace 时复制，避免之后的任务更新改写历史快照。没有改变记忆存储或添加自动摘要模型。

## 任务分解与反馈示例

实际隔离运行“申请退款10009”拆为四项任务：

1. 售后 Agent 核实订单归属、支付和已有售后状态。
2. 客服 Agent 取得政策证据，依赖订单核实。
3. 风控 Agent 判断风险和人工审核要求，依赖前两项。
4. 售后 Agent 创建退款申请，依赖前面三项。

见 [example_run.json](example_run.json)，本次四步完成，回复仍明确“申请已受理，等待处理”。**计划 completed 表示本轮申请处理任务完成，不代表支付渠道已完成退款。**

反馈会改变计划，而非仅追加日志：

- 查到已有退款：沿用原业务前置判断，取消后续风险/新申请任务，记录 skipped 及原因。
- 风控要求审核：插入人工审核任务；审核提交后，自动退款及相关后续写任务被阻止，整体为 waiting_for_review。
- 无成功结果、Agent 没有推进任务：停止，不重复调度到预算耗尽。
- Agent 抛出未捕获异常：转换为失败工具反馈，阻止后续任务，由现有回答降级逻辑解释失败。
- 工具已返回结构化失败：保留原失败结果和 block_reason，不重复制造一条工具失败记录。
- 用尽 12 步预算：阻止剩余任务；不自动追加预算或重试写操作。

## 权限与安全

输入仍经过现有关键词护栏，证据仍经过现有证据护栏；本轮没有删除审计中列出的关键词规则，也没有声称这些规则能识别所有攻击。

新增 `task_tool_scope()`，通过 ContextVar 绑定当前任务的工具集合。在已有工具权限中间件中先检查任务范围，再检查 Agent 权限；内部 `execute_tool`、`safe_tool_call` 也受任务范围限制。嵌套范围只能取交集，异常退出后恢复原范围；现有超时线程会复制 context，因此范围随调用传播。

例如“查询订单”任务即便错误调用退款工具，也会在启动工具线程前被拒绝。“创建工单”额外允许现有只读 `ticket_decision` 前置校验。用户订单归属、管理权限、退款事务和幂等继续由现有安全及业务代码负责；harness 不凭任务计划授予用户权限。

## 输出与持久化边界

- 普通接口：`result.orchestration.harness` 可查看计划及结果引用。
- 现有 SSE：增加 `task_plan` 事件，发生在业务编排节点返回后，是本轮任务快照；不是逐个任务实时推送。
- Trace：增加 `harness_plan`、`harness_task_started`、`harness_task_result`、`harness_finished`。失败也会保留在最终任务快照中。
- 上一轮 SQLite 持久化原型会把计划随业务节点结果保存到 checkpoint，且已验证重启后保留。正式默认图仍未启用 checkpointer；任务仍在 `orchestrate_agents` 节点内执行，不能把它说成逐任务断点恢复或跨进程 exactly-once。

## 验证

新增 `tests/test_agent_harness.py` 10 项测试，覆盖依赖顺序、动态插入审核、已有退款跳过、普通审核等待状态、未捕获异常、无进展停止、步骤预算、输入拦截/补槽等待、任务工具越界及嵌套范围恢复。

在不含 `.env` 的隔离源码副本和 SQLite 中运行现有测试：**319 passed、11 skipped、106 subtests passed、3 个既有弃用警告**。见 [regression.txt](regression.txt)。11 项跳过不计为通过；没有据此宣称 MySQL/Redis/真实渠道验证完成。

首轮回归有 5 项失败，均涉及 harness 重复添加错误结果或覆盖旧失败原因；修复后完整回归通过，初轮日志保留在 `regression_initial.txt`。

普通 HTTP、SSE、强制结束进程后的恢复检查 **10/10 通过**，见 [持久化冒烟产物](../durable_graph_prototype/smoke_20260911_213024_68707d.json)。使用固定路由、本地知识检索和真实业务节点，没有调用真实模型/支付渠道，也没有运行五层评测或改变评分口径。
