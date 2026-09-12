# 冗余清理与关键词规则审计

2026-09-11。已清理两处确定无用的代码，共删除 21 行；**所有关键词规则、词表、评分和配置均未修改**。

## 已完成的清理

1. 删除 `app/agent/routing/llm_router.py` 中无人调用的 `_fallback_semantic_route()`。项目源码、测试及脚本引用搜索只找到定义；实际 `infer_semantic_route()` 的异常分支直接构造同样的安全降级结果，保留该分支。
2. 删除 `app/services/payments.py` 未使用的 `authorize_order` 导入。未删除任何权限检查；支付可见性和管理操作仍走原业务授权路径。

没有把 `__init__.py` 的公共导出当成未使用导入，也没有仅因仓库内无调用就删除公开数据库接口 `load_orders_from_db()`。没有清理旧实验、知识库或测试集。

回归：**41 项测试、21 个子测试通过**，涉及语义修复、辅助主题、支付对账及 API 权限；3 个既有弃用警告。见 [targeted_tests.txt](targeted_tests.txt)。首次隔离副本遗漏 `main.py` 和 `scripts`，产生导入失败；补齐副本后通过，原日志保留在 `initial_test_environment_failure.txt`。未运行五层测评。

## 查找范围和判断口径

扫描 `app/**/*.py`、`scripts/**/*.py` 和 `main.py`，共 **159 个 Python 文件**；另检查 `web/index.html` 和测试中的引用。宽口径 AST 扫描保留 **432 处成员判断或文本/正则操作候选**，见 [scan_candidates.json](scan_candidates.json)。432 不是业务关键词规则数量，包含枚举判断、JSON 解析等非关键词逻辑。

下表按承担的功能归类，而不是把相同词语出现多次就判定为重复代码。范围不包括 `.env`、虚拟环境、历史报告中的源码副本；知识文档、测试数据和语义提示词中的自然语言也不冒充运行时子串匹配规则。

## 已有依据、可以等价删的条目（本轮未删）

这些常量的现有调用使用 `any(词 in 文本)`，不计数、不按匹配词打分。若长词包含短词，命中长词必然已经命中短词，因此删除长词不改变该布尔结果。

| 文件 / 常量 | 可移除的长词 | 已覆盖它的短词 |
|---|---|---|
| `routing/decision.py` / `REVIEW_BYPASS_PATTERNS` | 跳过审核流程 | 跳过审核 |
| 同上 | 绕过审核流程 | 绕过审核 |
| 同上 | 别走审核流程 | 别走审核 |
| 同上 | 不要走审核流程 | 不要走审核 |
| `policies/fallback_policy.py` / `ORDER_ID_REQUIRED_KEYWORDS` | 我的订单 | 订单 |
| 同上 | 退货仓库 | 退货 |
| 同上 | 修改地址 | 改地址 |
| 同文件 / `RISKY_OPERATION_KEYWORDS` | 修改地址 | 改地址 |
| 同文件 / `RISKY_ACTION_KEYWORDS` | 修改地址 | 改地址 |
| `routing/conversation_context.py` / `ISSUE_CONTEXT_KEYWORDS` | 修改地址 | 改地址 |

上表路径均位于 `app/agent/`。完整路径、常量及行号见 [redundant_phrases.json](redundant_phrases.json)。这是 **10 个词表条目**，不是 10 个可删除的模块；这个推理不能用于按命中数计分的 RAG 词表。

## 线上语言与业务判断清单

以下“可替换”均不表示现在可以直接删。

