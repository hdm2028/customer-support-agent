# Answer / E2E Dev 扩充 B01：生成前覆盖分析

本轮仅扩充开发集，沿用现有 evaluator 和字段。生成前 Answer 有 4 条，E2E 有 12 条，主要覆盖退款。旧行及其 expected 保留；先生成独立候选、核对来源和结构，再追加到原 Dev 文件。候选是否暴露系统失败不是删除案例或放宽标签的理由。

## 当前可执行标准

- Answer：`id/query/expected_keywords/forbidden_keywords/require_citation/notes`。关键词采用字面匹配；引用必须出现在本次 `policy_search` 结果及回答中；`llm_judge_metrics` 未启用。
- E2E：`id/intent/user_message/expected_order_id/expected_route/expected_tools/expected_tool_success/expected_final_action/forbidden_tools/must_include/must_not_include`。可选 `notes` 用于记录来源。检查实际工具结果（包括内部 `refund_decision`），不是只检查最初工具计划。
- E2E 的 `reply_only` 只是现有最终动作分类：普通查询和无订单转人工也可能归入它，不能解释为没有执行工具。工具和结果另行断言。
- E2E 的 DB/MQ 检查仅覆盖退款行与退款消息；人工工单和审核单落库需要专项集成测试，不能根据该字段声称全部副作用已验证。
- 现有 skill 的 JSON Schema / Validator 仅适用于 RAG。本轮按 Answer/E2E 消费代码检查数据，不修改 RAG Schema 或 evaluator。

## 场景矩阵

下表在生成候选前确定。A/E 的覆盖状态按各自旧数据判断；“—”表示该层本轮不扩充。每行是不同业务条件或执行边界，不将同义改写单独计为新场景。

|scenario_id|场景 / capability|类别|优先级|旧 Answer 覆盖|旧 E2E 覆盖|预期行为|知识源 / section；Ground Truth|
|---|---|---|---|---|---|---|---|
|AE01|已发货订单事实查询 / order_lookup|happy_path|P0|uncovered|uncovered|只查订单、回答真实商品与状态，无退款写入|无政策源；data/orders.json 的 10011；app/tools/order.py；app/agent/response/grounded.py|
|AE02|不存在订单的事实回答 / order_lookup|negative|P0|uncovered|—|明确核对订单，不编造商品或成功状态|无政策源；data/orders.json 不含 99998；app/agent/response/fallback.py|
|AE03|查询订单但缺订单号 / slot_clarification|ambiguity|P0|uncovered|uncovered|先追问订单号，无工具写入|商品售后规则.md / 信息不足；app/agent/routing/decision.py、pending_task.py|
|AE04|修改地址有订单号但缺新地址 / slot_clarification|boundary|P0|uncovered|uncovered|追问新地址，暂不创建审核、工单或修改地址|订单取消与修改政策.md / 修改收货地址流程；app/agent/routing/pending_task.py|
|AE05|无订单号的人工转接 / handoff|happy_path|P0|uncovered|uncovered|创建转人工工单，说明等待认领，不声称问题解决|无政策源；app/tools/human_review.py；app/agent/response/grounded.py 的 handoff_receipt；tests/test_human_handoff.py|
|AE06|已受理退款的撤销引导 / refund_withdrawal|regression|P0|uncovered|uncovered|提供选择已有申请的撤销入口，当前聊天不撤销、不新建退款|无政策源；app/agent/routing/refund_withdrawal.py；tests/test_conversation_task_boundaries.py|
|AE07|未支付订单要求退款 / refund_precondition|negative|P0|uncovered|—|说明尚未支付，无退款申请和资金操作|退款政策.md / 退款申请前置条件；data/orders.json 的 10005；app/domain/refund_policy.py|
|AE08|扣款待确认时要求退款 / refund_precondition|boundary|P0|uncovered|uncovered|先核实支付结果，前置拒绝新退款，无退款行或消息|支付与发票政策.md / 已扣款但订单显示未支付；app/storage/database.py 的 10007 payment_pending；app/agent/orchestrator.py|
|AE09|企业发票资料咨询 / invoice_policy|happy_path|P1|uncovered|uncovered|解释企业名称、抬头、税号要求，查询不升级执行|支付与发票政策.md / 发票抬头；app/agent/routing/decision.py|
|AE10|已开票后的修改规则 / invoice_policy|boundary|P1|uncovered|—|说明已开票后的修改或重开流程，不声称已修改|支付与发票政策.md / 修改发票信息|
|AE11|人为进水的保修限制 / warranty_policy|negative|P1|uncovered|uncovered|解释免费保修范围限制，需要政策依据，无检测结论或换新承诺|保修政策.md / 不属于保修范围的情况、质量检测|
|AE12|会员等级不豁免检测 / membership_policy|hard_negative|P0|uncovered|uncovered|解释会员不能跳过检测、退款资格和风险规则，查询不升级执行|会员权益政策.md / 售后权益限制、直接换新|
|AE13|发货后的地址变更咨询 / address_change_policy|boundary|P1|uncovered|uncovered|说明平台原地址修改限制、物流实际能力，咨询无需新地址槽位|订单取消与修改政策.md / 已发货后修改地址；app/agent/routing/decision.py|
|AE14|显示签收但本人未收到 / shipping_policy|boundary|P1|uncovered|—|保留订单事实，提供签收人和代收核实规则，不认定本人已收货|物流配送政策.md / 已签收但用户未收到；data/orders.json 的 10008|
|AE15|退款结果不明确时能否再次退款 / refund_policy|regression|P0|weakly_covered|—|说明先查询实际退款状态或人工核实，不自动重复执行|退款政策.md / 退款失败；app/services/payments.py|
|AE16|人工审核期间如何说明结果 / refund_policy|boundary|P0|weakly_covered|—|说明人工审核尚未完成，不能承诺退款完成|退款政策.md / 退款人工审核、退款处理通知；人工审核SOP.md / 审核期间|
|AE17|取消未支付订单的审核申请 / cancel_order|happy_path|P1|—|uncovered|进入取消订单审核与工单，不直接改变订单或发起退款|data/orders.json 的 10005；app/agent/routing/decision.py；app/agent/agents/after_sales.py；tests/test_nonrefund_review_continuation.py|
|AE18|申请保修检测但缺订单号 / warranty_repair|ambiguity|P0|—|uncovered|执行请求必须先补订单号，无检测工单、退款或人工审核写入|保修政策.md / 保修处理方式；商品售后规则.md / 通用规则；app/agent/routing/decision.py|

