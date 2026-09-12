# 支付与人工审核接入

当前没有真实支付适配器。`PAYMENT_ADAPTER` 未配置时支付提交/对账接口返回 503，系统不会模拟资金到账。

项目提供 `app.services.payments.PaymentGateway` 协议。部署方经审核的 `module:factory` 工厂应返回对象，实现 `submit_refund(request)` 和 `query_refund(request)`，均返回 `PaymentObservation`。工厂自行读取渠道凭据，不能把凭据放入观察结果、日志或回复。具体渠道的认证、验签、超时及实际退款 API 适配仍需按选定渠道完成。

提交必须使用 `RefundPaymentRequest.idempotency_key`（由 refund_id 稳定生成），同时核对订单、金额和币种；默认币种 CNY，可由 PAYMENT_CURRENCY 配置。提交超时、连接中断或本地落库失败后，退款进入 refund_unknown。此时只能先查询，不能用新幂等键重新发起退款。适配器查询结果应携带同一退款 ID、金额、币种，成功必须有渠道凭证。冲突终态需要人工核实，不静默覆盖。

首次创建退款和首次提交支付均要求订单 `payment_status=paid`，且订单不处于待支付/待付款。创建时在订单行锁内再次读取并核验，和退款及 MQ 事件在同一事务内；支付提交在退款行、订单行锁内核验后才记录提交状态。未支付、支付确认中、失败或缺失状态不能发起新的退款资金操作。已经发出的退款仍可查询与对账，不能因为后来的付款状态变化而中断结果恢复。该前置检查依赖本地订单记录，真实支付渠道的付款凭证核对仍由接入协议完成。

管理员接口：

- `POST /manual-reviews/{id}/resolve`，JSON `{"decision":"approve"或"reject","note":"实际核实说明"}`。
- `POST /admin/refunds/{id}/submit-payment`：仅 refund_processing 可首次提交。
- `POST /admin/refunds/{id}/reconcile-payment`：主动查询渠道结果。
- `GET /refunds/{id}`：用户只能查看自己的申请，管理员可查看。

人工批准本身不代表退款或到账成功。批准退款类型审核单后申请进入 queued，退款 Worker 将其推进到 refund_processing，再由具备权限的支付处理环节提交。可用 `python -m scripts.maintenance.refund_worker` 持续消费，或由管理员调用 `/refund-tasks/process` 处理一批。部署 Compose 已包含独立 worker；启动、停止和异常退避见 [恢复与发布说明](recovery_and_release.md)。Worker 不提交支付，真实支付渠道及生产调度运维仍需接入。

取消订单和修改地址的执行请求，只要语义路由已识别相应意图，就先查询订单、检查风险并创建 `cancel_order` 或 `address_change` 审核，不依赖固定措辞。低风险请求仍需通过该审核入口续办，查询咨询不创建审核。管理员批准时重新检查当前订单是否仍允许变更；修改地址必须填写已核实的新地址。未支付取消不生成退款，已支付取消创建待处理退款及消息，均不表示资金已到账。申请后已发货的订单不能通过此接口直接取消或改址。

审核补充说明：用户或管理员可调用 `POST /review-supplements`，提交 `review_id`、`text`（1–2000 字）和 `submission_id`（8–100 位字母、数字、下划线或连字符）。普通用户只能提交自己的审核单。客户端重试同一次提交应沿用编号；同一身份、编号和内容只保存一次，复用编号提交不同内容会返回 409。网页提供“补充审核材料”入口，支持文字说明；聊天复用待审核单时也会保存本轮补充及实际对话/政策上下文。

管理员通过审核详情读取 `supplement_version`，在批准/拒绝请求中传回该值。若打开页面后又有新材料，旧版本决定返回 409，需要重新查看；没有补充材料的旧审核仍兼容原请求格式。有补充材料时不能省略版本。决定记录会保存其依据的材料版本。审核完成后的补充作为后续记录保存，不重开审核、不改写决定或自动触发退款。附件上传尚未实现。

测试：`tests/test_payment_reconciliation.py` 使用注入的测试适配器验证超时恢复、金额核验、幂等和本地 DB 故障；`tests/test_review_resolution.py` 验证批准/拒绝和审计。它们不能替代真实支付沙箱的联调与验签测试。

RAG 可选联调：显式设置 `RAG_EVIDENCE_SELECTION=complementary` 才启用客服政策 Top20/Top5 选择器及完整 rerank query 传递。默认不切换。模型上下文已验证包含全部所选证据，最终生成回答覆盖尚待单独验收。
