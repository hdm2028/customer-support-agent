# RAG Dev 第一批：一次固定候选 A/B

结论：新 Dev 必要证据覆盖改善，但旧 V1 Dev 的主目标排序和部分证据退化。保留实验开关，默认仍为 A；本轮停止。

## 数据与对照条件

- [实际 Dev V2](../../data/eval/rag_eval_dev_v2_batch01.jsonl)：6 条、19 项 requirement；[原 V1 Dev](../../data/eval/rag_eval.jsonl)：20 条。两套单独统计。query、input_context、expected、split 均未修改；V1 原本没有 split 字段，未补写。
- [原基线](../eval_rag_dev_v2_batch01_baseline.json)：hybrid_rule，fixed_256（256 token、32 overlap），candidate_k=20，top_k=5，local_hash_v1/256，召回权重 0.62/0.28/0.10，KB kb_08b60d90794c5afd235bdb5c。
- 原报告只有候选 ID/匹配结果和最终 Top5 预览，缺完整 Top20 正文、落选候选的重排分数；原进程内 TTL 缓存不可用。仅补做一次离线同公式诊断：读取原 KB，按原分块恢复正文，并核对现有 manifest 的 chunk ID/content hash；计算相同 BM25、local hash 和 keyword 分数。未调用 RAGIndexManager.refresh、创建/更新 VectorStore 或写入 manifest。
- V2 全部 Top20 ID/顺序、semantic/lexical query、Top5 ID/检索分数/规则分数、候选及最终 requirement 匹配结果均与原基线完全一致。V2 文件 SHA 与原基线相同，V1 SHA 与前轮记录相同。恢复的完整 trace 属于本次诊断，不能称为历史已保存。
- A/B 共用冻结候选，每案断言候选未被修改。V1 A 在本批相同 KB/分块/召回条件下计算，不混用旧 KB 的历史 V1 分数。未调用其他排序模式或加载 CrossEncoder。
- 查找报告时误打开了一个文件名含 baseline、实际标记 holdout 的旧报告；未使用其中案例或结果作诊断或选参。未读取 validation 数据，未运行 validation/final_test。

## 证据丢失定位

实际顺序：构建 semantic/lexical query → 混合分数 → 过滤 score≤0 → 排序并截取 Top20 → 规则重排 → final_rank 编号 → evaluator/在线 retriever 截取前 5。hybrid_rule 在候选池之后没有过滤、去重或 constraint 替换。guardrail 只打评测标记，不删除条目。
逐项直接复用未改动的 `eval_rag.evidence_requirement_matches`：source、section/covered_sections 匹配后，在规范化 source+section+完整正文中检查 keywords_any、keywords_all、critical_terms。一项要求必须由同一个 chunk 完整满足。primary_target 仅匹配来源/章节，独立于必要证据。

C=候选位置，R=A 重排位置；R>5 均因 Top5 截断落选，无过滤/去重误删。E 是原 expected 中的顺序，下面给出完整 chunk ID。

|case_id / E|业务规则（原 keywords_all）|匹配 chunk_id|C→R|A Top5 / 原因|
|---|---|---|---|---|
|rag_dev_candidate_b01_002 / E1|不能因为商品属于定制商品，就拒绝所有质量问题售后请求|`doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c`|1→1|是|
|rag_dev_candidate_b01_002 / E2|是否能够退款或者重新制作；根据实际问题和人工审核结果判断|`doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c`|1→1|是|
|rag_dev_candidate_b01_002 / E3|不能仅根据用户描述直接认定商品属于质量问题|`doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c`|3→6|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_002 / E4|检测结果确认属于产品质量问题后；根据对应政策决定后续处理方式|`doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c`|3→6|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_002 / E5|只有满足对应退款条件后，才能继续进入退款申请流程|`doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6`|12→13|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_004 / E1|无法拦截时；等待商品送达或者拒收后；再按照退货流程处理|`doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3`|4→11|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_004 / E2|不能按照普通待发货订单直接取消|`doc_41203502602eba8a5ebda54a:chunk:22153be8fe710e0957519fb5`|7→13|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_004 / E3|需要等待物流状态更新|`doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7`|1→4|是|
|rag_dev_candidate_b01_004 / E3|需要等待物流状态更新|`doc_b3377cfe0dac262b10bf7bd4:chunk:109aaccc7b3e5580f9af3985`|10→2|是|
|rag_dev_candidate_b01_004 / E4|商品退回仓库并完成确认后；根据订单情况进入退款或者退货流程|`doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7`|1→4|是|
|rag_dev_candidate_b01_005 / E1|投诉升级不代表用户提出的要求一定成立|`doc_acfdb96dc65ed74f58613bc9:chunk:b80d2ab28186ef71990d202b`|1→1|是|
|rag_dev_candidate_b01_005 / E2|根据订单事实、平台政策以及人工审核结果决定|`doc_acfdb96dc65ed74f58613bc9:chunk:f0e57c6dbcc58932d1a866b3`|9→7|否；重排落后，Top5 截断|
|rag_dev_candidate_b01_005 / E3|以下情况可能进入人工审核；投诉升级并涉及资金操作|`doc_253629df931f97428ff46d6e:chunk:29987d965eb8a2bc8a2a8083`|6→5|是|
|rag_dev_candidate_b01_005 / E4|只有满足对应退款条件后，才能继续进入退款申请流程|`doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6`|8→14|否；重排落后，Top5 截断|

