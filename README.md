# 中文电商智能售后客服 Agent

一个面向中文电商售后场景的智能客服 Agent。项目基于 FastAPI、LangGraph、RAG、SQLite 和智谱大模型构建，支持订单查询、售后政策检索、工单草稿、多轮槽位补全、风险控制、流式输出、执行轨迹展示和自动化评估。

项目不是简单聊天机器人，而是围绕真实售后流程设计的 Agent 服务：用户输入售后问题后，系统会先进行路由判断，再按需查询订单、检索政策、判断工单资格、创建待审核工单，最后基于订单信息和政策证据生成客服回复。

## 在线演示

Render 部署地址：

```text
https://customer-support-agent-dnhl.onrender.com
```

说明：Render 免费实例长时间未访问后可能冷启动，首次请求会慢一些。

## 核心能力

- **Agent 工作流编排**：使用 LangGraph `StateGraph` 拆分加载上下文、路由、工具执行、上下文构造、回复生成和持久化节点。
- **售后意图路由**：识别订单查询、政策检索、工单创建、信息追问、人工审核和安全拦截。
- **工具调用**：封装订单查询、RAG 政策检索、工单草稿创建等工具。
- **RAG 政策检索**：支持 Markdown、TXT、PDF 文档解析，按章节切分 chunk，保留 `source`、`section`、`page`、`citation` 元数据。
- **混合检索优化**：结合智谱 `embedding-3` 向量检索和业务关键词召回，提高条款级命中率。
- **多轮槽位补全**：支持用户分多轮补充订单号、新收货地址等信息，信息不完整时不会提前创建工单。
- **业务风险控制**：退款、赔付、取消订单、修改地址等高风险动作只生成待人工审核工单，不直接执行业务变更。
- **工单资格判断**：创建工单前结合订单状态、签收状态、物流更新时间和保修期做二次校验。
- **可观测性**：记录 route、tool results、model context、reply、timings，并在 Web 页面展示执行轨迹。
- **自动化评估**：覆盖 Router 工具调用、RAG 召回、最终回答质量、API 主链路和多轮槽位补全。

## 技术栈

| 类型 | 技术 |
| --- | --- |
| 后端服务 | FastAPI, Uvicorn |
| Agent 编排 | LangGraph |
| 大模型 | 智谱 GLM |
| Embedding | 智谱 `embedding-3` |
| RAG | 文档切分、metadata、citation、混合检索 |
| 数据库 | SQLite |
| 前端 | 原生 HTML/CSS/JavaScript, SSE |
| 部署 | Docker, Render |
| 评估 | 自定义 eval 脚本、trace 分析 |

## 项目架构

```text
用户问题
-> FastAPI API
-> 输入安全检查
-> 多轮 pending task 合并
-> LangGraph Agent Workflow
   -> load_context
   -> route
   -> execute_tools
      -> order_lookup
      -> policy_search
      -> ticket_decision
      -> create_ticket
   -> build_model_context
   -> generate_reply
   -> persist_result
-> SSE 流式返回给 Web 工作台
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
│  │  ├─ ticket_policy.py
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
│  └─ eval/
├─ scripts/
│  ├─ run_eval.py
│  ├─ run_rag_eval.py
│  ├─ run_answer_eval.py
│  ├─ multi_turn_smoke_test.py
│  ├─ api_smoke_test.py
│  └─ analyze_traces.py
├─ docs/
│  ├─ optimization_log.md
│  └─ deployment.md
├─ Dockerfile
├─ render.yaml
└─ requirements.txt
```

## 关键流程

### 1. 路由与工具执行

Router 会从用户输入中抽取订单号，并判断本轮是否需要：

- 查询订单
- 检索政策
- 创建工单
- 追问用户
- 转人工审核
- 安全拦截

执行层不会盲目相信 Router 的初步计划。比如订单查询失败后，系统会立即停止政策检索和工单创建，避免对不存在订单生成虚假售后结果。

### 2. RAG 政策检索

知识库位于：

```text
data/knowledge/
```

RAG 流程：

