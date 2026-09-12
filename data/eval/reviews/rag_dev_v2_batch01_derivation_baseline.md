# RAG Dev V2 batch01：授权派生与一次当前配置基线

## 1. 授权、范围与来源

本次用户明确批准6条Candidate按指定差异纳入Dev。本记录登记这次人工决定；原Candidate和原review保留原样，包括原review的历史待审核状态。这是同一批6条案例的派生，不是6个新增独立场景。不合并旧V1，不运行V1参照或validation，不修改场景矩阵或生成计划。

原文件：[Candidate](../candidates/rag_dev_candidates_batch01.jsonl)；原证据背景：[review](rag_dev_candidates_batch01_review.md)。
新文件：[Dev](../rag_eval_dev_v2_batch01.jsonl)。
生成前确认Dev、本记录及专用结果文件均不存在。保留case_id及schema_version=rag-eval-v2，全案split从candidate改dev。

SHA-256（文件原始字节）：
- Candidate：6A6A235A5C902C5B28501763FB75E1BCC49ED58C004AAED89B3612E0030E4197
- Dev：D03FB99776AB81C498EB9630CF3B69414DED409E5161537FC6552E2658085C89
- evaluator结果：2D2BC48B3AA7FCA04AFF9C5ED953A4CB04181474F4BE180B7913B23A5DC9F857

## 2. 逐案对应与完整内容差异

所有未列出的评测内容保持不变，包括input_context、primary_targets、supporting_targets、枚举、tags。全部supporting_targets仍为[]。对派生对象进行了逐字段批准差异断言，PASS；case数6，必要证据数[3,5,2,4,4,1]，总计19。

### rag_dev_candidate_b01_001 → 同ID Dev / RAG-DEV-S020

人工决定：按用户指定修改后批准。
共同差异：split candidate → dev。
query原文：买来觉得不合适，东西没用过，商品、原包装和配件都完好齐全，也没有影响再次出售。这是普通现货，页面没有写不支持退货，这单现在能申请退吗？

query新文：买来觉得不合适，东西没用过，商品、原包装和配件都完好齐全，也没有影响再次出售。这是普通现货，页面没有写不支持退货，这单现在能申请退吗？申请以后，还要满足什么条件才会开始退款？

其余评测内容不变，三项requirement原样保留。

### rag_dev_candidate_b01_002 → 同ID Dev / RAG-DEV-S021

人工决定：原样批准（仅角色split正式改dev）。
共同差异：split candidate → dev。
除split外无内容差异。

### rag_dev_candidate_b01_003 → 同ID Dev / RAG-DEV-S003

人工决定：按用户指定修改后批准。
共同差异：split candidate → dev。
删除原E1、E2，保留原E3→新E1、原E4→新E2，匹配条件逐字段原样保留。删除项只作审查背景，不转supporting或其他强制要求。

原notes：检测结论无法确认时的政策检索；不得补成已确认质量问题。主目标按本子条件定位商品质量问题，原矩阵其他必要证据仍全部保留。

新notes：检测结论无法确认时的政策检索；不得补成已确认质量问题。主目标定位包含检测不明分支的章节；分支覆盖由人工审核requirement检查；章节首命中排名不等于分支证据首命中排名。

notes原“原矩阵其他必要证据仍全部保留”与批准的删项矛盾，故仅本例同步修正。主目标定位包含检测不明分支的章节；分支覆盖由保留的人工审核requirement检查；章节首命中排名不等于分支证据首命中排名。

删除背景（不评分）：
```json
[
  {
    "evidence_type": "product_quality",
    "acceptable_sources": [
      "退换货政策.md"
    ],
    "acceptable_sections": [
      "质量问题退款"
    ],
    "keywords_all": [
      "不能仅根据用户描述直接认定商品属于质量问题"
    ]
  },
  {
    "evidence_type": "product_quality",
    "acceptable_sources": [
      "商品售后规则.md"
    ],
    "acceptable_sections": [
      "商品质量问题"
    ],
    "keywords_all": [
      "检测结果确认属于产品质量问题后",
      "根据对应政策决定后续处理方式"
    ]
  }
]
```

### rag_dev_candidate_b01_004 → 同ID Dev / RAG-DEV-S006

人工决定：原样批准（仅角色split正式改dev）。
共同差异：split candidate → dev。
除split外无内容差异。

### rag_dev_candidate_b01_005 → 同ID Dev / RAG-DEV-S019

