# 数据集来源与改造说明

本项目的数据集不是直接使用真实用户数据，而是基于公开项目的数据结构和客服业务覆盖面整理出的中文模拟数据。

## 1. agent-service-toolkit

- GitHub: https://github.com/JoshuaC215/agent-service-toolkit
- License: MIT License
- 本地参考路径：`D:\python-project\agent-service-toolkit-main`

本项目参考并复用了其中的电商客服业务方向：

- `data/customer_orders.json`
- `data/customer_support/*.md`
- `src/agents/customer_support_agent.py`
- `src/streamlit_app.py`
- `src/service/service.py`

改造方式：

- 将订单数据扩展为更多中文电商售后场景。
- 将售后政策扩展为退换货、物流、保修、会员、支付发票、订单取消修改、库存补货等知识库。
- 将服务接口思想迁移为 `/info`、`/agent/stream`、`/agent/history`、`/feedback`。

## 2. Bitext customer support intent dataset

- GitHub: https://github.com/bitext/customer-support-llm-chatbot-training-dataset

本项目没有直接复制大规模英文训练样本，而是参考其客服意图覆盖面，整理了中文电商售后意图文件：

- `data/intents/customer_support_intents.json`
- `data/eval/customer_support_eval.jsonl`

改造方式：

- 将通用客服意图改写成中文电商售后意图。
- 为每个意图补充预期工具调用，用于后续 eval。

## 3. 数据文件用途

- `data/orders.json`：模拟订单数据库，供 `order_lookup` 工具使用。
- `data/knowledge/*.md`：RAG 知识库，供 `policy_search` 工具检索。
- `data/intents/customer_support_intents.json`：意图分类说明，后续可用于 Router 评估或训练样本扩展。
- `data/eval/customer_support_eval.jsonl`：评估集，后续可用于测试路由准确率、工具调用准确率和回答质量。

## 4. 数据使用边界

这些数据仅用于教学、项目展示和本地模拟，不包含真实用户隐私数据。Agent 不允许自动执行真实退款、赔付、取消订单、修改地址或修改数据库操作。
