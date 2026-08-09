# 中文电商智能售后客服 Agent

中文电商智能售后客服 Agent 是一个基于 FastAPI 的客服自动化服务，面向售后咨询、订单状态查询、退换货政策解释、物流异常处理、地址修改工单、投诉升级等场景。

项目重点实现了 Agent 工程中的关键能力：LangGraph 状态编排、工具调用、RAG 检索、多轮槽位补全、风险控制、流式输出、可观测性和自动化评估。

## 核心能力

- 售后意图路由：识别是否需要查询订单、检索政策、创建工单或转人工。
- LangGraph 编排：使用 `StateGraph` 将加载上下文、路由判断、工具执行、上下文构造、回复生成和结果持久化拆成节点，并通过共享 state 传递中间状态。
- 订单工具调用：根据订单号读取订单状态、物流状态、签收日期、保修期等信息。
- RAG 政策检索：支持 Markdown、TXT、PDF 文档解析，按章节切分 chunk，并基于 embedding + 关键词混合检索召回政策依据。
- 智谱模型接入：支持智谱 Chat Completions 和 `embedding-3`，API Key 通过 `.env` 管理。
- 多轮槽位补全：支持用户分多轮补充订单号、新收货地址等必要信息。
- 高风险控制：退款、赔付、取消订单、修改地址等动作只创建待审核工单，不直接执行真实业务变更。
- 结构化证据上下文：将订单信息、RAG 政策证据、工单结果整理后再交给模型，减少无依据回答。
- Citation 可追溯：回答政策类问题时引用知识库来源，例如 `订单取消与修改政策.md - 修改收货地址`。
- SQLite 持久化：订单、工单、会话消息、pending task 和 feedback 统一保存到数据库。
- 节点级流式展示：浏览器页面支持 SSE，LangGraph 每完成一个节点就推送路由、工具结果或最终回复。
- 自动化评估：覆盖 Agent 路由、RAG 检索、最终回答质量和多轮槽位补全。

## 项目架构

```text
用户问题
-> FastAPI 接口
-> 输入安全检查
-> Pending Task 多轮槽位补全
-> LangGraph StateGraph 工作流
   -> load_context 加载历史和 pending task
   -> route 路由决策和槽位检查
   -> execute_tools 执行订单查询 / RAG 检索 / 工单创建
   -> build_model_context 组装订单、政策、工单证据
   -> generate_reply 调用 LLM 或本地 fallback
   -> persist_result 写入 Tracing / SQLite / Feedback
-> Web UI 流式展示
```

## 目录结构

```text
.
├─ main.py
├─ web/
│  └─ index.html
├─ app/
│  ├─ agent/
│  │  ├─ agent_core.py
│  │  ├─ router.py
│  │  ├─ pending_task.py
│  │  ├─ fallback_policy.py
│  │  ├─ guardrails.py
│  │  └─ memory.py
│  ├─ rag/
│  │  ├─ document_loader.py
│  │  ├─ embedding_client.py
│  │  ├─ vector_index.py
│  │  └─ rag.py
│  ├─ tools/
│  │  └─ support_tools.py
│  ├─ storage/
│  │  ├─ database.py
│  │  ├─ store.py
│  │  └─ feedback_store.py
│  ├─ observability/
│  │  └─ tracing.py
│  ├─ llm/
│  │  └─ llm_client.py
│  └─ core/
│     ├─ config.py
│     └─ schemas.py
├─ data/
│  ├─ orders.json
│  ├─ knowledge/
│  ├─ eval/
│  └─ intents/
├─ scripts/
│  ├─ run_eval.py
│  ├─ run_rag_eval.py
│  ├─ run_answer_eval.py
│  ├─ multi_turn_smoke_test.py
│  ├─ api_smoke_test.py
│  └─ embedding_smoke_test.py
└─ docs/
   └─ optimization_log.md
```

## 模块说明

| 模块 | 作用 |
| --- | --- |
| `main.py` | FastAPI 入口，提供 Web 页面、聊天、流式输出、历史记录、反馈、知识库调试接口 |
| `app/agent/agent_core.py` | Agent 主流程编排，基于 LangGraph `StateGraph` 串联 memory、pending task、router、tools、RAG context、LLM、tracing |
| `app/agent/router.py` | 路由决策，判断是否查订单、查政策、创建工单、提取订单号、推断工单类型 |
| `app/agent/pending_task.py` | 多轮槽位补全，管理 `order_id`、`new_address` 等任务槽位 |
| `app/agent/fallback_policy.py` | 信息不足追问和高风险转人工策略 |
| `app/agent/guardrails.py` | 提示词注入和危险请求拦截 |
| `app/rag/document_loader.py` | 知识库文档解析和 chunk 切分，支持 Markdown、TXT、PDF，预留图片 OCR 入口 |
| `app/rag/embedding_client.py` | Embedding 封装，支持本地 hash embedding 和智谱 `embedding-3` |
| `app/rag/vector_index.py` | 内存向量索引，使用向量相似度 + 业务关键词混合排序 |
| `app/tools/support_tools.py` | 工具层，包含订单查询、政策检索、工单草稿创建 |
| `app/storage/database.py` | SQLite 数据层，保存订单、工单、会话消息、pending task 和 feedback |
| `app/observability/tracing.py` | 请求链路追踪，记录 route、tool_results、model_context、reply |