| 位置 / 函数 | 真实用途与调用关系 | 删除判断 |
|---|---|---|
| `app/agent/policies/guardrails.py` / `check_user_input` | Router 调用 LLM 前按注入词拒绝输入 | 可替换为有上下文的输入检查；当前不是死代码，直接删会失去这层拦截，词表本身也不构成完整安全防线 |
| 同文件 / `contains_risky_action` | `decision.build_route_decision` 在 execute 时增加风险信号，影响风控及说明 | 可与统一风险决策合并，但不等同于退款风控，不能整段直接删 |
| `app/agent/policies/fallback_policy.py` / `requires_order_id` | `conversation_context` 在语义路由之前判断是否继承历史订单或问题 | 可由结构化会话绑定替换；与 `decision.requires_order_id(SemanticRoute)` 名字相同但职责、调用阶段不同 |
| 同文件 / `is_risky_operation`、`should_handoff_to_human` | `decision` 对 execute 请求补人工升级决策 | 优先检查的旧规则；改地址已有结构化审核路径，但此处还影响 handoff，删除并非等价 |
| `app/agent/routing/decision.py` / `requests_review_bypass` | execute 请求的绕过审核提示，触发人工、风控 | 可把文本解释迁入结构化风险字段，强制审核约束仍需保留；4 个冗余长词可等价删 |
| `app/agent/routing/conversation_context.py` / `is_short_followup`、`should_inherit_order_id`、`find_recent_issue_message` | 短问句词表和历史搜索决定补入什么上下文 | 可替换，前提是显式保存当前订单、话题及切换行为；目前直接删会影响多轮补全 |
| `app/agent/routing/pending_task.py` / `is_address_question` | 问号、能否、怎么等表达避免咨询被当作地址值 | 可替换成结构化槽位提取，但不能先撤掉防误执行限制 |
| 同文件 / `cancels_pending_request`、`pending_message_kind` | 精确取消词、业务切换词、显式订单格式区分取消/替换/补槽 | 保留到任务状态识别有等效覆盖；它们会改变待处理申请生命周期 |
| 同文件 / `is_address_change_request`、`extract_new_address`、`collect_slots` | 改址触发词、否定清理和“寄到/修改为”等前缀抽取地址 | 可迁入有 schema 的提取器；需覆盖疑问、否定、切换订单和地址正文含数字 |
| `app/agent/routing/refund_withdrawal.py` / `mentions_refund_withdrawal` | Router 在 LLM 前识别撤销退款，转入选择已有申请的指导流程 | 不宜直接删；当前语义主题契约没有等效撤销主题，删除可能落入新申请路径 |
| `app/agent/routing/parsing.py` / `extract_order_id` | 4 位及以上数字提取，路由和记忆均使用 | 格式解析，不是意图规则；可收紧或替换，但不能随关键词分类器一起删 |
| `app/domain/risk_policy.py` / `evaluate_refund_risk` | 投诉词、绕过词、未收到与已签收冲突给风险分，决定审核 | 文本信号可改造；金额、账号、频次等结构化规则不是关键词规则，不能一起删 |
| `app/domain/refund_policy.py` / `is_quality_or_fault_request`、`infer_refund_reason` | 质量词和退款原因分类；质量判断还影响定制品退款资格 | 优先改造，不能只删：这已进入业务资格判断，需要结构化原因及事实核实替代 |
| 同文件 / `require_confirmed_refund_payment`、`existing_after_sales_reason`、`evaluate_refund_eligibility` | 待支付、退款、定制、已发货等字段子串决定支付/售后/资格限制 | 先统一状态枚举和商品属性再删文本兼容；保留支付确认和不可重复申请约束 |
| `app/agent/policies/ticket_policy.py` / `extract_shipping_time`、`evaluate_ticket_creation` | 从物流文本取日期，“已签收”“未超过48”“待发货”影响是否建单 | `notes` 文本分支优先改为可信 `last_update_at`；没有结构化时间时删除会放宽建单条件 |
| `app/services/review_service.py` / `resolve_review` | 退款/退货审核中的订单文本防止重复退款 | 可与统一售后状态判断合并，不能直接去掉并发事务内的业务复核 |
| `app/services/review_continuation.py` / `require_unshipped_order` | 物流文本中的已发货等标记阻止取消/改址 | 状态规范化后可删除子串兼容，正式执行前状态校验必须保留 |
| `app/agent/policies/evidence_guardrail.py` / `detect_policy_profile` | 有有效 route 时按 intent；无 route 时按触发词选政策类型，并优先退款 | 无 route 的回退可在所有调用传入语义上下文后删除；目前检索选择器和离线 evaluator 仍依赖它 |
| 同文件 / `validate_policy_evidence` | Top3 来源匹配、必要词任一命中判断证据是否可用 | 不能等同于完整规则覆盖；可替换为更明确的证据契约，但不能只取消失败拦截 |
| `app/agent/response/grounded.py` / `policy_excerpts` | `_INTERNAL` 检测 MQ、幂等、数据库等，整段排除；另有完整段落/句尾判断 | 内部词整段过滤可能丢掉同段业务规则，宜改为内容标注或更细片段；当前直接删会暴露内部叙述，句子完整性约束也应保留 |

## RAG、知识处理与技术规则清单

