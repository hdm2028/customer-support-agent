# 五层 Dev 实测（2026-09-09）

本次运行 Routing / RAG / Tool / Answer / E2E 五层既有评测，使用实际模型调用与隔离数据库。原评分条件、数据和业务代码不变。运行产物和逐案分数保留；完整状态以本目录 `manifest.json`、`summary.json` 为准。

## 配置与评测口径

- 模型 `glm-4-flash`，temperature=0.2；实际语义路由，未回放旧响应。Answer/E2E 启用 `use_llm=True`，业务强制模板和无证据回复仍然生效。
- 每套业务测试独立 SQLite、演示订单、进程内缓存、知识库副本；应用启动时在副本建立索引，没有触碰现有业务库或生产索引。
- RAG 为当前默认规则方案 `hybrid_rule`，Fixed256 / overlap 32、本地 local_hash_v1 / 256、权重 0.62/0.28/0.10。互补选择器未启用。
- 独立 RAG 测量固定 Top20→Top5。Answer/E2E 沿用真实政策工具调用参数；本次 Answer 的政策查询实际返回 Top2，不能把独立 Top5 覆盖率视为最终回答覆盖率。
- RAG 20 条旧 Dev 与 6 条新 Dev 单独统计，不与此前 60 条 V2 Holdout 混用。其他四层是既有开发案例。没有生成案例、修改 expected，或运行 Holdout/validation/final_test。

## 结果

五层评测已完整运行，质量门禁未通过。各层单独统计，没有计算跨层“总体准确率”。

|层 / 数据|通过数|通过率|关键指标|
|---|---:|---:|---|
|Routing，24 条|23/24|95.83%|意图、动作均 100%；主题 95.83%；派生路由字段 100%|
|RAG，V1 Dev 20 条|13/20|65.00%|Hit@5=100%；MRR=0.9667；业务类别覆盖=90.00%|
|RAG，V2 Dev 6 条|2/6|33.33%|Hit@5=83.33%；MRR=0.7500；必要证据 11/19，逐案覆盖宏平均=65.00%|
|Tool，55 条|42/55|76.36%|选择 46/55=83.64%；参数 26/34=76.47%；执行 44/50=88.00%；危险工具误用=0|
|Answer，4 条|1/4|25.00%|关键词正确性/完整性 50%；相关性 100%；引用检查 2/3；禁用词触发 1/4|
|E2E，12 条|7/12|58.33%|路由检查 75%；工具检查 75%；数据库/MQ 的适用案例检查均 100%|

工具参数与执行指标使用原 evaluator 的可评分案例作为分母；`N/A` 没有临时当作通过。RAG 两种覆盖口径也没有混算。V2 候选证据覆盖宏平均为 91.67%，不是“所有必要证据都已召回”；003 的缺口仍存在。

实际回复模式：Answer 为 grounded_llm 2 条、rule_fallback 1 条、grounded_no_evidence 1 条；E2E 为 grounded_llm 3 条、rule_fallback 8 条、grounded_no_evidence 1 条。启用模型执行不代表每条最终回复都由模型生成。

五层运行约 355 秒。已记录 80 次语义模型调用及 5 次生成调用；61 次完整 Trace 中的模型调用有 provider usage，合计 **185839 tokens**。另外 24 次直接 Routing 调用保存了响应与耗时，但没有 provider usage，因此这个 token 数不是完整账单。单次工作流平均耗时：Tool 已追踪请求约 5729 ms、Answer 6718 ms、E2E 5247 ms；独立 RAG 两批平均检索加排序为 4.16 / 5.63 ms，不能当作生产负载 p95。

没有观察到网络、鉴权或模型调用异常；有 **4 个案例发生模型输出格式回退**：Tool 3 个，Answer 1 个。Tool 的其中 1 个仍通过原评测，所以不能把格式回退数直接当作失败数。

逐案分数、首个失败检查、依赖分类及 Trace 文件/行号见 [summary.json](summary.json)。Routing 保存 24 条步骤记录；Tool 的 41 条工作流案例、Answer 4 条和 E2E 12 条均可关联完整 Trace。Tool 另 14 条权限案例不产生完整 Agent Trace。

## Trace 已确认的问题

### Routing

`routing_refund_002`：模型将“这个东西我不要了”的主题判为 `return_apply`，期望为 `refund_apply`；意图、动作和后续路由字段均通过。定位为语义主题不一致，不是服务不可用。原始响应与全部 24 案路由记录见 [Routing 产物](routing_tools/routing_v2/reports/case_results.jsonl)。

### RAG

当前默认路径的 V2 必要证据为 11/19。002、004、005 在候选中存在可匹配证据，但 Top5 没有完整保留；003 的退款资格必要证据未进入候选，不能把它写成 primary_target 缺失。完整候选及排名见 [V2 检索记录](rag/v2_dev6.json)。本次未启用选择器，不引用其此前 18/19 作为本次系统结果。

### Tool

