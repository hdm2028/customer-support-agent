# Answer / E2E Dev 扩充 B01

已追加 Answer 16 条（4→20）、E2E 12 条（12→24），覆盖 18 个跨层业务场景。原 4/12 条的 query、expected、顺序和字节保留；业务代码、模型配置默认值、RAG 数据、Schema 和 evaluator 未修改。新增样本属于 Dev，不用于声称泛化准确率。

## 一次隔离实测结果

|层|旧案例|新增 B01|当前完整 Dev|
|---|---:|---:|---:|
|Answer|2/4|11/16|13/20|
|E2E|7/12|10/12|17/24|

均为原 evaluator 的通过数。旧案例失败名单与上一轮保留版本一致，仍包含字面 `MQ`/`知识库`、否定句及旧工具路径冲突。本轮只增加数据，没有可以归因于代码优化的收益；不得用 13/20 对比 2/4 或用 17/24 对比 7/12 宣称提升。

[results_by_cohort.json](results_by_cohort.json)保存旧/新/全部各自原口径指标、全部 44 案状态、失败原因与 Trace 行号。两套 evaluator 均因剩余失败退出 1；runner 退出 0 仅表示编排完成。

|新增失败|已观察到的原因|类型与边界|
|---|---|---|
|Answer 004 / E2E 003|修改地址请求的模型辅助主题为非法 `order_lookup`，严格校验回退到 general_support/unknown，只查订单，丢失新地址澄清|真实模型输出契约问题；[Answer Trace 第 8 行](live/answer/trace.jsonl)、[E2E Trace 第 15 行](live/e2e/trace.jsonl)|
|Answer 010|发票修改咨询被标成 invoice_change；规则对该主题要求订单号、未启用 policy_search，直接追问订单号|咨询主题与槽位边界问题；Trace 第 14 行及 app/agent/routing/decision.py|
|Answer 012 / E2E 009|会员政策被归到 general_support/general_question，membership_policy 只在 related_topics；未查政策|真实语义路由问题；Answer Trace 第 16 行、E2E 第 21 行。即使 E2E 回答关键词检查通过，整个链路仍判失败|
|Answer 014|物流证据已检索到，包含签收人员和代收规则；问句“不申请退款”却触发 return_refund profile，物流来源被拒绝|已证实的证据后处理误拒绝，非候选召回缺失；Trace 第 18 行的 tool_result 和 evidence_guardrail，app/agent/policies/evidence_guardrail.py:detect_policy_profile|
|Answer 016|回答引用“不能将 PENDING、PROCESSING 或 MANUAL_REVIEW 描述为退款已经成功”，但缺字面“不能承诺”“完成”|新增断言也存在同义表达误判，不能据此认定真实错误承诺；Trace 第 20 行。原失败保留，列为后续评分契约复核项，本轮没有调低关键词或修改 evaluator|

E2E 003 的原始 `dangerous_tool_misuse` 记录的是本条禁止的只读 `order_lookup`，不是发生了资金操作；全批未观察到 forbidden 写工具调用。对无订单号案例，DB/MQ 为不适用，不把它们计成持久化安全检查通过。

实际记录 57 次模型调用、204,368 provider tokens，已记录调用失败数为 0；Answer 套件耗时 145.46 秒，E2E 173.07 秒。这是一次本地运行开销，不是压测或上线容量结论。运行期间源码指纹未变，候选及追加数据在运行后仍与冻结版本一致。

## 数据与可追溯性