| 位置 | 用途 | 删除判断 |
|---|---|---|
| `app/rag/embedding_client.py` / `CUSTOMER_KEYWORDS`、`tokenize` | 业务词切分，供本地 hash embedding 和词法检索使用 | 不是 Router；移除会影响向量/词法表示，不能当作无用意图规则 |
| 同文件 / `keyword_score`；`hybrid_index.py`；`retriever.py` | 查询业务词在来源/正文中的匹配加分，进入混合召回及缓存配置 | 可独立做召回消融后决定；Cross-Encoder 不会自动替代候选召回，此轮没有实验依据判可删 |
| `app/rag/query_builder.py` / `INTENT_RETRIEVAL_TERMS`、`TOPIC_RETRIEVAL_TERMS` | 根据结构化 intent/topic 扩展词法查询 | 这是语义到检索词映射，不是原始文本关键词路由；删会改变召回 |
| `app/rag/reranker.py` / `BUSINESS_RERANK_RULES`、`keyword_coverage_score`、`state_match_score`、`business_rule_score` | 词覆盖、状态短语、来源和章节加分 | 仅在相应排序模式生效，但模式仍支持；可以逐项消融，不能凭有 Cross-Encoder 就删除 |
| `app/rag/ranking.py` / `candidate_evidence_categories` | 元数据与来源名共同判断证据类别 | 有冗余来源猜测倾向；元数据完整、语义等效后可删除来源名回退，目前来源可补额外类别 |
| `app/rag/evidence_selection.py` / `_guardrail_masks` | 重新按查询触发词维护旧证据护栏 | 优先统一其语义上下文；当前与线上有 route 的护栏存在不一致，不能直接删保护条件 |
| 同文件 / `_coverage`、`_query_facets`、`select_complementary_evidence` | 业务词/词组、字符片段、结构标签及文档引用决定互补证据 | 活跃实验能力；删除会改变选择结果，需固定候选池验证。字符 n-gram 相似度不全是业务关键词判断 |
| `app/rag/ingestion/metadata.py` / `classify_document` | 从文件名猜知识类别，之后再由路径、front matter、显式元数据覆盖 | 显式元数据覆盖完整后可删除猜测；目前回退仍参与入库 |
| `app/rag/ingestion/loader.py` | 标题正则及“政策/范围/要求”等后缀识别章节 | 文档解析规则，删会改变章节及分块结果，非本轮可删项 |
| `app/tools/executor.py` / `sanitize_trace_value` | password/token/secret 等敏感字段名脱敏 | 保留，不能因为它使用字符串匹配就删除 |
| 同文件 / 错误分类 | SQLite locked/busy 文本识别可重试异常 | 保留，除非改用同等可靠的错误码分类 |
| `app/core/security.py`、`services/payments.py`、`storage/*`、`observability/*` | Bearer/配置解析、渠道/业务状态枚举、数据库类型和指标分类 | 技术协议与状态判断；不是用户意图关键词规则 |
| `app/agent/routing/llm_router.py` | 去代码围栏、提取 JSON、校验 intent/action/topic 枚举 | 输出契约解析，不是关键词替代 LLM；保留 |
| `app/agent/response/grounded.py` 的 evidence ID 检查、`workflow.py`/`response/fallback.py` 的状态分支 | 根据已知工具/状态选回答 | 不属于自然语言关键词识别；保留 |

`main.py` 未发现额外的用户自然语言关键词路由；前端发现的 `.includes()` 用于退款/工单状态控制，不是意图判断。配置中的排序模式名、`enterprise_knowledge.py` 的能力标签也不是关键词匹配引擎。

## 离线和演示代码

- `scripts/eval/eval_answer.py`：expected/forbidden keywords、相关性子串检查。
- `scripts/eval/eval_e2e.py`：must_include / must_not_include；`eval_tools.py`：`*_contains`；`eval_rag.py`：keywords_any、目标与证据文本匹配。
- `scripts/eval/negation_aware_answer.py`：否定词和作用域正则，用于独立诊断重评分及其单元测试；`live_reply_mode.py` 仅切换真实回答模式，不执行该否定规则。
- RAG 对照、回放、消融脚本复用词法得分及证据标签；固定回复/路由脚本和 `scripts/dev/durable_graph_demo.py` 中的匹配用于隔离演示。
- 测试文件的关键词断言用于定义预期；维护脚本中的数据库名正则、文件后缀和协议前缀用于参数/文件解析。

这些不能拿来证明线上系统使用关键词路由，也不能为清理线上规则而删除，否则会改变评分口径、复现条件或测试约束。本轮没有读取 validation/final_test 的失败做调参。

## 实测的局部误判与优先方向

通过 [probe_rules.py](probe_rules.py) 在隔离环境调用纯函数，结果见 [probes.json](probes.json)：

| 输入 | 当前函数结果 | 含义 |
|---|---|---|
| 如何防范泄露提示词？ | 输入护栏拒绝 | 讨论风险与发起攻击没有被区分 |
| 保修期一般多久 | 旧 `requires_order_id` 返回 True | 会话继承提示过宽；不是说完整路由必然要求订单 |
| 我不投诉，也不要求直接退款 | 风险 55 分、medium | 在空订单/用户画像的受控输入中，否定词仍触发投诉与绕过审核加分 |
| 没有质量问题，只是不想要 | `is_quality_or_fault_request=True` | 质量事实不能只看是否含“质量问题”；这里只测函数，没有实际提交退款 |
| 快递显示签收但没有收到，我暂不申请退款。 | 无 route 为退款，有 shipping route 为物流 | 证据选择器与实际回答护栏的业务类型判断需要统一 |

优先顺序：先评审是否移除上面的 10 个等价冗余条目；再统一证据护栏的语义上下文；再把退款原因、风险文本信号、订单继承和槽位判断迁到明确的结构化契约。整块规则删除必须以替代能力及固定条件对照为前提。**本次只给出判断和证据，不进行这些规则修改。**
