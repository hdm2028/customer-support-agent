# RAG Dev 第一批：后续优化结果（2026-09-08）

当前范围的完成标准已达到：新 Dev 在冻结 Top20 上达到必要证据覆盖上限 18/19；原 V1 Dev 的每案主目标排名、证据覆盖、已通过结果、guardrail 和已命中关键词均无退化。默认方案保持原设置，最终实验通过显式参数启用。

新 Dev 5/6 通过，唯一失败是 003 的退款资格证据未进入 Top20。原 V1 Dev 17/20 通过，仍有 3 条原失败。这里的完成范围是本批候选选择优化；没有开展召回、知识库或全系统优化。

## 固定条件与实际产物

- 继续使用 [冻结候选](../rag_dev_b01_optimization/candidates.json)，没有再次召回、恢复正文或重建索引。KB 为 `kb_08b60d90794c5afd235bdb5c`；candidate_k=20，top_k=5，fixed_256，local_hash_v1/256，原召回权重 0.62/0.28/0.10。
- 数据：[Dev V2 6 条/19 项](../../data/eval/rag_eval_dev_v2_batch01.jsonl)、[V1 Dev 20 条](../../data/eval/rag_eval.jsonl)。逐次验证文件 SHA；query、input_context、expected、split 均不变。没有生成案例或重复人工审核。
- A 为上一轮保存的原 `hybrid_rule`，来源/首章节先验保持开启；B 为 `r13_guardrail_compatible`。上一轮关闭先验的方案没有成为本轮默认或基线。新旧 Dev 分开统计，不形成 26 条总体准确率。
- [最终运行产物](r13_guardrail_compatible.json) 保存逐案评分、完整重排/选择记录、所有 chunk ID、时间和当时的实现源码；[最终公共接口核验](final_verification.json) 保存交付代码快照、实际 Top5 和断言结果。所有中间试验也保留。
- 本轮未读取或运行 validation、holdout、final_test。未改 Schema、Validator、eval_rag.py、知识库、索引、查询字符串构建或召回配置。上一轮报告记录的误读旧 holdout 报告没有再次发生。

## 最终修改

只在规则重排之后增加可选的候选选择阶段；基线分数不改，首条结果固定，选中集合按原重排相对顺序输出。主要行为：
1. 识别首条证据的截断句，只在同文档相邻候选中确有文本重叠且补齐句子时选取续块。没有额外读取候选池外 chunk。
2. 对多个业务主题的请求，按现有 query contract 的主题、关联主题和事实评估补充证据；单主题继续沿用基线和有限句子补齐，避免泛化主题挤掉原来的具体条款。
3. 解析首条正文实际出现的《政策引用》和现有业务词表中的具体概念，检查关联条件与前置步骤是否在同一片段得到支持；显式输入主题/事实优先于这些推导出的补充概念。
4. 保留基线已覆盖的业务类别和业务词组，复用现有触发词组识别“快递/物流”这类替代。并列时参考原 Top5 的章节覆盖及原排名；没有每文件/章节最多一个 chunk 的限制。
5. 复用既有 `source_type` 区分 case 与非 case，优先用非 case 候选覆盖政策主题；同时保留既有业务 guardrail 对前三条来源和证据关键词的要求。未修改 guardrail 规则，也未把其通过条件移入 evaluator。
实现不读取案例 ID、审核文件、expected 或测试集；唯一名为 `expected_sources` 的字段来自已有线上 `POLICY_PROFILES`，不是评测数据。使用 expected 的匹配和覆盖上限检查只发生在离线 scorer 之后。

代码：[选择器](../../app/rag/evidence_selection.py)、[排名入口](../../app/rag/ranking.py)、[同步/异步检索透传](../../app/rag/retriever.py)。版本 `query-facets-dependencies-v1`。已有默认路径逐案复核与原 A 完全一致。

```python
retriever.retrieve(query, top_k=5, candidate_k=20, mode="hybrid_rule",
                   evidence_selection="complementary")
```

不传 `evidence_selection` 或传 `None` 即回到原方案；`aretrieve` 同样支持。没有自动切换环境配置。