人工决定：原样批准（仅角色split正式改dev）。
共同差异：split candidate → dev。
除split外无内容差异。

### rag_dev_candidate_b01_006 → 同ID Dev / RAG-DEV-S022

人工决定：按用户指定修改后批准。
共同差异：split candidate → dev。
删除原E2，原E1及所有匹配条件保留，query/input_context/primary_targets/notes不变。未添加自动系统状态。E1已包含未知结果、禁止直接重复执行、查询实际状态或进入人工处理的规则。“退款失败”章节名不表示本例已认定失败。

删除背景（不评分）：
```json
{
  "evidence_type": "human_review",
  "acceptable_sources": [
    "人工审核SOP.md"
  ],
  "acceptable_sections": [
    "退款人工审核"
  ],
  "keywords_all": [
    "以下情况可以进入退款人工审核",
    "自动系统无法确认退款是否已经执行"
  ]
}
```

## 3. 运行前检查、代码与实际配置

Validator命令（项目根目录）：

```powershell
python -B .agents/skills/eval-dataset-builder/scripts/validate_dataset.py data/eval/rag_eval_dev_v2_batch01.jsonl
```

输出：Rows: 6；Parsed: 6；Errors: 0；Result: PASS。独立执行退出码0。
第一次组合检查命令末尾Get-Item查询不存在的预期目标返回PowerShell退出1，非Validator失败；已单独运行确认Validator退出0。

代码HEAD：e0ef6c5c517101f4e25f5c9b2e6cbf29829c4ebc。
执行Python：C:\Users\DELL\AppData\Local\Programs\Python\Python313\python.exe。
运行前已存在未提交修改：reports/eval_answer.json、reports/eval_e2e.json、reports/failed_cases/eval_answer_failed_cases.json、reports/failed_cases/eval_e2e_failed_cases.json。
运行前未跟踪：.agents/、data/eval/candidates/、data/eval/design/、data/eval/reviews/。不能将这些文件误记为HEAD已包含。
app业务代码和scripts/eval/eval_rag.py无git已跟踪修改。运行前后120个受保护文件SHA-256对照，0变化（app及eval脚本Python文件、KB Markdown、原Candidate/review、V1、validation、Schema/Validator）。

关键实现哈希：
- D:\python-project\客服agent\app\rag\query_builder.py：886338303809FE3C45C0798A1C26779D5982377AB2A3129E9600988996B06161
- D:\python-project\客服agent\app\rag\ranking.py：3304D8703429C429C122D1574EA5F9CC5F354948F3ECF78A2521E44B1B4B6DF3
- D:\python-project\客服agent\app\rag\retriever.py：EDD9A74CFCD2D1E4D84BF7B059EFBEC65B330812566800B851E1954A5FB0204F
- D:\python-project\客服agent\app\rag\ingestion\chunker.py：14FFF733D4B1E22CE9A2E1F9B96BA9A73A2514B8C9B39DDE8FF22F837618C134
- D:\python-project\客服agent\scripts\eval\eval_rag.py：C1DD88CE3A421CED120F4C83C93AFB88E57D83883DC095F4779A310631200322

实际入口默认fixed_256（256个自定义token，overlap32；中文单字、英文词等分词规则），chunker token-strategy-v3。未改变分块配置。
实际candidate_k=20（evaluator常量），top_k=5（CLI默认），semantic-query-mode=rerank（CLI默认）。
get_settings实际rag_ranking_mode=hybrid_rule，与evaluator的BASELINE_MODE一致。
召回为hybrid_vector_bm25_keyword，权重semantic/BM25/keyword=0.62/0.28/0.10，candidate_multiplier=4。
实际embedding身份为local|local_hash_v1|256|document，不是远程embedding-3；配置里的远程dimensions=1024未被本次local provider采用。
Query：input_context经case_query_context送入Query Builder；semantic_query为原query+已提供事实；lexical_query加入既有意图/主题词和事实；rerank_query包含原问题、业务语义及事实。无expected注入。原始及实际三种query字符串保存在每案query_contract。
action_type默认null、handoff_required默认false的上下文展开是已有实现行为，不是本次数据新增事实；notes/tags不参与匹配或动作验证。

evaluator没有单模式CLI开关。仅一次正常调用固定运行5个内置模式，以下以hybrid_rule主基线报告，不按最好模式挑选分数，不重复调用扫描参数。
Cross-Encoder为内置其他模式加载；hybrid_rule本身使用规则重排。