002、004、005 分别只需 3、3、4 个候选 chunk 即可完全覆盖。组合见 [diagnosis.json](diagnosis.json) 的 `oracle_diagnostic_only`：它读取 expected，仅是离线覆盖上限，从未作为系统输出或运行时特征。003 无全覆盖组合：E2 退款资格在 Top20 无匹配；其 primary_target 在候选第 2、A/B 最终第 1，不属于主目标候选缺失。

## 唯一修改及依据

`rank_candidates(..., rule_metadata_priors=False)` 只关闭来源 +0.12 和首章节 +0.18 这组固定加分。默认 True；A=True，B=False。短语匹配仍使用原 source/section/text 拼接，关键词、状态信号、tie-break 不变。无模型替换、无 source/section 配额、无 case 特例、无 expected 运行时输入；实验 trace 版本加 `-no-metadata-priors` 后缀。
002 的 C6/C9/C10 获“七天无理由”首章节加分，挤到 E3/E4 所在 C3 前面；首标题不能代表跨章节 chunk 的所有业务规则。004 的 C10/C12 获来源及首章节 +0.30，升到 R2/R3，E1 的 C4 落到 R11。005 的 FAQ C13 获来源 +0.12，升到 R4，完整处理原则 C9 留在 R7。B 消除了这组已观测加分，但不能据此声称修复了所有排序问题。

- rag_dev_candidate_b01_002：Top5 正文字符二元组 Jaccard 最大 0.245（R2/R5），仅作离线重叠描述，不作评分或运行时特征。
- rag_dev_candidate_b01_004：Top5 正文字符二元组 Jaccard 最大 0.230（R1/R4），仅作离线重叠描述，不作评分或运行时特征。
- rag_dev_candidate_b01_005：Top5 正文字符二元组 Jaccard 最大 0.174（R1/R2），仅作离线重叠描述，不作评分或运行时特征。

正文存在主题重复和分块重叠，没有完整相同条目，上述检查不足以支持“高度重复占位”为主因。002 多条通用售后条目未补齐限定来源的缺失规则；004 R2/R4 重复 E3 等待物流更新，但 R4 另提供 E4；005 两条 FAQ 分别有投诉、审核信息，均未补 E2/E4。因此未尝试去冗余，也不按章节限额。

## A/B 汇总：沿用原口径

|数据|指标|A|B|
|---|---|---|---|
|v2|hit_at_1|0.6667|0.8333|
|v2|hit_at_3|0.8333|0.8333|
|v2|hit_at_5|0.8333|0.8333|
|v2|mrr|0.75|0.8333|
|v2|candidate_evidence_recall_at_20|0.9167|0.9167|
|v2|evidence_coverage_rate|0.65|0.7583|
|v2|passed_count|2|2|
|v1|hit_at_1|0.95|0.85|
|v1|hit_at_3|1.0|1.0|
|v1|hit_at_5|1.0|1.0|
|v1|mrr|0.9667|0.925|
|v1|candidate_evidence_recall_at_20|0.975|0.975|
|v1|evidence_coverage_rate|0.9|0.8833|
|v1|passed_count|13|15|

V2 evidence_coverage_rate 是逐案覆盖率宏平均：0.6500→0.7583。必要证据计数 11/19→14/19，仅作计数，不替换现有宏平均分母；通过仍为 2/6。V1 无 V2 requirement，evidence_coverage_rate 按现有业务类别约束计算，不能冒称与 V2 相同的必要规则指标。

## 逐案比较

Hit 依次为 Hit@1/@3/@5；RR 是 evaluator 原 reciprocal_rank，汇总为原 MRR；N/A 保留。V2 覆盖数为 requirement；V1 覆盖数为现有约束类别。

### v2