计划新增 18 个跨层场景，映射到 Answer 16 条、E2E 12 条。Answer 目标中 14 个 uncovered、2 个 weakly_covered；E2E 目标中 12 个 uncovered。AE06、AE15 属于回归类型，其余为新场景或边界补充。

旧集已覆盖的通用退款政策、到账时间、退款缺订单号、无此订单退款、高额及绕过审核退款、注入拦截不再通过改写问句重复扩充。未支付退款在 E2E 旧集已有案例，本轮不再新增同义例；只补之前没有的 payment_pending 边界。

## 本轮不声称覆盖的能力

- 多轮补槽、切换订单、取消待办：现有 JSONL runner 每案只发送一条消息，没有 turns 或可执行状态夹具字段；现有专项测试继续负责，不伪造字段。
- 渠道超时、重复回调、审核批准/拒绝后续办：需要故障注入或管理 API 驱动，不能由单条客户消息构造。
- 动态七天退货、48 小时物流、保修到期：旧套件未冻结时钟；本轮新增执行路径避免依赖这些日期边界。AE14 只查询既有签收事实，不计算超时时长。
- 生产身份、真实支付渠道、MySQL/Redis 故障不属于本批 JSONL 验收。

## 标注约束

- 候选与正式 Dev 都使用现有字段，scenario 与来源放入 notes 并另存映射；线上选择逻辑不会读取它们。
- 不把 `MQ`、`知识库` 等内部术语作为用户回答必需词。
- 不禁用可能出现在正确否定句中的宽泛词项，例如 `立即到账`、`免费保修`、`退款成功`。只选择具体错误执行回执，仍记录字面评分不能完全理解否定的局限。
- 政策关键词是原评分能检查的最低信息要求，不能冒充完整语义判定。原文条件是否完整保留仍有度量缺口。
- 业务拒绝通过 `expected_tool_success=false`（适用时）、前置 `refund_decision` 及退款 DB/MQ 无新增验收，不要求被拒绝操作返回伪成功。
- 新增候选在运行前冻结预期。运行发现的系统失败、评测盲区或依赖故障分开记录，不据得分改写 expected。