## 各次对照记录

以下均与同一原 A 比较，不隐去未改善/退化的变体。R11 以 R09 为父版本，撤回 R10 的等权尝试后只增加条件与前置概念的绑定；其他轮次承接上一轮。历史源码快照以各 JSON 中 `implementation` 为准，当前源码只代表最终版本。

|变体|本次变化|V2 必要证据 / 宏覆盖 / 通过|V1 MRR / 覆盖 / 通过|相对 A 的退化|
|---|---|---|---|---|
|[r02_complete_primary](r02_complete_primary.json)|只补齐首条截断句|12/19 / 0.6917 / 2/6|0.9667 / 0.9 / 13/20|无|
|[r03_complementary](r03_complementary.json)|增加主题/引用互补选择|16/19 / 0.8333 / 3/6|0.9667 / 0.975 / 15/20|v1/rag_refund_005:通过→失败|
|[r04_stable_affinity](r04_stable_affinity.json)|整数计分分子消除等价分数的浮点伪差|16/19 / 0.8333 / 3/6|0.9667 / 0.975 / 15/20|v1/rag_refund_005:通过→失败|
|[r05_multitopic_scope](r05_multitopic_scope.json)|限制跨政策扩展为多主题请求|16/19 / 0.8333 / 3/6|0.9667 / 0.975 / 16/20|无|
|[r06_keyword_groups](r06_keyword_groups.json)|保留业务词组而非强制相同字面词|17/19 / 0.875 / 4/6|0.9667 / 0.975 / 16/20|无|
|[r07_section_tiebreak](r07_section_tiebreak.json)|并列时参考原章节覆盖|17/19 / 0.875 / 4/6|0.9667 / 0.975 / 16/20|无|
|[r08_primary_concepts](r08_primary_concepts.json)|加入首条正文提及的前置概念|17/19 / 0.875 / 4/6|0.9667 / 0.975 / 16/20|无|
|[r09_normative_evidence](r09_normative_evidence.json)|非 case 证据优先覆盖政策主题|17/19 / 0.875 / 4/6|0.9667 / 0.975 / 16/20|无|
|[r10_covered_sections](r10_covered_sections.json)|首章节/内部章节改为等权，未采纳|15/19 / 0.8 / 2/6|0.9667 / 0.975 / 16/20|无|
|[r11_bound_dependencies](r11_bound_dependencies.json)|在 R09 上绑定业务条件与前置概念|16/19 / 0.8417 / 3/6|0.9667 / 0.975 / 17/20|无|
|[r12_explicit_topics_first](r12_explicit_topics_first.json)|显式主题/事实优先于推导概念|18/19 / 0.9167 / 4/6|0.9667 / 0.975 / 17/20|v2/rag_dev_candidate_b01_005:guardrail|
|[r13_guardrail_compatible](r13_guardrail_compatible.json)|保留已有 guardrail 条件|18/19 / 0.9167 / 5/6|0.9667 / 0.975 / 17/20|无|

R04 修正了确定存在的浮点并列问题，但总体指标未变化；R07–R09 的孤立结果同样未增加必要证据命中。R10 比当时较优方案退步；R12 虽达到 18/19，却令 005 新增 guardrail 失败，因此没有交付 R12。早期 JSON 中 `regressions_vs_original` 只检查排名/覆盖/通过，本表额外展示实际 guardrail 退化，最终核验也逐案检查关键词丢失。

## 最终汇总：A → B

|数据|指标|A|B|
|---|---|---|---|
|v2|hit_at_1|0.6667|0.6667|
|v2|hit_at_3|0.8333|0.8333|
|v2|hit_at_5|0.8333|1.0|
|v2|mrr|0.75|0.7917|
|v2|candidate_evidence_recall_at_20|0.9167|0.9167|
|v2|evidence_coverage_rate|0.65|0.9167|
|v2|evidence_guardrail_pass_rate|1.0|1.0|
|v2|passed_count|2|5|
|v1|hit_at_1|0.95|0.95|
|v1|hit_at_3|1.0|1.0|
|v1|hit_at_5|1.0|1.0|
|v1|mrr|0.9667|0.9667|
|v1|candidate_evidence_recall_at_20|0.975|0.975|
|v1|evidence_coverage_rate|0.9|0.975|
|v1|evidence_guardrail_pass_rate|1.0|1.0|
|v1|passed_count|13|17|