## LangGraph 共享状态编排

Agent 主链路使用 LangGraph `StateGraph` 进行编排。每个节点只负责一个明确步骤，并通过 `AgentWorkflowState` 共享中间状态。

```text
AgentWorkflowState
├─ user_message / conversation_id
├─ history / pending_task
├─ effective_user_message / slots / missing_slots
├─ route
├─ tool_results
├─ model_messages
├─ reply
└─ trace / result
```

工作流节点：

```text
load_context
-> route
-> execute_tools
-> build_model_context
-> generate_reply
-> persist_result
```

这样做的目的：

- 让 Agent 执行链路从“一个长函数”升级为可观察、可扩展的状态图。
- Router、Tool Executor、Prompt 构造和持久化各自独立，后续更容易插入条件边、人工审核节点或异步任务节点。
- 所有中间状态都集中在共享 state 中，便于 tracing、debug 和前端执行轨迹展示。

`/agent/stream` 复用 LangGraph 的节点函数逐步推进共享 state。`route` 节点完成后立即推送路由判断，`execute_tools` 节点完成后立即推送工具结果。如果开启真实模型和流式输出，生成阶段会调用智谱原生 streaming API，把模型 token 持续转发给前端；如果触发高风险兜底，则直接返回规则回复。

## RAG 设计

知识库位于：

```text
data/knowledge/
```

RAG 流程：

```text
原始售后政策文档
-> 文档解析
-> Markdown 标题级切分
-> chunk metadata 保留 source / section / page / citation
-> embedding 向量化
-> 向量相似度 + 业务关键词混合排序
-> top_k 证据进入模型上下文
```

每个 chunk 保留：

```json
{
  "chunk_id": "退换货政策.md::p0::s1::c1",
  "source": "退换货政策.md",
  "section": "七天无理由退货",
  "text": "政策片段正文",
  "citation": "退换货政策.md - 七天无理由退货"
}
```

为了提升条款级命中率，项目在检索前做 query enrichment，将用户问题、订单状态、物流状态、商品信息、风险边界和业务意图拼入检索 query。

## 多轮槽位补全

地址修改场景需要多个槽位：

```text
order_id
new_address
```

示例流程：

```text
用户：帮我改收货地址。
Agent：请提供订单号和新的收货地址，该操作需要人工审核。

用户：10009
Agent：订单号已收到，请继续提供新的收货地址。

用户：新地址是北京市朝阳区望京街道88号
Agent：查询订单、检索政策、创建地址修改工单草稿。
```

工单只会被创建为 `pending_human_review`，不会直接修改订单、退款或赔付。

## 数据持久化

项目使用 SQLite 作为本地持久化数据库：

```text
data/customer_support.db
```

数据库表：

| 表 | 作用 |
| --- | --- |
| `orders` | 订单数据，启动时从 `data/orders.json` 同步种子数据 |
| `tickets` | 工单草稿，保存 `ticket_id`、订单号、问题类型、优先级、状态、用户诉求 |
| `conversation_messages` | 多轮会话消息，支持服务重启后读取历史 |
| `pending_tasks` | 待补全任务，保存多轮槽位状态 |
| `feedback` | 用户评分和反馈 |

`data/orders.json` 仍然保留为可读的种子数据文件；运行时订单查询统一从 SQLite 读取。

数据库文件属于本地运行数据，已加入 `.gitignore`：

```text
data/customer_support.db
data/customer_support.db-*
```

## 可观测性

每次 Agent 请求都会写入 trace：

```text
data/traces/agent_trace.jsonl
```

trace 中包含：

- `route`：本轮路由判断结果。
- `tool_results`：订单查询、RAG 检索、工单创建等工具结果。
- `model_context`：进入模型前的上下文长度和消息数量。
- `reply`：回复模式和回复长度。
- `timings`：每个节点和工具的耗时。

流式接口会把关键耗时同步推给前端，浏览器工作台可以看到：

```text
node.load_context
node.route
tool.order_lookup
tool.policy_search
tool.create_ticket
node.build_model_context
node.generate_reply
node.persist_result
总耗时
```

分析历史 trace：

```powershell
py -3.13 scripts\analyze_traces.py
```

该脚本会输出请求数量、成功率、平均耗时、路由触发次数、工具调用次数、回复模式，以及各阶段平均耗时和最大耗时。

## 接口说明