|case_id|Hit A→B|首命中 A→B|RR A→B|覆盖数 A→B|通过 A→B|
|---|---|---|---|---|---|
|rag_dev_candidate_b01_001|0/1/1→1/1/1|2→1|0.5→1.0|3/3→3/3|True→True|
|rag_dev_candidate_b01_002|1/1/1→1/1/1|1→1|1.0→1.0|2/5→4/5|False→False|
|rag_dev_candidate_b01_003|1/1/1→1/1/1|1→1|1.0→1.0|1/2→1/2|False→False|
|rag_dev_candidate_b01_004|0/0/0→0/0/0|N/A→N/A|0.0→0.0|2/4→2/4|False→False|
|rag_dev_candidate_b01_005|1/1/1→1/1/1|1→1|1.0→1.0|2/4→3/4|False→False|
|rag_dev_candidate_b01_006|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|

### v1

|case_id|Hit A→B|首命中 A→B|RR A→B|覆盖数 A→B|通过 A→B|
|---|---|---|---|---|---|
|rag_refund_001|1/1/1→1/1/1|1→1|1.0→1.0|1/2→2/2|False→True|
|rag_refund_002|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_003|1/1/1→1/1/1|1→1|1.0→1.0|2/2→2/2|True→True|
|rag_refund_004|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_005|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_006|1/1/1→0/1/1|1→2|1.0→0.5|1/2→1/2|False→False|
|rag_refund_007|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_008|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_009|1/1/1→1/1/1|1→1|1.0→1.0|1/2→1/2|False→False|
|rag_refund_010|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_011|0/1/1→1/1/1|3→1|0.3333→1.0|1/1→1/1|False→True|
|rag_refund_012|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_013|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_014|1/1/1→1/1/1|1→1|1.0→1.0|2/2→2/2|True→True|
|rag_refund_015|1/1/1→1/1/1|1→1|1.0→1.0|1/2→1/2|False→False|
|rag_refund_016|1/1/1→0/1/1|1→2|1.0→0.5|3/3→2/3|False→False|
|rag_refund_017|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_018|1/1/1→0/1/1|1→2|1.0→0.5|1/1→1/1|False→True|
|rag_refund_019|1/1/1→1/1/1|1→1|1.0→1.0|1/1→1/1|True→True|
|rag_refund_020|1/1/1→1/1/1|1→1|1.0→1.0|2/2→1/2|True→False|

V2 具体增失规则：

|case_id|B 新增|B 丢失|B 仍缺|
|---|---|---|---|
|rag_dev_candidate_b01_001|无|无|无|
|rag_dev_candidate_b01_002|E3：不能仅根据用户描述直接认定商品属于质量问题<br>E4：检测结果确认属于产品质量问题后；根据对应政策决定后续处理方式|无|E5：只有满足对应退款条件后，才能继续进入退款申请流程|
|rag_dev_candidate_b01_003|无|无|E2：只有满足对应退款条件后，才能继续进入退款申请流程|
|rag_dev_candidate_b01_004|无|无|E1：无法拦截时；等待商品送达或者拒收后；再按照退货流程处理<br>E2：不能按照普通待发货订单直接取消|
|rag_dev_candidate_b01_005|E2：根据订单事实、平台政策以及人工审核结果决定|无|E4：只有满足对应退款条件后，才能继续进入退款申请流程|
|rag_dev_candidate_b01_006|无|无|无|

V1 类别/关键词变化及剩余缺口（其余案例无缺口）：

|case_id|类别变化 / B 仍缺|关键词新增 / B 仍缺|
|---|---|---|
|rag_refund_001|新增 order_change；丢失 无；仍缺 无|新增 无；仍缺 无|
|rag_refund_006|新增 无；丢失 无；仍缺 shipping|新增 无；仍缺 无|
|rag_refund_009|新增 无；丢失 无；仍缺 order_change|新增 无；仍缺 无|
|rag_refund_011|新增 无；丢失 无；仍缺 无|新增 最新状态；仍缺 无|
|rag_refund_015|新增 无；丢失 无；仍缺 product_after_sales|新增 无；仍缺 无|
|rag_refund_016|新增 无；丢失 shipping；仍缺 shipping|新增 损坏；仍缺 运输|
|rag_refund_018|新增 无；丢失 无；仍缺 无|新增 FAILED；仍缺 无|
|rag_refund_020|新增 无；丢失 complaint；仍缺 complaint|新增 无；仍缺 无|

V1 排序退化：006、016、018 从 1→2；011 从 3→1 改善。016 丢失 shipping，020 丢失 complaint 且从通过变失败。001、011、018 从失败变通过。因此通过数增加不等于没有损害。

## 最终选出的 chunk：全部案例

Cn 表示该案冻结候选第 n 项，下表按最终顺序列出。完整 chunk_id、来源/章节和正文见 [candidates.json](candidates.json) 对应 case_id；[ab_results.json](ab_results.json) 的 `traces.A/B.final_chunk_ids` 和 `ranking` 保存完整实际输出及全量分数，不含 oracle 替代。