```text
原始政策文档
-> 文档解析
-> 章节级 chunk 切分
-> 保留 citation
-> embedding 向量化
-> 向量相似度 + 关键词混合排序
-> top_k 证据进入模型上下文
```

每个 chunk 示例：

```json
{
  "source": "退换货政策.md",
  "section": "七天无理由退货",
  "text": "政策片段正文",
  "citation": "退换货政策.md - 七天无理由退货"
}
```

### 3. 多轮槽位补全

修改地址等任务需要多个槽位：

```text
order_id
new_address
```

如果用户只说“帮我改收货地址”，系统会先追问订单号和新地址。只有槽位补齐后，才会继续订单查询、政策检索和工单创建。

### 4. 高风险与工单资格控制

系统不会直接执行真实退款、赔付、取消订单、修改地址等动作。

创建工单前还会进行业务资格判断：

- 订单不存在：不继续检索政策，不创建工单。
- 订单未签收：不创建保修检测工单。
- 物流更新未超过 48 小时：不创建物流异常工单。
- 地址修改、退款、投诉等高风险动作：只创建 `pending_human_review` 工单。

## 数据持久化

项目使用 SQLite 保存运行数据：

```text
data/customer_support.db
```

| 表 | 作用 |
| --- | --- |
| `orders` | 订单数据 |
| `tickets` | 工单草稿 |
| `conversation_messages` | 多轮会话历史 |
| `pending_tasks` | 待补全槽位任务 |
| `feedback` | 用户评分和反馈 |

本地数据库文件已加入 `.gitignore`，不会提交到 GitHub。

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
| `/tickets` | GET | 查看工单草稿 |
| `/knowledge/search` | GET | 知识库检索调试 |
| `/knowledge/chunks` | GET | 查看知识库 chunk |

## 环境变量

复制 `.env.example` 为 `.env`，并填写智谱 API Key：

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

## 本地启动

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

## 文档更新

如果 `data/knowledge/` 下的政策文档更新，需要重新构建 RAG 索引：

```powershell
py -3.13 scripts\ingest_knowledge.py
```

然后重启服务或重新部署。

## 自动化评估

当前回归结果：

| 脚本 | 评估内容 | 当前结果 |
| --- | --- | --- |
| `scripts/run_eval.py` | Router 路由和工具调用 | 21/21 |
| `scripts/run_rag_eval.py` | RAG 来源命中和关键词命中 | 8/8 |
| `scripts/run_answer_eval.py` | Citation 引用和高风险回复控制 | 21/21 |
| `scripts/multi_turn_smoke_test.py` | 多轮槽位补全 | 通过 |
| `scripts/api_smoke_test.py` | API 主链路 | 通过 |
| `scripts/db_smoke_test.py` | SQLite 持久化 | 通过 |

运行：

```powershell
py -3.13 scripts\run_eval.py
py -3.13 scripts\run_rag_eval.py
py -3.13 scripts\run_answer_eval.py
py -3.13 scripts\multi_turn_smoke_test.py
py -3.13 scripts\api_smoke_test.py
py -3.13 scripts\db_smoke_test.py
```

## 部署

本地 Docker：

```powershell
docker build -t customer-support-agent .
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

Render 部署需要配置环境变量：

```text
LLM_API_KEY=你的智谱 API Key
DATABASE_PATH=/var/data/customer_support.db
```

部署细节见 [docs/deployment.md](docs/deployment.md)。

## 项目亮点

详细优化过程见 [docs/optimization_log.md](docs/optimization_log.md)。核心亮点包括：

- 使用 LangGraph 将 Agent 主流程拆成可观察、可扩展的状态图。
- 将 Router、工具执行、RAG 证据上下文和持久化解耦。
- 建立 Router、RAG、Answer 三层自动化评估闭环。
- 对高风险售后动作做确定性兜底和人工审核控制。
- 通过订单优先和工单资格判断避免模型对不存在订单或未满足条件的订单生成错误结果。
- 通过 trace 和前端执行轨迹展示 Agent 每一步判断、工具结果和耗时。
