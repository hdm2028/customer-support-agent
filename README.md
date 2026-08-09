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
- **两阶段检索与 Rerank**：先用 embedding + 关键词混合检索召回候选 chunk，再基于业务意图、章节标题、政策短语和订单状态做二次排序。
- **RAG 证据兜底**：对检索结果做分数阈值、来源文档和关键政策词校验，召回不全时停止自动动作。
- **知识库增量 Ingest**：通过文档 hash 生成 manifest，识别新增、修改、未变化和删除文档，并结合 embedding cache 复用已有向量。
- **多轮槽位补全**：支持用户分多轮补充订单号、新收货地址等信息，信息不完整时不会提前创建工单。
- **多轮上下文继承**：用户后续只说“退款”“投诉”等短追问时，系统会从会话历史继承最近订单号继续处理。
- **业务风险控制**：退款、赔付、取消订单、修改地址等高风险动作只生成待人工审核工单，不直接执行业务变更。
- **工单资格判断**：创建工单前结合订单状态、签收状态、物流更新时间和保修期做二次校验。
- **可观测性**：记录 route、tool results、model context、reply、timings，并在 Web 页面展示执行轨迹。
- **自动化评估**：覆盖 Router 工具调用、RAG 召回、最终回答质量、API 主链路和多轮槽位补全。
- **端到端业务评估**：从用户输入开始检查路由、槽位、工具序列、工具参数、最终业务动作和回复约束。

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

## 文档更新与 Ingest

如果 `data/knowledge/` 下的政策文档更新，运行：

```powershell
py -3.13 scripts\ingest_knowledge.py
```

脚本会完成：

```text
扫描知识库文档
-> 计算每个文件的 sha256 hash
-> 对比上一次 manifest
-> 识别新增 / 修改 / 未变化 / 删除文档
-> 重新切分 chunk
-> 构建内存向量索引并预热 embedding cache
-> 输出 Knowledge Ingest Report
-> 写入 data/cache/knowledge_manifest.json
```

示例输出：

```text
Knowledge Ingest Report
文档总数: 8
chunk 总数: 31
新增文档: 0
修改文档: 0
未变化文档: 8
删除文档: 0
预计复用 embedding: 31
预计新增 embedding: 0
```

`data/cache/` 属于运行时缓存，已加入 `.gitignore`，不会提交到 GitHub。文档更新后需要重启服务或重新部署，让线上服务重新加载最新知识库。

## 自动化评估

当前回归结果：

| 脚本 | 评估内容 | 当前结果 |
| --- | --- | --- |
| `scripts/run_eval.py` | Router 路由和工具调用 | 21/21 |
| `scripts/run_rag_eval.py` | RAG 来源命中和关键词命中 | 8/8 |
| `scripts/run_answer_eval.py` | Citation 引用和高风险回复控制 | 21/21 |
| `scripts/run_e2e_eval.py` | 端到端业务链路、工具序列和最终动作 | 12/12 |
| `scripts/multi_turn_smoke_test.py` | 多轮槽位补全 | 通过 |
| `scripts/context_smoke_test.py` | 多轮上下文继承 | 通过 |
| `scripts/tool_failure_smoke_test.py` | 工具异常、链路短路和降级回复 | 通过 |
| `scripts/retrieval_guardrail_smoke_test.py` | RAG 低置信、来源不匹配和召回不全兜底 | 通过 |
| `scripts/api_smoke_test.py` | API 主链路 | 通过 |
| `scripts/db_smoke_test.py` | SQLite 持久化 | 通过 |

运行：

```powershell
py -3.13 scripts\run_eval.py
py -3.13 scripts\run_rag_eval.py
py -3.13 scripts\run_answer_eval.py
py -3.13 scripts\run_e2e_eval.py
py -3.13 scripts\multi_turn_smoke_test.py
py -3.13 scripts\context_smoke_test.py
py -3.13 scripts\tool_failure_smoke_test.py
py -3.13 scripts\retrieval_guardrail_smoke_test.py
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