|数据 / case_id|A Top5|B Top5|
|---|---|---|
|v2 / rag_dev_candidate_b01_001|C2、C1、C12、C10、C4|C1、C2、C4、C3、C8|
|v2 / rag_dev_candidate_b01_002|C1、C6、C9、C10、C4|C1、C4、C2、C3、C7|
|v2 / rag_dev_candidate_b01_003|C2、C6、C4、C1、C8|C2、C1、C8、C3、C6|
|v2 / rag_dev_candidate_b01_004|C2、C10、C12、C1、C5|C1、C2、C5、C9、C3|
|v2 / rag_dev_candidate_b01_005|C1、C2、C3、C13、C6|C1、C2、C3、C6、C9|
|v2 / rag_dev_candidate_b01_006|C3、C7、C1、C5、C10|C3、C2、C1、C6、C7|
|v1 / rag_refund_001|C3、C1、C2、C11、C12|C1、C2、C3、C4、C8|
|v1 / rag_refund_002|C2、C4、C1、C3、C5|C1、C2、C3、C4、C5|
|v1 / rag_refund_003|C4、C11、C2、C5、C12|C4、C3、C1、C2、C7|
|v1 / rag_refund_004|C3、C1、C4、C2、C5|C1、C2、C3、C5、C4|
|v1 / rag_refund_005|C1、C6、C2、C11、C3|C1、C2、C3、C5、C4|
|v1 / rag_refund_006|C3、C4、C2、C1、C18|C2、C3、C1、C4、C5|
|v1 / rag_refund_007|C2、C3、C5、C1、C6|C2、C1、C3、C6、C4|
|v1 / rag_refund_008|C2、C7、C3、C1、C8|C2、C3、C1、C4、C5|
|v1 / rag_refund_009|C3、C1、C8、C2、C13|C1、C3、C2、C4、C8|
|v1 / rag_refund_010|C1、C10、C2、C8、C3|C1、C2、C3、C5、C6|
|v1 / rag_refund_011|C8、C5、C14、C1、C2|C1、C2、C3、C7、C8|
|v1 / rag_refund_012|C6、C2、C13、C3、C14|C2、C1、C3、C6、C4|
|v1 / rag_refund_013|C5、C3、C1、C14、C4|C1、C4、C2、C5、C3|
|v1 / rag_refund_014|C2、C5、C9、C13、C1|C2、C1、C4、C3、C5|
|v1 / rag_refund_015|C1、C2、C4、C10、C3|C1、C2、C3、C6、C4|
|v1 / rag_refund_016|C4、C10、C1、C18、C11|C1、C4、C2、C3、C11|
|v1 / rag_refund_017|C1、C5、C2、C6、C11|C1、C2、C5、C7、C4|
|v1 / rag_refund_018|C3、C9、C7、C1、C11|C1、C3、C4、C2、C5|
|v1 / rag_refund_019|C3、C7、C5、C1、C12|C1、C3、C7、C2、C4|
|v1 / rag_refund_020|C2、C10、C9、C7、C15|C2、C1、C4、C10、C9|

## 开销、测试与回退

仅记录此次 `rank_candidates` 单次墙钟耗时，排除召回、正文恢复、评分和 IO。顺序 A 后 B，无预热或重复基准，毫秒级差异不能用于声称线上提速。端到端延迟、内存峰值、API 费用未测量；A/B 均未调用外部模型。

|数据|A 总计 / 平均 ms|B 总计 / 平均 ms|
|---|---|---|
|v2|1.961 / 0.327|1.891 / 0.315|
|v1|6.332 / 0.317|5.516 / 0.276|

- 测试：`python -X utf8 -m pytest tests/test_rag_rule_metadata_priors.py tests/test_rag_ranking.py -q`，22 passed、5 subtests passed。验证默认兼容、只移除元数据先验、保留状态信号、输入不变和同章节多个 chunk 可保留。
- 修改：[reranker.py](../../app/rag/reranker.py)、[ranking.py](../../app/rag/ranking.py)。仅一组加分逻辑，后者透传开关并标记实验版本。不传参数或设置 True 即回到 A；未改默认配置。
- [回放入口](../../scripts/eval/rag_dev_b01_replay.py)：capture→diagnose→replay。已有候选/A/B 产物时拒绝覆盖。仅各完成一次，没有尝试多组方案。
- 未修改知识库、现有索引、Schema、Validator、eval_rag.py 或数据，未覆盖原基线，未生成案例或重复人工审核。
- 停止：002/005 部分改善，004 未改善；003 退款资格召回缺口仍在；V1 有明确退化。不替换默认方案，不继续第二轮。