实际运行元数据（来自正常输出）：

```json
{
  "runner_version": "rag-ablation-v1",
  "run_timestamp": "2026-09-07T21:42:37",
  "dataset": {
    "path": "D:\\python-project\\客服agent\\data\\eval\\rag_eval_dev_v2_batch01.jsonl",
    "version": "sha256:d03fb99776ab81c498eb9630cf3b69414ded409e5161537fc6552e2658085c89"
  },
  "kb_version": "kb_08b60d90794c5afd235bdb5c",
  "candidate_k": 20,
  "top_k": 5,
  "semantic_query_mode": "rerank",
  "hybrid_retrieval": {
    "semantic_weight": 0.62,
    "bm25_weight": 0.28,
    "keyword_weight": 0.1,
    "candidate_multiplier": 4
  },
  "embedding_identity": "local|local_hash_v1|256|document",
  "semantic_reranker": {
    "provider": "sentence_transformers_cross_encoder",
    "model": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    "revision": "1427fd652930e4ba29e8149678df786c240d8825",
    "library": "sentence-transformers",
    "library_version": "6.0.1",
    "device": "cpu",
    "batch_size": 8,
    "max_length": 512,
    "input_version": "candidate-representation-v1"
  },
  "rule_reranker": {
    "mode": "hybrid_rule",
    "version": "legacy-business-rules-v1"
  },
  "business_constraint": {
    "version": "business-evidence-constraint-v1",
    "semantic_mapping": {
      "address_change": "order_change",
      "address_change_apply": "order_change",
      "address_change_policy": "order_change",
      "cancel_apply": "order_change",
      "cancel_order": "order_change",
      "cancel_policy": "order_change",
      "complaint": "complaint",
      "duplicate_charge": "payment_invoice",
      "escalation": "complaint",
      "invoice_apply": "payment_invoice",
      "invoice_change": "payment_invoice",
      "invoice_policy": "payment_invoice",
      "lost_package": "shipping",
      "membership": "membership",
      "membership_benefit": "membership",
      "membership_policy": "membership",
      "payment_failed": "payment_invoice",
      "payment_invoice": "payment_invoice",
      "payment_status": "payment_invoice",
      "product_failure": "product_after_sales",
      "refund_apply": "refund",
      "refund_eligibility": "refund",
      "refund_policy": "refund",
      "refund_timing": "refund",
      "repair_apply": "product_after_sales",
      "replacement": "product_after_sales",
      "return_apply": "refund",
      "return_refund": "refund",
      "shipping_delay": "shipping",
      "shipping_exception": "shipping",
      "shipping_policy": "shipping",
      "shipping_status": "shipping",
      "warranty_policy": "product_after_sales",
      "warranty_repair": "product_after_sales"
    }
  }
}
```

知识库身份：kb_08b60d90794c5afd235bdb5c，内存索引经manager.refresh重建/激活。磁盘data/cache/knowledge_manifest.json运行后SHA-256=A181F4FBC4701E48F5FC0FFA294478CA0A1F8728D9DEA76E4C7FE65E5D6E55A7。
未事先记录manifest哈希，故不声称其字节未变；索引刷新、manifest保存及缓存写入属于原evaluator正常副作用。实际cache hit/miss未在报告披露，不可观测。完整向量内容标识未输出。
未改变知识库，相关KB文件哈希见文末。

## 4. 一次实际调用与结果

```powershell
$env:PYTHONIOENCODING='utf-8'
python -B -m scripts.eval.eval_rag --dataset data/eval/rag_eval_dev_v2_batch01.jsonl --experiment-name eval_rag_dev_v2_batch01_baseline
```

运行时间：2026-09-07T21:42:37（本地Asia/Shanghai）。
进程退出码1：评测完成但存在失败案例，不是执行异常；报告无execution_error。
加载权重完成；HF Hub给出未认证请求限流提示，但未阻塞本次运行。未设置token或更换模型。
正常结果：[完整报告](../../../reports/eval_rag_dev_v2_batch01_baseline.json)、[失败案例](../../../reports/failed_cases/eval_rag_dev_v2_batch01_baseline_failed_cases.json)。