13 条失败中存在多种原因，不能仅按“工具没有调用”统一解释：

- `tool_policy_002`、`tool_boundary_complaint_refund` 出现非法关联主题，模型响应成功返回但枚举校验失败，回退路由遗漏政策/风险等工具。`tool_order_004` 也发生格式回退，不过原案例仍通过。
- `tool_boundary_unpaid_refund`、`tool_boundary_duplicate_refund`、`tool_boundary_payment_pending_refund` 等案例的实际链路包含 `refund_decision`；与原精确工具序列及参数断言不一致。保留原失败，同时记录当前业务前置检查的影响。
- `tool_ticket_pending_cancel` 的实际审核类型为 `cancel_order`、工单优先级为 high，原断言期待 risk_control / normal，且工具顺序不同。
- 部分未知订单/不满足退款条件的案例在执行成功断言上失败；需要结合工具结果理解，不能据此自动要求业务拒绝改为执行成功。

与上一轮固定语义响应回放的 43/55 相比，本次实际模型调用为 42/55，唯一通过状态改变的是 `tool_policy_002`，本次返回了非法 `return_policy` 关联主题。这不是本轮修改业务代码导致的退化，也不能将不同模型响应下的分差当成代码 A/B。

全部原因及实际工具参数见 [Tool 结果](routing_tools/tools/reports/eval_tools.json) 和 [Tool Trace](routing_tools/tools/trace.jsonl)。

### Answer

|案例|实际失败与证据|定位|
|---|---|---|
|answer_refund_001|回答缺少期望关键词“订单”“不能”；实际 policy_search 返回 2 个 chunk，主要为异步退款/失败/到账及换新/退款引用内容|证据提供与最终回答内容检查；不能只依据独立 RAG 的 Top5 命中得出回答完整|
|answer_refund_002|回复原文为“平台完成退款操作不代表资金一定立即到账”，评分器仍因包含“立即到账”失败|禁用词检查不识别否定语境；保留原失败分数，不将该例描述成模型承诺立即到账|
|answer_refund_004|模型返回非法关联主题 `return_refund`，产生 `ValueError: Invalid related topic`；路由回退为 general_support，只调用 order_lookup，没有 policy_search|模型输出格式/枚举约束失败→缺少政策证据→引用检查失败；不是网络或鉴权故障|

对应 Trace ID：001 为 `26cf5cf5-2f8b-472c-ad90-fcea0fdc86cd`，002 为 `92148ce1-39fe-479a-bcd8-90db70f773d2`，004 为 `f9e0d11d-256a-49f9-8807-6ab658ba4213`。见 [Answer Trace](answer_e2e/answer/trace.jsonl)。

Answer 的 correctness/completeness 为关键词检查，faithfulness 为引用字符串检查，hallucination_rate 为禁用词出现率。它们不是人工语义质量判断；本次只有 4 案，没有另外调用 LLM Judge，也没有修改评分器处理否定句。

### E2E

|案例|第一个失败检查|Trace / 逐案结果支持的说明|
|---|---|---|
|e2e_refund_001|ANSWER_FAILURE|工具、风险、退款、数据库/MQ 检查通过；最终回复缺少断言要求的“MQ”|
|e2e_refund_003|ANSWER_FAILURE|上游检查通过；回复缺少“知识库”|
|e2e_refund_007、008|ROUTING_FAILURE|实际走 order_lookup→refund_decision→policy_search，未调用断言期待的 risk_check/refund_apply；应结合当前退款前置规则分析，不能直接认定应恢复退款执行|
|e2e_refund_009|ROUTING_FAILURE|实际进入订单取消审核路径，调用 order_lookup、risk_check、create_manual_review、create_ticket；期望的政策/退款链路缺失|

保留全部下游检查，不把一次上游失败引起的多个失败项统计为多个案例。[E2E 逐案结果](answer_e2e/e2e/reports/eval_e2e.json)、[E2E Trace](answer_e2e/e2e/trace.jsonl)。

## 自动化与复现

```powershell
python -m scripts.eval.run_five_layer_eval --output reports/five_layer_<新的运行名>
python -m scripts.eval.summarize_five_layer_eval --directory reports/five_layer_<新的运行名> --fail-on-quality
```

`--fail-on-quality` 在任一原评测案例失败时返回 1。程序运行完成不等于业务评测全部通过。使用说明见 [五层评测文档](../../docs/five_layer_evaluation.md)。

新增统一隔离入口、逐案结果记录器和结果汇总脚本；原 evaluator 评分代码未修改。只给原隔离运行器增加了可选的逐案记录开关。应用源码、知识、数据的实际运行版本保存在 [源码快照](source_snapshot.zip)，输入指纹见 [manifest.json](manifest.json)。没有配置远端 CI 的模型凭据或触发 GitHub Actions。

本轮验证范围不包含真实支付渠道、生产身份配置、HTTP 层授权、生产 MySQL/Redis、负载测试或多轮专项能力。它是当前五层 Dev 基线，不能替代这些验收。