- 当前运行入口：[Answer](../../data/eval/answer_eval.jsonl)、[E2E](../../data/eval/e2e_eval.jsonl)。ID 前缀为 `answer_dev_b01_` 和 `e2e_dev_b01_`。
- 生成前的[场景矩阵与覆盖分析](../../data/eval/design/answer_e2e_expansion_b01.md)先于候选落盘：Answer 14 个未覆盖场景、2 个弱覆盖场景；E2E 12 个未覆盖场景。不是通过同义改写堆数量。
- 候选原件：[Answer](../../data/eval/candidates/answer_dev_candidates_b01.jsonl)、[E2E](../../data/eval/candidates/e2e_dev_candidates_b01.jsonl)。在代码/政策来源审阅后追加；这是本轮来源核对，不是独立人工盲审。
- [case_sources.json](case_sources.json)逐案映射 scenario、政策章节和代码来源。回归条目为 Answer 006、015 与 E2E 005；没有无法确立 Ground Truth 而强行接纳的条目。
- [validation.json](validation.json)检查字段、类型、重复 ID、层内近似问句、工具与路由名称、矛盾断言、政策文件与章节、原行保留及冻结候选一致性。归一化问句 SequenceMatcher 阈值 0.80 下没有新增层内近似对；跨层共享问句用于检查同一场景的回答与业务行为，不算多个场景。

补充内容包括订单事实/不存在订单、查询缺订单号、修改地址缺新地址、无订单人工转接、退款撤销引导、未支付和支付待确认退款拒绝、发票资料与修改、进水保修限制、会员规则限制、发货后修改地址、签收争议、未知退款结果、人工审核期间的回答、未支付订单取消审核、保修执行缺订单号。

## 验收边界

- 新增回答不要求出现内部词 `MQ`、`知识库`，也不禁用会误伤正常否定句的宽泛词项。仍沿用原字面评分，关键词和引用命中不能代替完整语义评审。
- 旧标签的否定句、内部术语和工具顺序问题保留，没有利用扩充顺手改分。新增 `payment_pending` 案例按当前前置拒绝契约检查 `refund_decision`、无新退款行/消息，与旧“调用 refund_apply 后失败”契约分开。
- 退款撤销引导在提取订单号前返回，E2E 005 的 `expected_order_id=null`；本条验证无工具调用及引导回执，DB/MQ 原评分为不适用，不冒充撤销接口集成测试。
- 无订单人工转接成功时，原 evaluator 的最终动作仍归类 `reply_only`，本条另检查 `transfer_to_human.success` 和待认领回执。该标签不表示没有执行工具。
- 现有 E2E 不直接核对审核/转人工工单表或取消前后订单字段；新取消订单案例检查工具、审核回执及无退款副作用，更完整的状态断言由 `test_nonrefund_review_continuation.py` 等专项测试承担。
- 不新增伪造的 turns、fixture、fault 字段。多轮、支付故障/回调、管理员批准拒绝和生产身份需要相应运行器能力。本批没有修改它们。
- 新增执行案例避免七天、48 小时、保修到期等日期依赖，但旧套件仍共享套件级数据库且未冻结业务时钟。不要把完整运行等同于生产业务验收。

## 运行与复现

一次实测使用原 `run_isolated_baseline`：冻结源码、每套独立 SQLite、本地知识副本、进程内缓存、真实 `glm-4-flash` 路由和回答（业务强制模板/降级保留）。配置为既有 `hybrid_rule + complementary`、查询辅助主题容错；没有修改生产默认。预期在运行前固定，运行后不按分数改写。

```powershell
python -m scripts.eval.run_isolated_baseline --suites answer e2e --live-replies --record-semantic-responses --record-case-results --semantic-related-topics discard_invalid --policy-evidence complementary --output reports/answer_e2e_<新的运行名>
python reports/answer_e2e_dev_expansion_b01/validate_data.py
```

实际运行目录为 [live](live)，包含源码快照、配置、逐案结果、语义原始输出和 Trace。原五层历史报告保留，不与本次扩充后的整体分数直接比较。

## 回退

`manifest.json` 保存旧文件长度、旧内容指纹及新增候选指纹。本轮对两个 Dev 文件只有末尾追加；回退时先核对旧前缀及追加部分与 manifest/候选一致，再仅移除末尾新增的 16/12 行即可。不要还原其他历史工作区修改。