V2 必要证据计数 11/19→18/19；evidence_coverage_rate 仍按各案例覆盖率宏平均，0.6500→0.9167，不用 18/19 替换原指标。Top20 中也只有 18/19 可匹配，003 的缺失不可通过本轮选择器补齐。V1 的覆盖指标是原业务类别覆盖，0.9000→0.9750，不与 V2 requirement 指标混算。

## 逐案结果及最终选择

Hit 为 Hit@1/@3/@5，RR 为原 reciprocal_rank；Cn 表示该案 [冻结 Top20](../rag_dev_b01_optimization/candidates.json) 第 n 项。所有完整 chunk ID、来源、正文和排序分数都保存在最终 JSON 的 `datasets.<v1/v2>.traces`。V2 覆盖数为 requirement，V1 覆盖数为业务类别。

### v2

|case_id|Hit A→B|首命中 / RR A→B|覆盖数 A→B|通过 A→B|A Top5 → B Top5|
|---|---|---|---|---|---|
|rag_dev_candidate_b01_001|0/1/1→0/1/1|2→2 / 0.5→0.5|3/3→3/3|True→True|C2,C1,C12,C10,C4 → C2,C1,C12,C10,C4|
|rag_dev_candidate_b01_002|1/1/1→1/1/1|1→1 / 1.0→1.0|2/5→5/5|False→True|C1,C6,C9,C10,C4 → C1,C4,C3,C8,C12|
|rag_dev_candidate_b01_003|1/1/1→1/1/1|1→1 / 1.0→1.0|1/2→1/2|False→False|C2,C6,C4,C1,C8 → C2,C6,C11,C16,C20|
|rag_dev_candidate_b01_004|0/0/0→0/0/1|N/A→4 / 0.0→0.25|2/4→4/4|False→True|C2,C10,C12,C1,C5 → C2,C12,C1,C4,C7|
|rag_dev_candidate_b01_005|1/1/1→1/1/1|1→1 / 1.0→1.0|2/4→4/4|False→True|C1,C2,C3,C13,C6 → C1,C3,C6,C9,C8|
|rag_dev_candidate_b01_006|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C3,C7,C1,C5,C10 → C3,C7,C1,C10,C12|

### v1

