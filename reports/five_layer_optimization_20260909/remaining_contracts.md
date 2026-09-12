# 剩余失败的处理边界

原始 Dev 文件、expected 和 evaluator 均保留。以下是原因核对，不是重新计分，也不表示这些案例已经通过。

|案例|已观察到的行为 / 冲突|下一步所需证据或改动|
|---|---|---|
|routing_refund_002|售后执行主意图正确，topic 为 return_apply，标签为 refund_apply|明确未区分退货/退款的请求应采用哪个业务主题，再独立验证路由改动；不能凭主目标正确宣称整案通过|
|tool_boundary_complaint_only|模型把明确投诉、否认退款识别为 complaint/query；只查询订单|真实语义错误；需验证否定范围和多意图识别，保留“咨询不得升级执行”的约束|
|tool_boundary_complaint_refund|模型主意图为 complaint，辅助主题错误使用 intent 名 return_refund|最终版本严格回退，不再从不完整执行语义恢复工单写操作；语义未识别成功仍是失败|
|tool_order_005、tool_boundary_not_found_refund|无此订单，查询工具正确返回失败；旧执行成功检查要求 expected 工具 success=True|需要能表达“预期业务拒绝”的版本化验收契约，不能返回虚假的成功或伪造订单|
|tool_contrast_refund_002|10001 的签收日期为 2026-08-01；本轮运行已超过 7 天，退款被资格规则拒绝|固定业务时钟和具备明确相对时间的测试夹具；不得修改生产日期判断来通过旧案例|
|tool_contrast_quality_002|词项“质量太差”未变成“质量问题”；同时存在退款执行失败和套件内已有审核状态|参数词项与业务资格分别验证；不能把质量投诉直接当作检测结论。先解决按案例隔离与日期漂移，避免因前案审核记录误判根因|
|tool_contrast_progress_002、tool_boundary_duplicate_refund|售后流程进行中，前置决策阻止重复退款，实际调用含 refund_decision|保留幂等与拒绝重复申请的安全边界；旧工具序列需按业务契约单独迁移|
|tool_boundary_unpaid_refund、tool_boundary_payment_pending_refund|未支付/支付待确认，refund_decision 阻止退款创建；旧序列没有此工具|保留支付前置检查；另有 lexical_query 固定词项断言，不等于政策正文未命中|
|tool_ticket_shipped_address|请求未给新地址，先澄清；旧标签要求创建审核和转人工|确认缺槽时的只读查询与澄清契约；不能带缺失地址进入写操作|
|tool_ticket_pending_cancel|创建 cancel_order 类型审核，工单高优先级；旧标签为 risk_control、normal，顺序也不同|应依据取消订单续办的真实业务需要审定，不能为匹配旧标签退回无类型审核|
|answer_refund_002|原禁词检查命中“……不代表资金一定立即到账”中的子串|已提供独立否定句诊断；原分数保留|
|answer_refund_004、e2e_refund_003|已给政策来源引用，但没有字面“知识库”|引用存在与字面用词分开核查；不伪造缺失证据，也不把词项修正包装成质量提升|
|e2e_refund_001|退款申请已入队，回复准确说明未到账；仅缺字面“MQ”|以真实申请状态及 DB/MQ 记录验收；客户回复无需暴露内部队列术语|
|e2e_refund_007、008|未支付/已有售后，前置拒绝，无新退款或消息；旧标签要求 refund_apply 调用后失败|保留安全前置拒绝，未来契约应检查无副作用及业务解释|
|e2e_refund_009|取消订单流程产生对应审核/工单，旧标签按退款流程验收|按当前取消订单业务边界确认契约；不能把取消订单转换成退款执行|
|rag_dev_candidate_b01_003|必要的退款资格证据不在 Top20；primary target 已在候选中|独立召回实验；本轮互补选择不能恢复不存在的证据|

核对位置：`app/domain/refund_policy.py` 的支付、重复申请、日期判断；`app/agent/agents/after_sales.py` 的前置决策与审核类型；`app/agent/routing/pending_task.py` 的槽位处理；`scripts/eval/eval_tools.py` 的执行成功与工具序列检查；`scripts/eval/eval_e2e.py` 的路由和字面回复检查。完整结果和 Trace 见本目录 `live_final/summary.json`。

这些问题尚不足以支持自动上线。尤其不能将旧断言冲突统一改记“通过”，也不能把所有失败都归因于数据：两个投诉语义错误和 RAG 003 召回缺口仍是真实未解决项。
