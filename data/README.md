# 数据目录

## `knowledge/`

售后知识库文档，用于 `policy_search` 的 RAG 检索。

## `orders.json`

订单与客户画像种子数据。SQLite 初始化会导入该文件；MySQL 仅在 `SEED_DEMO_DATA=true` 时导入。

## `intents/`

售后意图样本，覆盖订单查询、退货退款、物流异常、保修维修、支付发票、投诉升级等场景。

## `eval/`

评测样本目录。脚本位于 `scripts/eval/`。

## `cache/`

知识库 manifest 与 embedding cache。

## `traces/`

Agent 运行 trace。线上或共享环境请按需要清理敏感会话内容。

这些数据为模拟业务数据，不包含真实用户信息。