| 模式（一次调用内置输出） | 通过 | Hit@1 | Hit@3 | Hit@5 | MRR | 宏平均Coverage@5 |
|---|---|---|---|---|---|---|
| hybrid | 2/6 | 0.5 | 0.8333 | 1 | 0.6806 | 0.7167 |
| hybrid_rule | 2/6 | 0.6667 | 0.8333 | 0.8333 | 0.75 | 0.65 |
| hybrid_semantic | 1/6 | 0.3333 | 0.6667 | 0.8333 | 0.5056 | 0.5917 |
| hybrid_semantic_constraint | 1/6 | 0.3333 | 0.6667 | 0.8333 | 0.5056 | 0.5917 |
| hybrid_semantic_fusion | 2/6 | 0.3333 | 0.6667 | 1 | 0.5667 | 0.7583 |

主基线hybrid_rule：2/6通过；必要证据Top5共11/19（微平均57.89%）；报告宏平均65%（逐案覆盖率平均），两种口径不混淆。Top20共18/19，报告Recall@20宏平均91.67%。主目标6/6进入候选，Top5主目标5/6。

## 5. 静态存在、容量与真实Top5的区别

沿用原review fixed_256静态匹配，不重跑切块策略实验、不注入任何静态chunk到实际候选或结果。
原19项保留requirement匹配条件未变，均已有单项匹配见证；001只改query不影响静态GT匹配，003/006只删项。
从旧见证/联合覆盖信息可容纳全部保留证据与主目标的chunk数量分别为1、3、2、3、4、1（均≤5）。这是全库存在可行组合，不是实际召回或排名保证。
实际候选匹配集合也证明001/002/004/005/006分别可用1/3/3/4/1个候选chunk同时满足所有要求；003候选集缺退款资格，任何Top20子集都不能补全。
真实Top5只有001与006同时满足全案必要证据。其余情况见下面逐项结果。没有案例因所需chunk数量必然超过top_k=5而失败。

| case尾号 | 主目标候选首排 | 主目标最终Top5首排 | Hit@1/3/5 | 候选证据 | Top5证据 | 结果 |
|---|---|---|---|---|---|---|
| 001 | 1 | 2 | 0/1/1 | 3/3 | 3/3 | passed |
| 002 | 1 | 1 | 1/1/1 | 5/5 | 2/5 | evidence_coverage_failure |
| 003 | 2 | 1 | 1/1/1 | 1/2 | 1/2 | retrieval_failure |
| 004 | 4 | N/A | 0/0/0 | 4/4 | 2/4 | ranking_failure |
| 005 | 1 | 1 | 1/1/1 | 4/4 | 2/4 | evidence_coverage_failure |
| 006 | 3 | 1 | 1/1/1 | 1/1 | 1/1 | passed |

N/A指主目标未进最终Top5；不推测Top5之外的规则重排精确排名。证据表以新Dev E序号区分，同名evidence_type不能合并。

### rag_dev_candidate_b01_001 / RAG-DEV-S020

通过。完整基线三项均命中同一chunk，候选rank1→最终rank2。最终rank1为退换货限制/已发货等混合chunk，不能把该排序直接解释为回答错误。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 refund_eligibility；source=退换货政策.md；section=七天无理由退货；keywords_all=["签收之日起 7 天内","商品保持完好","商品配件齐全","商品包装不存在严重损坏","不影响商品二次销售","商品不属于不支持七天无理由退货的特殊商品"] | 命中 | 命中 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3；最终：doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3 |
| E2 refund_eligibility；source=退换货政策.md；section=七天无理由退货；keywords_all=["满足以上条件后，可以创建退货申请"] | 命中 | 命中 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3；最终：doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3 |
| E3 refund_timing；source=退换货政策.md；section=七天无理由退货；keywords_all=["商品退回并完成质检后，系统进入退款流程"] | 命中 | 命中 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3；最终：doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 2 | 退换货政策.md / 不支持七天无理由退货的情况 | 1.4284 / 0.575 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3 |
| 2 | 1 | 退换货政策.md / 退款与退货政策 | 1.395 / 0.435 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3 |
| 3 | 12 | 退换货政策.md / 七天无理由退货 | 1.263 / 0.55 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:cf00176f12749e3bd1f09805 |
| 4 | 10 | 商品售后规则.md / 七天无理由退货 | 1.2447 / 0.51 | doc_8ae3fda162d548bd3b86efff:chunk:077197e5bda580bfec3853a8 |
| 5 | 4 | 历史问题案例.md / 案例二：耳机人为进水未免费换新 | 1.1983 / 0.395 | doc_9e26bcd1585c743b0269eb0c:chunk:1aa671cbe0e5e50088d5a342 |