|case_id|Hit A→B|首命中 / RR A→B|覆盖数 A→B|通过 A→B|A Top5 → B Top5|
|---|---|---|---|---|---|
|rag_refund_001|1/1/1→1/1/1|1→1 / 1.0→1.0|1/2→2/2|False→True|C3,C1,C2,C11,C12 → C3,C1,C2,C4,C16|
|rag_refund_002|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C2,C4,C1,C3,C5 → C2,C4,C1,C3,C5|
|rag_refund_003|1/1/1→1/1/1|1→1 / 1.0→1.0|2/2→2/2|True→True|C4,C11,C2,C5,C12 → C4,C7,C10,C3,C1|
|rag_refund_004|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C3,C1,C4,C2,C5 → C3,C1,C4,C2,C5|
|rag_refund_005|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C1,C6,C2,C11,C3 → C1,C6,C2,C11,C3|
|rag_refund_006|1/1/1→1/1/1|1→1 / 1.0→1.0|1/2→2/2|False→True|C3,C4,C2,C1,C18 → C3,C4,C18,C19,C8|
|rag_refund_007|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C2,C3,C5,C1,C6 → C2,C3,C5,C1,C6|
|rag_refund_008|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C2,C7,C3,C1,C8 → C2,C7,C3,C8,C9|
|rag_refund_009|1/1/1→1/1/1|1→1 / 1.0→1.0|1/2→1/2|False→False|C3,C1,C8,C2,C13 → C3,C1,C8,C2,C9|
|rag_refund_010|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C1,C10,C2,C8,C3 → C1,C10,C2,C8,C3|
|rag_refund_011|0/1/1→0/1/1|3→3 / 0.3333→0.3333|1/1→1/1|False→False|C8,C5,C14,C1,C2 → C8,C5,C14,C1,C2|
|rag_refund_012|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C6,C2,C13,C3,C14 → C6,C2,C13,C3,C9|
|rag_refund_013|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C5,C3,C1,C14,C4 → C5,C3,C1,C14,C4|
|rag_refund_014|1/1/1→1/1/1|1→1 / 1.0→1.0|2/2→2/2|True→True|C2,C5,C9,C13,C1 → C2,C11,C4,C3,C18|
|rag_refund_015|1/1/1→1/1/1|1→1 / 1.0→1.0|1/2→2/2|False→True|C1,C2,C4,C10,C3 → C1,C2,C10,C3,C8|
|rag_refund_016|1/1/1→1/1/1|1→1 / 1.0→1.0|3/3→3/3|False→True|C4,C10,C1,C18,C11 → C4,C1,C18,C3,C14|
|rag_refund_017|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C1,C5,C2,C6,C11 → C1,C5,C2,C6,C11|
|rag_refund_018|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|False→False|C3,C9,C7,C1,C11 → C3,C9,C7,C1,C6|
|rag_refund_019|1/1/1→1/1/1|1→1 / 1.0→1.0|1/1→1/1|True→True|C3,C7,C5,C1,C12 → C3,C7,C5,C1,C9|
|rag_refund_020|1/1/1→1/1/1|1→1 / 1.0→1.0|2/2→2/2|True→True|C2,C10,C9,C7,C15 → C2,C9,C15,C3,C12|

V1 新通过：001 补齐 order_change，006 补齐 shipping，015 补齐 product_after_sales，016 补齐原缺关键词“运输”“损坏”。V1 没有类别、已命中关键词或 guardrail 丢失。剩余 009 的 order_change 类别候选缺失；011 的“最新状态”和 018 的“FAILED”存在候选但未进入 Top5。后两条均为单主题原失败，此轮保留其原路径，没有扩大优化范围。

## V2 全部 19 项证据对照

直接复用未改动的 `evidence_requirement_matches`；E 为原 expected 顺序。位置列为“候选位置 → A Top5 位置 → B Top5 位置”，— 表示未入选；不以 primary_target 命中替代必要证据。