| 接口 | 方法 | 作用 |
| --- | --- | --- |
| `/` | GET | Web 工作台 |
| `/health` | GET | 健康检查 |
| `/info` | GET | 服务元数据 |
| `/agent/chat` | POST | 非流式聊天 |
| `/agent/stream` | POST | SSE 流式聊天 |
| `/agent/history` | POST | 查询会话历史 |
| `/feedback` | POST | 保存用户评分 |
| `/orders/{order_id}` | GET | 订单查询调试 |
| `/tickets` | GET | 查看最近创建的工单草稿 |
| `/knowledge/search` | GET | 知识库检索调试 |
| `/knowledge/chunks` | GET | 查看知识库 chunk |

请求示例：

```json
{
  "message": "订单 10009 还没发货，我想改收货地址。",
  "conversation_id": null,
  "use_llm": false
}
```

## 环境变量

`.env` 示例：

```env
LLM_API_KEY=你的智谱APIKey
LLM_ENDPOINT=https://open.bigmodel.cn/api/paas/v4/chat/completions
ZHIPU_MODEL=glm-4-flash

RAG_EMBEDDING_PROVIDER=zhipu
ZHIPU_EMBEDDING_URL=https://open.bigmodel.cn/api/paas/v4/embeddings
ZHIPU_EMBEDDING_MODEL=embedding-3
EMBEDDING_DIMENSIONS=1024
LLM_TIMEOUT_SECONDS=60
```

可选 API Key 变量名：

```text
ZHIPUAI_API_KEY
ZHIPU_API_KEY
BIGMODEL_API_KEY
LLM_API_KEY
```

## 启动方式

安装依赖：

```powershell
py -3.13 -m pip install -r requirements.txt
```

启动服务：

```powershell
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

打开 Web 工作台：

```text
http://127.0.0.1:8012/
```

打开 Swagger：

```text
http://127.0.0.1:8012/docs
```

## 评估结果

当前项目包含三类自动化评估和一个多轮冒烟测试。

| 脚本 | 评估内容 | 当前结果 |
| --- | --- | --- |
| `scripts/run_eval.py` | Router 路由和工具调用 | 17/17 |
| `scripts/run_rag_eval.py` | RAG 来源命中和关键词命中 | 8/8 |
| `scripts/run_answer_eval.py` | Citation 引用和高风险回复控制 | 17/17 |
| `scripts/multi_turn_smoke_test.py` | 多轮槽位补全 | 通过 |
| `scripts/api_smoke_test.py` | API 主链路 | 通过 |
| `scripts/db_smoke_test.py` | SQLite 数据持久化 | 通过 |

运行：

```powershell
py -3.13 scripts\run_eval.py
py -3.13 scripts\run_rag_eval.py
py -3.13 scripts\run_answer_eval.py
py -3.13 scripts\multi_turn_smoke_test.py
py -3.13 scripts\api_smoke_test.py
py -3.13 scripts\db_smoke_test.py
```

评估报告会写入：

```text
data/eval_reports/
```

## 关键优化

完整记录见：

```text
docs/optimization_log.md
```

项目中的核心优化包括：

- 将 Router 规则从主流程拆成独立模块，便于单独评估和后续升级。
- 引入三层评估体系：路由工具评估、RAG 检索评估、最终回答评估。
- 将 RAG 检索结果格式化为 evidence context，要求回答引用 citation。
- 增加 query enrichment，让检索结合订单状态、物流状态和业务意图，提高条款级命中。
- 将 pending task 升级为多槽位补全，避免信息不完整时过早创建工单。
- 接入 SQLite 持久化，保存订单、工单、会话消息、pending task 和 feedback。
- 对退款、赔付、取消订单、修改地址等高风险动作强制人工审核。
- 对缺少订单号或要求绕过审核的高风险请求强制走规则兜底，避免模型在无证据时生成承诺。
- 接入智谱原生流式生成，把最终回复从“等待完整返回”优化为 token 级增量展示。
- 将前端执行轨迹从原始 JSON 升级为业务可读摘要，展示路由依据、订单状态、RAG 证据和工单流转，同时保留 JSON 便于调试。

## 数据说明

项目使用模拟数据，不包含真实用户信息。

- `data/orders.json`：模拟订单状态和售后相关字段。
- `data/knowledge/*.md`：中文电商售后政策知识库。
- `data/eval/*.jsonl`：自动化评估集。
- `data/dataset_sources.md`：数据来源和改造说明。

## 后续规划

- 扩展 PDF/OCR 售后凭证解析能力。
- 引入更完整的 Rerank 或 Cross-Encoder 重排序。
- 将 tracing 报告接入可视化面板。
- 扩展更多多槽位任务，例如退款原因、凭证图片、发票抬头、改派手机号。

## 部署

项目已提供 Docker 和 Render 部署配置：

```text
Dockerfile
.dockerignore
.env.example
render.yaml
docs/deployment.md
```

本地 Docker 测试：

```powershell
docker build -t customer-support-agent .
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

云部署时需要配置：

```text
LLM_API_KEY=你的智谱 API Key
DATABASE_PATH=/var/data/customer_support.db
```

更多说明见：

```text
docs/deployment.md
```