### rag_dev_candidate_b01_002 / RAG-DEV-S021

evidence_coverage_failure：E3/E4匹配chunk在候选rank3，E5在rank12，均未进入Top5。最终rank2/3/4为七天无理由或不支持无理由等chunk，rule_boost分别0.445/0.46/0.42。现有结果支持最终选取遗漏必要证据，不是知识缺失或Top5容量不足；未输出落选chunk的完整规则排序分数，不能断言唯一加分根因。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 custom_product；source=商品售后规则.md；section=定制商品；keywords_all=["不能因为商品属于定制商品，就拒绝所有质量问题售后请求"] | 命中 | 命中 | doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c；最终：doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c |
| E2 custom_product；source=商品售后规则.md；section=定制商品；keywords_all=["是否能够退款或者重新制作","根据实际问题和人工审核结果判断"] | 命中 | 命中 | doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c；最终：doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c |
| E3 product_quality；source=商品售后规则.md；section=商品质量问题；keywords_all=["不能仅根据用户描述直接认定商品属于质量问题"] | 命中 | 缺失 | doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c；最终：无 |
| E4 product_quality；source=商品售后规则.md；section=商品质量问题；keywords_all=["检测结果确认属于产品质量问题后","根据对应政策决定后续处理方式"] | 命中 | 缺失 | doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c；最终：无 |
| E5 refund_eligibility；source=退款政策.md；section=退款资格判断；keywords_all=["只有满足对应退款条件后，才能继续进入退款申请流程"] | 命中 | 缺失 | doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6；最终：无 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 1 | 商品售后规则.md / 定制商品 | 1.2764 / 0.305 | doc_8ae3fda162d548bd3b86efff:chunk:906ead21f528b71a86c26c6c |
| 2 | 6 | 退换货政策.md / 七天无理由退货 | 1.2045 / 0.445 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:cf00176f12749e3bd1f09805 |
| 3 | 9 | 商品售后规则.md / 七天无理由退货 | 1.1823 / 0.46 | doc_8ae3fda162d548bd3b86efff:chunk:077197e5bda580bfec3853a8 |
| 4 | 10 | 退换货政策.md / 不支持七天无理由退货的情况 | 1.1377 / 0.42 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3 |
| 5 | 4 | 退换货政策.md / 退款与退货政策 | 1.1092 / 0.305 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:a0f1e7a9a74b4ac71b611eb3 |

### rag_dev_candidate_b01_003 / RAG-DEV-S003

retrieval_failure：新E1（原E3）的检测不明人工出口候选rank2→最终rank1；新E2（原E4）退款资格在Top20即无匹配。Top5中4条来自商品售后规则，另1条来自退换货政策，没有退款政策。初步定位候选召回覆盖缺口；Top20以外全库检索排名不可观测，不能归因具体embedding或Query模块。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 human_review；source=商品售后规则.md；section=商品质量问题；keywords_all=["如果检测结果无法确认","可以进入人工审核"] | 命中 | 命中 | doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c；最终：doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c |
| E2 refund_eligibility；source=退款政策.md；section=退款资格判断；keywords_all=["只有满足对应退款条件后，才能继续进入退款申请流程"] | 缺失 | 缺失 | 无；最终：无 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 2 | 商品售后规则.md / 电子产品 | 1.5627 / 0.645 | doc_8ae3fda162d548bd3b86efff:chunk:39cc6b8b40701f053cbd828c |
| 2 | 6 | 商品售后规则.md / 七天无理由退货 | 1.4547 / 0.645 | doc_8ae3fda162d548bd3b86efff:chunk:077197e5bda580bfec3853a8 |
| 3 | 4 | 商品售后规则.md / 家电商品 | 1.4257 / 0.565 | doc_8ae3fda162d548bd3b86efff:chunk:9947f1734a98542173531484 |
| 4 | 1 | 退换货政策.md / 七天无理由退货 | 1.3789 / 0.445 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:cf00176f12749e3bd1f09805 |
| 5 | 8 | 商品售后规则.md / 售后处理类型 | 1.3045 / 0.505 | doc_8ae3fda162d548bd3b86efff:chunk:d11503fecd622bbfc8b8c7cc |

### rag_dev_candidate_b01_004 / RAG-DEV-S006