|case_id / E|规则（原 keywords_all）|匹配 chunk_id|候选 → A → B|
|---|---|---|---|
|rag_dev_candidate_b01_001 / E1|签收之日起 7 天内；商品保持完好；商品配件齐全；商品包装不存在严重损坏；不影响商品二次销售；商品不属于不支持七天无理由退货的特殊商品|`doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3`|1 → 2 → 2|
|rag_dev_candidate_b01_001 / E2|满足以上条件后，可以创建退货申请|`doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3`|1 → 2 → 2|
|rag_dev_candidate_b01_001 / E3|商品退回并完成质检后，系统进入退款流程|`doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3`|1 → 2 → 2|
|rag_dev_candidate_b01_002 / E1|不能因为商品属于定制商品，就拒绝所有质量问题售后请求|`doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c`|1 → 1 → 1|
|rag_dev_candidate_b01_002 / E2|是否能够退款或者重新制作；根据实际问题和人工审核结果判断|`doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c`|1 → 1 → 1|
|rag_dev_candidate_b01_002 / E3|不能仅根据用户描述直接认定商品属于质量问题|`doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c`|3 → — → 3|
|rag_dev_candidate_b01_002 / E4|检测结果确认属于产品质量问题后；根据对应政策决定后续处理方式|`doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c`|3 → — → 3|
|rag_dev_candidate_b01_002 / E5|只有满足对应退款条件后，才能继续进入退款申请流程|`doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6`|12 → — → 5|
|rag_dev_candidate_b01_003 / E1|如果检测结果无法确认；可以进入人工审核|`doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c`|2 → 1 → 1|
|rag_dev_candidate_b01_003 / E2|只有满足对应退款条件后，才能继续进入退款申请流程|无候选匹配|— → — → —|
|rag_dev_candidate_b01_004 / E1|无法拦截时；等待商品送达或者拒收后；再按照退货流程处理|`doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3`|4 → — → 4|
|rag_dev_candidate_b01_004 / E2|不能按照普通待发货订单直接取消|`doc_41203502602eba8a5ebda54a:chunk:22153be8fe710e0957519fb5`|7 → — → 5|
|rag_dev_candidate_b01_004 / E3|需要等待物流状态更新|`doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7`|1 → 4 → 3|
|rag_dev_candidate_b01_004 / E3|需要等待物流状态更新|`doc_b3377cfe0dac262b10bf7bd4:chunk:109aaccc7b3e5580f9af3985`|10 → 2 → —|
|rag_dev_candidate_b01_004 / E4|商品退回仓库并完成确认后；根据订单情况进入退款或者退货流程|`doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7`|1 → 4 → 3|
|rag_dev_candidate_b01_005 / E1|投诉升级不代表用户提出的要求一定成立|`doc_acfdb96dc65ed74f58613bc9:chunk:b80d2ab28186ef71990d202b`|1 → 1 → 1|
|rag_dev_candidate_b01_005 / E2|根据订单事实、平台政策以及人工审核结果决定|`doc_acfdb96dc65ed74f58613bc9:chunk:f0e57c6dbcc58932d1a866b3`|9 → — → 4|
|rag_dev_candidate_b01_005 / E3|以下情况可能进入人工审核；投诉升级并涉及资金操作|`doc_253629df931f97428ff46d6e:chunk:29987d965eb8a2bc8a2a8083`|6 → 5 → 3|
|rag_dev_candidate_b01_005 / E4|只有满足对应退款条件后，才能继续进入退款申请流程|`doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6`|8 → — → 5|
|rag_dev_candidate_b01_006 / E1|如果无法确认退款是否已经成功；不得直接重复执行退款；应先查询实际退款状态或者进入人工处理|`doc_253629df931f97428ff46d6e:chunk:17797a64638403575a58c349`|3 → 1 → 1|

新增命中：002 的质量判断 E3/E4 和退款资格 E5；004 的拦截失败后退货路径 E1、已发货不能直接取消 E2；005 的完整处理原则 E2、退款资格 E4。A 已有必要证据没有丢失。003 主目标仍在第 1，缺失的是退款资格必要证据，不是 primary_target。

## 测试、开销和适用边界

- [测试输出](tests.txt)：43 passed、5 subtests passed。覆盖真实重叠续句、无重叠/完整句不替换、同源多块保留、类别/词组保护、guardrail 前三来源、并列分数一致性、单主题范围、输入不变、显式开关及同步/异步透传。
- `python -X utf8 -m scripts.eval.rag_dev_b01_followup verify_final`：公共 retriever 接入冻结候选后，全部输出和评分逐案等于最终选定实验；默认路径逐案等于原 A；数据、KB、query contract 保持一致。没有用 mock 评分或 oracle 输出替代检索结果，接口的候选提供函数仅用于固定同一 Top20。
- 新选择器在 20 候选/5 输出时最多检查 C(19,4)=3876 个包含原首条的组合，增加 CPU 开销。下面是一次公共接口冻结回放的墙钟时间，不含召回、网络、evaluator 或文件 IO；不作为稳定性能基准。

|数据|A 上轮规则排序平均 ms|B 本轮选择/包装平均 ms|B 最大 ms|
|---|---|---|---|
|v2|0.327|27.806|58.627|
|v1|0.317|12.074|48.392|

A/B 时间来自不同回放且 B 包含公共包装层，只能说明观测到额外选择开销，不能计算稳定提速/减速倍数。本轮没有调用外部模型；未测端到端延迟、峰值内存或线上负载。
这些 Dev 已经过多轮调优，结果只支持当前 Dev 完成标准，不构成未见数据的效果证明。默认未切换；003 的召回缺口、V1 原剩余失败和独立验证保留为后续事项，本轮不再继续追分。
