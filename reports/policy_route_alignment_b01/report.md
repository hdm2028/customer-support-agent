# 结构化路由与政策证据校验对齐 B01

2026-09-11：已修复“路由识别为物流咨询，证据校验却因问句包含退款二字而误拒绝物流证据”的问题。完整 Answer Dev 从 **13/20（65%）提升到 14/20（70%）**；E2E Dev 保持 **17/24（70.83%）**。两套数据分别统计，原通过案例均未退化。

## 修改与原因

原 `CustomerAgent` 只把问句和检索结果传给证据校验。`detect_policy_profile` 看到“退款”等词就优先套用退款政策，即使用户表达的是“不申请退款”。

现在调用方同时传入已有的 `RouteDecision`：动作是 query/execute/handoff 且主意图对应已有政策类型时，直接使用该类型；无路由、动作未知或没有对应政策类型时，保留原关键词判断。辅助主题不覆盖主意图。没有新增模型调用、业务类型、政策来源白名单或评分阈值。

修改集中在 `app/agent/policies/evidence_guardrail.py` 及其 `app/agent/agents/customer.py` 调用点。来源、分数、关键词和空证据检查保持原样。没有路由参数的旧调用也保持原行为，因此独立 RAG evaluator 和选择器的现有调用口径不变。本轮不修改 rerank、查询构建、召回、分块、知识库、Dev 数据或 expected。

选择这一方案是因为系统已经有明确的业务路由，只需把它传给后续校验，避免重复的关键词判断覆盖已有结果。它不能修复模型本身识别错误，也不代表完成了所有关联主题的证据覆盖检查。

## 诊断与对照

先启动 A 并冻结整理后的当前源码，再实现 B。两组均复用已有隔离运行器，回放 `reports/answer_e2e_dev_expansion_b01/live` 中实际观察到的路由；最终回答调用真实 `glm-4-flash`。固定配置为 `fixed_256`、本地 256 维 embedding、`hybrid_rule + complementary`，政策工具 candidate_k=20、top_k=5。每套从独立 SQLite、演示订单及进程内缓存开始。

快照对比确认 A/B 应用代码只有上述两个文件不同。44 条案例的路由、政策查询参数、工具调用名称与顺序相同；23 次政策检索返回的完整证据（含 chunk 顺序、文本及分数）也完全相同。完整 Top20 没有另存，本轮的直接对照对象是进入校验前的同一份 Top5，不能声称检查了未保存的 Top20 trace。

另外在同一份 A 的实际工具结果上，分别运行旧/新校验：23 次校验中只有 `answer_dev_b01_014` 改变，从拒绝变为通过，其余 22 次判定及报告完全一致；其他 21 条案例未执行该校验，不算“校验通过”。详见 [固定证据回放](guardrail_replay.json)。该回放不使用 expected 选择证据。

| 原指标 | A | B |
| --- | ---: | ---: |
| Answer 通过 | 13/20 | 14/20 |
| Answer correctness / completeness | 0.7500 / 0.7500 | 0.8000 / 0.8000 |
| Answer faithfulness_groundedness | 0.7273 | 0.8182 |
| Answer relevance / hallucination_rate | 1.0000 / 0.0500 | 1.0000 / 0.0500 |
| E2E 通过 | 17/24 | 17/24 |

指标完全沿用现有 evaluator 的判断和分母。E2E 全部 24 条的原检查项逐项一致，包括工具、风险、退款、数据库/MQ 与回答。Answer 仍有 6 条失败，E2E 仍有 7 条失败；全部 44 条的状态及剩余问题见 [逐案表](cases.md)，完整指标、回复、原检查项及 Trace ID 见 [A/B 对照](comparison.json)。

新增通过案例的变化：

- 问句：订单 10008 显示签收但本人未收到，咨询签收核实信息，并明确“不申请退款”。
- A：主意图为 shipping_exception，校验却选 return_refund，返回 evidence_source_mismatch，进入兜底回答。
- B：选择 shipping_exception，原物流证据通过，回答引用签收时间、地点、人员及家人/前台/代收点代收等核实规则。
- 检索 chunk 未变；改善发生在证据校验到回答之间。原始前后回复及 chunk ID 见 [该案例记录](recovered_answer.json)。

## 测试与运行开销

新增 7 项回归测试，覆盖 CustomerAgent 实际接线、否定/引用表达、退款与物流混合请求、其他业务主意图、未知路由回退、辅助主题边界及证据不足。既有桥接测试补充路由传递断言。隔离发布检查结果为 **303 passed、101 subtests passed、11 skipped、3 warnings**，详见 [测试日志](release/unit_integration.txt)。11 项为 MySQL/Redis 专项跳过，本轮未验证真实依赖。

| 套件 | 耗时 A → B | 已记录模型调用 A → B | provider total tokens A → B |
| --- | ---: | ---: | ---: |
| Answer | 54.73 → 67.82 秒 | 9 → 10 | 36,923 → 41,685 |
| E2E | 41.41 → 48.02 秒 | 7 → 7 | 42,609 → 42,655 |

两组已记录调用的失败数均为 0；路由使用回放，不计入真实模型调用。目标案例通过证据校验后增加了一次实际回答生成。以上是单次套件耗时与用量，包含模型和网络波动，不能作为该校验函数的性能测量。

## 对照限制与未解决项

- 最终生成没有固定响应。B 的 `answer_refund_001` 返回无效或重复片段编号，触发已有 `grounded_llm_partial`：22 个可选片段中，A 选中 17 个，B 经校验保留 13 个；该案仍通过原评分。其路由、检索输入和证据校验报告完全一致，不能据整体通过数声称没有生成质量波动。
- 沿用现有套件内共享数据库及案例顺序，没有顺手实施案例独立夹具或冻结业务时钟；运行在同一天完成，实际政策输入与 E2E 检查均已核对一致。多轮或跨日期的稳定性需单独验证。
- A 的 `source_inputs_unchanged=false` 表示实验期间工作区发生了本轮修改；运行器在开始前已一次性冻结源码，A 的两套评测均使用同一旧快照。B 为 true，两份源码快照可核对，未混用中途修改。
- 地址澄清、发票咨询、会员路由及字面评分问题仍保留失败；未通过修改标签提高分数。本轮没有重新衡量语义模型准确率，也未运行 validation/final_test。

## 复现与回退

A/B 命令相同，仅在修改前后使用不同输出目录：

```powershell
python -X utf8 -m scripts.eval.run_isolated_baseline --suites answer e2e --route-replay-from reports/answer_e2e_dev_expansion_b01/live --live-replies --record-case-results --semantic-related-topics discard_invalid --policy-evidence complementary --output reports/policy_route_alignment_b01/<a或b的新目录>
```

各目录包含配置清单、源码快照、原 evaluator 输出、逐案记录与 Trace。新运行必须使用尚不存在的目录；当前代码为 B，复现 A 需要其源码快照或在独立副本应用本轮回退补丁。

[本轮修改补丁](changes.patch)、[回退补丁](rollback.patch) 均以修改前的实际工作区为基准；`changed_files_before.zip` 保存原文件字节。回退补丁已通过 `git apply --check`，未部署。