ranking_failure：主目标及E1候选rank4，E2候选rank7，最终均落选；E3/E4在Top5命中。最终5条全部来自物流规则/物流配送政策，记录含shipping_exception和已发货加分。候选已包含满足所有要求的3个chunk，故不是容量不足；支持最终排名/选取偏向物流证据的初步解释，不断言单个规则因果。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 shipping_intercept；source=退换货政策.md；section=已发货订单；keywords_all=["无法拦截时","等待商品送达或者拒收后","再按照退货流程处理"] | 命中 | 缺失 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3；最终：无 |
| E2 order_cancel；source=订单取消与修改政策.md；section=已发货订单；keywords_all=["不能按照普通待发货订单直接取消"] | 命中 | 缺失 | doc_41203502602eba8a5ebda54a:chunk:22153be8fe710e0957519fb5；最终：无 |
| E3 shipping_status；source=物流配送政策.md；section=用户拒收；keywords_all=["需要等待物流状态更新"] | 命中 | 命中 | doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7<br>doc_b3377cfe0dac262b10bf7bd4:chunk:109aaccc7b3e5580f9af3985；最终：doc_b3377cfe0dac262b10bf7bd4:chunk:109aaccc7b3e5580f9af3985<br>doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7 |
| E4 refund_eligibility；source=物流配送政策.md；section=用户拒收；keywords_all=["商品退回仓库并完成确认后","根据订单情况进入退款或者退货流程"] | 命中 | 命中 | doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7；最终：doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 2 | 物流规则.md / 物流异常工单 | 1.4409 / 0.51 | doc_e624c6825315857037f266a7:chunk:5f2cacfa16faf5bad7446e4e |
| 2 | 10 | 物流配送政策.md / 物流异常 | 1.392 / 0.59 | doc_b3377cfe0dac262b10bf7bd4:chunk:109aaccc7b3e5580f9af3985 |
| 3 | 12 | 物流规则.md / 物流异常与退款 | 1.3258 / 0.565 | doc_e624c6825315857037f266a7:chunk:55f8e226724fd552c0a8bde2 |
| 4 | 1 | 物流配送政策.md / 用户拒收 | 1.2768 / 0.29 | doc_b3377cfe0dac262b10bf7bd4:chunk:bfd18572fe1a2a47dff6c6e7 |
| 5 | 5 | 物流规则.md / 人工处理 | 1.2434 / 0.41 | doc_e624c6825315857037f266a7:chunk:83f3aff293dfb35b9e03e137 |

### rag_dev_candidate_b01_005 / RAG-DEV-S019

evidence_coverage_failure：E2候选rank9、E4候选rank8均未进Top5；E1最终rank1、E3最终rank5命中。Top5另外含投诉识别/升级和两条售后FAQ。命中包含处理原则的主章节不等于完整处理原则已齐备。根据旧静态记录E1/E2分布两个chunk，必须分别检查，不删减关键词。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 complaint_escalation；source=投诉升级SOP.md；section=处理原则；keywords_all=["投诉升级不代表用户提出的要求一定成立"] | 命中 | 命中 | doc_acfdb96dc65ed74f58613bc9:chunk:b80d2ab28186ef71990d202b；最终：doc_acfdb96dc65ed74f58613bc9:chunk:b80d2ab28186ef71990d202b |
| E2 complaint_escalation；source=投诉升级SOP.md；section=处理原则；keywords_all=["根据订单事实、平台政策以及人工审核结果决定"] | 命中 | 缺失 | doc_acfdb96dc65ed74f58613bc9:chunk:f0e57c6dbcc58932d1a866b3；最终：无 |
| E3 refund_risk_review；source=退款政策.md；section=退款人工审核；keywords_all=["以下情况可能进入人工审核","投诉升级并涉及资金操作"] | 命中 | 命中 | doc_253629df931f97428ff46d6e:chunk:29987d965eb8a2bc8a2a8083；最终：doc_253629df931f97428ff46d6e:chunk:29987d965eb8a2bc8a2a8083 |
| E4 refund_eligibility；source=退款政策.md；section=退款资格判断；keywords_all=["只有满足对应退款条件后，才能继续进入退款申请流程"] | 命中 | 缺失 | doc_253629df931f97428ff46d6e:chunk:934ec7c616508616a9f030e6；最终：无 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 1 | 投诉升级SOP.md / 投诉升级 | 1.0921 / 0.105 | doc_acfdb96dc65ed74f58613bc9:chunk:b80d2ab28186ef71990d202b |
| 2 | 2 | 投诉升级SOP.md / 投诉升级SOP | 1.0515 / 0.065 | doc_acfdb96dc65ed74f58613bc9:chunk:c923e482ed128f2cd97e9b66 |
| 3 | 3 | 售后FAQ.md / 已经扣款但是订单显示没支付怎么办？ | 1.0034 / 0.185 | doc_b1f79b4599410856d608f1ef:chunk:223412a428195591f3e37e5b |
| 4 | 13 | 售后FAQ.md / 我想投诉客服怎么办？ | 0.8521 / 0.185 | doc_b1f79b4599410856d608f1ef:chunk:15241fb616e81e76b7997f70 |
| 5 | 6 | 退款政策.md / 退款处理状态 | 0.8392 / 0.065 | doc_253629df931f97428ff46d6e:chunk:29987d965eb8a2bc8a2a8083 |

