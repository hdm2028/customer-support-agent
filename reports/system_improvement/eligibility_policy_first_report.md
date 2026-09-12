# 退款资格咨询先查政策，再补订单

本轮修复无订单的退款资格咨询被整体阻断的问题。咨询先检索并展示政策原文，同时保留补订单任务；补号后继续原咨询，只有明确转为执行请求才进入退款申请流程。未部署。

## 定位与最终行为

现有工具 Dev 的 `tool_policy_002` 已被语义路由识别为 `return_refund / query / refund_eligibility`，`need_policy=True`。但缺少订单号会设置 `need_clarification=True`，随后工具计划、槽位处理和编排入口都停止所有工具。因此旧结果只有索要订单号的回复，没有执行政策检索。

最终代码保留原订单要求及 `need_clarification`，仅对无订单、无风险/审核/转人工/写操作标志的退款资格咨询开放 `policy_search`。编排器只调用一次客服政策 Agent，随后结束本轮，不进入业务执行循环。同步与 SSE 均使用确定性回复，展示实际取得的政策片段并说明尚未核实具体订单资格；政策失败时说明证据不可用。真正退款执行缺订单时仍不运行工具。

待补充记录保存本轮咨询。用户补充纯数字或“订单号是……”后沿用原问题，包括“买回来五天了，现在还能退吗？”这类没有完整业务关键词的句子。新的执行指令或新问题仍替换旧待补充任务；咨询不会承接先前已被替换的退款执行请求。

业务修改为四个文件：`routing/pending_task.py` 的统一许可条件、`routing/router_v2.py` 的工具计划、`orchestrator.py` 的单次只读调度、`response/fallback.py` 的规则与补号回复。没有更改语义模型、退款业务判断、数据、知识库、索引、Schema、Validator 或 evaluator。

## 固定 Dev 对照

A：`review_supplements_dev_replay`。B：[eligibility_policy_first_dev_replay](eligibility_policy_first_dev_replay/manifest.json)。两侧 80 次首轮语义输入与响应逐字一致并消费完毕，配置、开发数据、知识库、订单及 evaluator 字节一致。B 无真实模型调用；原评分函数和分母计算方式不变。

|原指标组|A|B|
|---|---:|---:|
|路由 Dev|23/24|23/24|
|工具 Dev|42/55|43/55|
|回答 Dev|1/4|1/4|
|端到端 Dev|7/12|7/12|

唯一新增通过为 `tool_policy_002`，没有新增失败案例。最终回复实际包含定制商品不支持七天无理由的规则、质量问题检测路径及七天无理由的条件，最后提示补订单；没有声称该用户已经符合退款资格或创建申请。危险工具误用计数仍为 0。

工具选择准确率 0.8364 → 0.8545、参数准确率 0.7647 → 0.7714、执行成功率 0.88 → 0.90；其他原标量指标不变。完整逐案工具/检查字段、剩余失败集合和目标案例原文见 [A/B 结果](eligibility_policy_first_ab.json)。四套数据独立报告，不合并总体分数。

B 四组本地回放耗时分别为 0.50、3.06、1.49、1.93 秒。新增咨询会实际执行政策检索，不能用这些回放耗时推断线上延迟。回答与端到端仍有未解决失败，本轮没有提高其分数。

## 测试与保留的失败

- [最终完整隔离检查](release_checks_eligibility_policy_first_final/manifest.json)：**269 passed、11 skipped、61 subtests passed**，pytest 9.73 秒，已有 3 条弃用警告。新增六项测试覆盖同步/SSE、启用生成配置时仍使用原文、补订单后再执行、咨询替换旧执行任务、缺订单执行阻断、政策失败及多种补号格式。
- [专用 MySQL 新增流程检查](eligibility_policy_first_mysql.txt)：**6 passed、9 subtests passed**，5.15 秒；每项创建随机测试库并清理。25 项未选入该专项；本轮没有重新宣称完整 MySQL 回归，上一阶段完整 MySQL 103 项记录保留。
- [修改前测试](eligibility_consultation_before.txt)复现政策工具未执行，以及直接依赖普通历史关键词合并时部分订单表达无法续接。
- 初版曾移除资格咨询的待补订单状态，[其 Dev 回放](eligibility_consultation_dev_replay/manifest.json)为路由 19/24、工具 43/55、回答 1/4、端到端 7/12。四条路由退化，且会失去非关键词式问题的可靠续接。这版未保留；最终实现保留原澄清状态，只开放政策读取。[初版检查日志](release_checks_eligibility_consultation/unit_integration.txt)另有两个直接流式测试因新增 route 依赖而失败；该依赖随初版撤回，原测试未改写，最终全部通过。

当前代码可用 [补丁](eligibility_policy_first.patch)相对 `release_checks_worker_final` 回退，反向应用检查通过。测试/Dev 冻结快照中 `decision.py` 只有换行差异；最终工作区已恢复上一阶段原字节，功能和订单要求均未改变。其余当前源码与最终测试快照一致，差异核验记录在 A/B JSON。

没有运行 validation/final_test，没有启用 RAG 实验或新增评测案例。真实支付、生产身份、外部告警、浏览器验收及其他 Dev 失败仍独立跟踪；完整项目尚未完成验收。
