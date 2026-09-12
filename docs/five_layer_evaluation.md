# 五层 Dev 测评

运行当前五层评测并保留源码快照、数据指纹、逐案结果及 Trace：

```powershell
python -m scripts.eval.run_five_layer_eval --output reports/five_layer_<新的运行名>
python -m scripts.eval.summarize_five_layer_eval --directory reports/five_layer_<新的运行名>
```

输出目录必须尚不存在，避免覆盖旧基线。若需要自动质量门禁，第二条命令加 `--fail-on-quality`；有任一原评分失败时返回 1。评测运行完成与案例全部通过是不同状态，分别查看 `summary.json` 的 `evaluation_completed` 和 `all_evaluations_passed`。

|层|现有数据|评分与产物|
|---|---|---|
|Routing|`routing_eval.jsonl`，24 条|意图、动作、主题和派生路由字段；保存每案语义输出与原始模型响应|
|RAG|`rag_eval.jsonl`，20 条；`rag_eval_dev_v2_batch01.jsonl`，6 条|Hit@1/3/5、MRR、候选证据覆盖和最终覆盖；两套数据单独报告，保留 Top20、完整排序和 Top5|
|Tool|`tool_eval.jsonl`，55 条|选择、参数、执行成功及禁止工具调用；执行写操作只发生在临时数据库|
|Answer|`answer_eval.jsonl`，20 条（原 4 + B01 新增 16）|启用真实生成；现有关键词、相关性、引用、禁用表述检查保持不变|
|E2E|`e2e_eval.jsonl`，24 条（原 12 + B01 新增 12）|启用真实生成；检查路由、Agent、工具、风险、退款、数据库/MQ、副作用及回答，保存各阶段失败信息|

Runner 在一个冻结源码副本上启动各层，每套业务评测又使用独立 SQLite、演示订单、进程内缓存和知识库副本。不会复制 `.env`、现有业务库、旧 Trace、Holdout、validation 或 final_test。模型凭据通过子进程环境传入，不保存到报告。环境需有项目模型配置；模型调用会产生实际用量。

当前固定配置：`fixed_256` / overlap 32、本地 `local_hash_v1` / 256、召回权重 0.62/0.28/0.10、`hybrid_rule`，互补选择器和语义扩展关闭。独立 RAG 层固定 candidate_k=20、top_k=5；其他层沿用真实工具调用参数，不能把独立检索得分直接当作回答引用覆盖率。记忆配置固定 8000 字符 / 1800 秒。

真实语义路由不使用回放。Answer/E2E 通过已有执行开关设置 `use_llm=True`，业务代码要求的模板、澄清和无证据回复仍保留，并记录实际 reply_mode。没有修改 evaluator 的通过条件或期望值，也没有引入新的 LLM Judge。

`summary.json` 保存逐案失败及对应 Trace ID、JSONL 行号或步骤记录。Routing 直接测路由，不经过整条工作流，所以使用 `case_results.jsonl` 和 `semantic_responses.jsonl` 定位；Tool 中部分权限案例也不产生完整 Agent Trace。报告会分别给出可关联 Trace 数，不把缺少的记录编造成 Trace。

依赖故障、模型输出格式失败与现有断言失败分别标记；它们不改变原始分母和得分。阶段名称表示第一个失败检查，不自动证明唯一根因。`llm_calls` 和 token 汇总只覆盖实际关联的 Trace；直接路由调用可能没有 provider usage，不应把缺失用量记成 0 或声称是完整账单。

Answer 的现有判断是字符串级检查。例如否定句仍可能触发禁用词；报告保留原判定，并在分析中说明证据。早期报告的 4 条 Answer 案例及当前扩展开发集都不能代表全面的回答质量验收。本轮也不验证 HTTP 身份、真实支付、生产 MySQL/Redis、并发性能或多轮专项能力，这些应使用各自的集成测试与接入验收。

首次完整运行见 [2026-09-09 测评报告](../reports/five_layer_eval_20260909/report.md)。当前 GitHub Actions 的 `checks.yml` 仍是隔离发布检查；本轮没有上传模型凭据或触发远端评测。

## 可回退的优化配置

使用 `--profile optimized` 运行实验版本，省略该参数仍运行 baseline：

```powershell
python -m scripts.eval.run_five_layer_eval --profile optimized --output reports/five_layer_<新的优化运行名>
python -m scripts.eval.summarize_five_layer_eval --directory reports/five_layer_<新的优化运行名> --fail-on-quality
```

optimized 显式启用已有互补选择器，并通过实际政策工具传入完整查询上下文、Top20 候选和 Top5 证据；独立 RAG 层也使用同一选择器。它同时启用 `SEMANTIC_RELATED_TOPICS=discard_invalid`：只有 `action_type=query` 才允许丢弃不在合法 topic 集合中的辅助字符串，合法主字段仍须通过原校验。执行、转人工、非法主字段及非法数组结构保持严格拒绝。不会将未知辅助主题猜测映射成退款等执行动作，不增加模型重试。该配置不写入生产 `.env`，也不替换默认模式。

Answer 的否定句诊断可单独运行；原 evaluator、expected 和原报告不变：

```powershell
python -m scripts.eval.negation_aware_answer --input <原eval_answer.json> --output <新的诊断报告.json>
```

该报告标记 `scoring_version=explicit-denial-v1`，逐次记录命中位置与明确否定判断。同一回复重新评分的变化属于评分修正，不计作系统质量提升，也不替代五层原始质量门禁。

注意：业务套件内目前共享一份演示数据库，订单日期为固定日历日期，生产资格判断使用运行时钟。因此旧案例存在跨案状态影响和日期漂移，不能把这些评测当作已固定业务时钟、按案例隔离的业务验收。原标签中另有内部工具顺序和客户回复必须包含 `MQ` 的断言；优化报告会明确列出这些冲突，不通过恢复无效退款调用或给客户补内部术语提高分数。

本轮修复、逐案对照与剩余问题见 [五层优化报告](../reports/five_layer_optimization_20260909/report.md)。

## Answer / E2E Dev 扩充 B01

在原文件末尾追加 Answer 16 条、E2E 12 条，旧 4/12 条及 expected 保留。新增 ID 分别为 `answer_dev_b01_001`～`016`、`e2e_dev_b01_001`～`012`；独立候选保存在 `data/eval/candidates/*_dev_candidates_b01.jsonl`。这些是开发案例，不是 Holdout。

生成前的[场景矩阵与覆盖分析](../data/eval/design/answer_e2e_expansion_b01.md)记录 18 个跨层场景；[扩充报告](../reports/answer_e2e_dev_expansion_b01/report.md)记录来源校验、运行结果和局限。模型、业务代码与 evaluator 均未因本次扩充改动。历史 4/12 条报告不覆盖；当前整体分数不能直接与历史小样本分数比较，需分别报告旧案例与新增案例。

仅运行扩展后的两层（独立测试数据库、真实模型、保留回复强制降级）：

```powershell
python -m scripts.eval.run_isolated_baseline --suites answer e2e --live-replies --record-semantic-responses --record-case-results --semantic-related-topics discard_invalid --policy-evidence complementary --output reports/answer_e2e_<新的运行名>
python reports/answer_e2e_dev_expansion_b01/validate_data.py
```

此命令使用既有优化实验配置，不改变生产默认；保留本批全部有效失败，不按分数筛除案例。现有 runner 每案仍为一条消息，新增案例不等同于多轮、支付回调或人工审批续办验收。`live/manifest.json` 中套件退出码及逐案报告反映原评分结果，runner 自身退出 0 只代表编排完成。