### rag_dev_candidate_b01_006 / RAG-DEV-S022

通过。唯一E1的未知结果、不得直接重复执行、查询实际状态或人工处理全部命中；候选rank3→最终rank1。实际section为退款人工审核，但covered_sections包含退款失败。章节名不代表输入已认定FAILED；未验证真实查询、资金执行、重复保护或回答措辞。

| Requirement（完整匹配条件） | Top20 | Top5 | 匹配chunk（候选；最终） |
|---|---|---|---|
| E1 refund_idempotency；source=退款政策.md；section=退款失败；keywords_all=["如果无法确认退款是否已经成功","不得直接重复执行退款","应先查询实际退款状态或者进入人工处理"] | 命中 | 命中 | doc_253629df931f97428ff46d6e:chunk:17797a64638403575a58c349；最终：doc_253629df931f97428ff46d6e:chunk:17797a64638403575a58c349 |

实际Top5轨迹（source/section为真实输出，章节覆盖见原始ranking_trace.covered_sections；分数仅说明观测，不证明规则正确）：

| 最终rank | 候选rank | source / section | rule_score / boost | chunk_id |
|---|---|---|---|---|
| 1 | 3 | 退款政策.md / 退款人工审核 | 1.412 / 0.55 | doc_253629df931f97428ff46d6e:chunk:17797a64638403575a58c349 |
| 2 | 7 | 商品售后规则.md / 七天无理由退货 | 1.3312 / 0.55 | doc_8ae3fda162d548bd3b86efff:chunk:077197e5bda580bfec3853a8 |
| 3 | 1 | 历史问题案例.md / 案例十一：用户重复提交退款 | 1.2964 / 0.37 | doc_9e26bcd1585c743b0269eb0c:chunk:be82f3ef745fdf5645001341 |
| 4 | 5 | 历史问题案例.md / 案例九：高金额退款进入人工审核 | 1.2558 / 0.45 | doc_9e26bcd1585c743b0269eb0c:chunk:0a37233ff4dcf3069d492d96 |
| 5 | 10 | 退换货政策.md / 不支持七天无理由退货的情况 | 1.2315 / 0.47 | doc_61ae6301a2e6d1ddf7fc0bc4:chunk:bee5613ae656ee0fc5bac7f3 |

## 6. 可观测性与停止点

S003：新E1人工审核分支命中，候选2/最终1；本次恰与章节主目标最终排名相同，不能将两个指标一般化为等价。新E2退款资格缺失于候选及最终集。已移除原E1/E2不参与缺失清单或supporting。
S022：唯一保留E1命中，候选3/最终1。只证明规则证据召回，不证明实际状态查询、退款执行、重复操作保护或回答正确；没有加入自动系统状态或失败状态。
S019：E1命中不代表E2完整原则命中，更不代表用户符合退款资格；本次E2/E4实际缺失。

evaluator提供候选chunk ID、候选证据匹配明细、Top5 ranking_trace及text_preview。完整Top20文本/全部落选候选最终规则分数、Top20之外排名、实际动作与答案正确性没有输出，标记不可观测，不推测。初步原因限定在当前可观测的召回覆盖/最终选取层，不自动把所有低分归为排序根因。

新增交付：
- data/eval/rag_eval_dev_v2_batch01.jsonl
- data/eval/reviews/rag_dev_v2_batch01_derivation_baseline.md
- reports/eval_rag_dev_v2_batch01_baseline.json（evaluator正常输出）
- reports/failed_cases/eval_rag_dev_v2_batch01_baseline_failed_cases.json（evaluator正常输出）

无业务/evaluator/Schema/Validator改动；原Candidate/review/V1/validation均保持原字节。未修改split以外未批准内容，唯一notes同步修正为003。未进行排序优化，不因低分修改expected。人工接纳是数据审核结论，不等于系统评测通过。

### KB与原始审核快照SHA-256

- D:\python-project\客服agent\data\knowledge\人工审核SOP.md：82009E364064390A399149A9FAF527B2F3922D0C827B83D54E2B3E3F01BB557C
- D:\python-project\客服agent\data\knowledge\会员权益政策.md：F82EF9EE47CC93F4863C19C75A8F46C1C176EBE59B2F55B32C8AFBEFF28AE84C
- D:\python-project\客服agent\data\knowledge\保修政策.md：FFD154556E803ED4833EB8D4436BF18136A94F79C1E4DBC3D42392380CD2DD58
- D:\python-project\客服agent\data\knowledge\历史问题案例.md：FCB85256604DE188864252B9814E00AAADDECB665BD9E57EC10065445C43C13D
- D:\python-project\客服agent\data\knowledge\售后FAQ.md：72500DDA632AF6AC0E3CE248BD13A58514544812D66E6CE01CB1E15A597A943C
- D:\python-project\客服agent\data\knowledge\商品售后规则.md：12096EC9AD50C7527B32C60C9B0B6C31881DD8524D47130FE8CCE391FA2AFC5D
- D:\python-project\客服agent\data\knowledge\商品说明文档.md：F9FD274A4A0496A6180A49A1C429BF9F7A3EE819EFA93AA462DC6FA0DE84C861
- D:\python-project\客服agent\data\knowledge\客服SOP.md：4ED55E51A56963C306E1BADF5CC76A1179823E059C489B98CF97CB1006FBF8BC
- D:\python-project\客服agent\data\knowledge\投诉升级SOP.md：B1341E45759DC62DD17CAE1D8D40ABF6172429BC9E5667FEEFF5480CF1F25617
- D:\python-project\客服agent\data\knowledge\支付与发票政策.md：9686A12269EB31D49DC51C845FE745BE4A325702F4F133029CF9D3C522B7E2B2
- D:\python-project\客服agent\data\knowledge\物流规则.md：B071E76BFB1764073CF605EEA4F60E1D8E22B724C477DCC08AD89827A5B131A5
- D:\python-project\客服agent\data\knowledge\物流配送政策.md：4E69477EF4F4E9B4C87E791897F59B48E257D2D6445717387CDF64E1F01769D6
- D:\python-project\客服agent\data\knowledge\订单取消与修改政策.md：AD14B22598BEA9308B45A90CC01BDCE0D19F9456CA78B25E549629EE9CBF8007
- D:\python-project\客服agent\data\knowledge\退换货政策.md：BA7A31D50D49E14176A9C221706A1EF07BDDD14F5A9C7D3FCA985CAE741B2D90
- D:\python-project\客服agent\data\knowledge\退款政策.md：87AC9A39F5901439F43B21010C9CD219751673DB6915D6A998A54A7B47005EA2
- D:\python-project\客服agent\data\eval\rag_eval.jsonl：666BF6895C7737926BEB782275F98DFB4C22DEE47AFB5A0C678B0ACD5D7CB299
- D:\python-project\客服agent\data\eval\rag_eval_holdout.jsonl：1734F22A5A5B6C06AF5116AC6F3958F3AB8C02226105404564C353B23DAEC591
- D:\python-project\客服agent\data\eval\candidates\rag_dev_candidates_batch01.jsonl：6A6A235A5C902C5B28501763FB75E1BCC49ED58C004AAED89B3612E0030E4197
- D:\python-project\客服agent\data\eval\reviews\rag_dev_candidates_batch01_review.md：56EAD444CDEE806795FB3F0E204182CE64474FACBB4A9316A3C4B992A12C90AF
- D:\python-project\客服agent\.agents\skills\eval-dataset-builder\schemas\rag.schema.json：C8669075852C7694DFADC399F8C9B6EBF92D32096D20A0BD7A48889F99C8C8AB
- D:\python-project\客服agent\.agents\skills\eval-dataset-builder\scripts\validate_dataset.py：9C8EACED627CA0754A495695A0EA4BC4AD1F56393B8B1D77FD1C52CBF247E11C

