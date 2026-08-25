# 中文电商智能售后客服 Agent

面向电商售后场景的多 Agent 自动化客服平台。系统不是让 LLM 直接接管业务，而是由 Agent Orchestrator 统一调度客服、售后、风控三个平级 Agent，通过 Tool Calling、Hybrid RAG、MySQL、Redis、MQ 把用户咨询转成可控、可观测、可评测的售后流程。

## 核心架构

```text
                         User
                          |
                          v
                       FastAPI
                          |
                          v
                    Load Context
                          |
                          v
                        Router
                          |
                          v
                   Agent Orchestrator
                          |
                    Shared State
                          |
          ---------------------------------
          |               |               |
          v               v               v
    Customer Agent   AfterSales Agent   Risk Agent
          |               |               |
          v               v               v
     Hybrid RAG     Business Tools    Risk Policy
          |               |               |
          ----------- Shared State --------
                          |
                          v
                   Generate Reply
```

外部入口统一通过 `AgentOrchestrator`。LangGraph 负责承载请求生命周期：加载上下文、路由、进入 Orchestrator 的 Agent Loop、构造回复、保存结果；普通接口和 SSE 流式接口复用同一套执行节点。

## 三个 Agent

| Agent | 职责 | 可调用工具 |
| --- | --- | --- |
| Customer Agent | 普通咨询、售后政策、物流规则、客服 SOP、知识库检索 | `policy_search` |
| AfterSales Agent | 订单查询、退款申请、工单创建、人工审核、转人工 | `order_lookup`, `refund_apply`, `create_ticket`, `create_manual_review`, `transfer_to_human` |
| Risk Agent | 高频退款、异常账号、恶意投诉、虚假描述、大额退款审核 | `risk_check` |

三个 Agent 不直接互相调用。每个 Agent 执行后返回 `AgentResult`，Orchestrator 将结果写入 `AgentState`，再根据最新共享状态决定下一个 Agent。

## 真实退款流程

```text
用户：“订单 10001 耳机坏了我要退款”
        |
        v
Orchestrator -> AfterSales Agent -> order_lookup -> MySQL
        |
        v
Orchestrator -> Customer Agent -> Hybrid RAG
        |
        v
Orchestrator -> Risk Agent -> risk_check -> RiskPolicy
        |
        v
低风险：AfterSales Agent -> refund_apply
        |
        v
Redis RefundLock + Redis Idempotency Cache
        |
        v
DB Active Refund Check + idempotency_key Unique Index
        |
        v
RefundRequest -> MySQL
        |
        v
Publish refund.created -> MQ
        |
        v
Refund Worker -> 更新退款状态、订单状态、用户通知

高风险：AfterSales Agent -> create_manual_review -> 人工审核
```

## 项目结构

```text
app/
├─ agent/
│  ├─ orchestrator.py        Orchestrator 调度 Agent、更新 State、校验工具链
│  ├─ state.py               AgentState / AgentResult 共享状态模型
│  ├─ agents/                Customer / AfterSales / Risk 三个执行单元
│  ├─ entry/                 LangGraph 非流式与流式入口
│  ├─ routing/               意图识别、订单号抽取、槽位补全、会话上下文
│  ├─ policies/              安全规则、工单规则、RAG 证据校验
│  └─ response/              Prompt 与确定性回复构造
├─ tools/
│  ├─ registry.py            Function Calling schema、工具白名单、权限隔离
│  ├─ executor.py            Tool lookup、参数校验、异常捕获、tracing
│  ├─ order.py               订单查询工具
│  ├─ policy.py              企业知识库检索工具
│  ├─ risk.py                风控工具
│  ├─ refund.py              退款申请、Redis 锁、DB 幂等、MQ 投递
│  ├─ ticket.py              售后工单工具
│  └─ human_review.py        人工审核与转人工工具
├─ domain/
│  ├─ risk_policy.py         确定性风险评分规则
│  └─ refund_policy.py       退款资格与退款原因规则
├─ rag/
│  ├─ retriever.py           HybridRetriever 统一入口
│  ├─ hybrid_index.py        Vector + BM25 + Keyword Fusion
│  ├─ vector_store.py        向量相似度
│  ├─ reranker.py            业务重排
│  └─ document_loader.py     文档加载与 Chunk 策略
├─ storage/                  MySQL/SQLite 业务数据门面、Redis 缓存封装
├─ concurrency/              RefundLock 与退款幂等缓存
├─ mq/                       refund.created 消息发布、消费、ack/fail
├─ services/                 Refund Worker
├─ infrastructure/           MySQL / Redis / MQ 生产组件入口
└─ observability/            Trace、耗时、Token、错误信息落库
```

## 数据与基础设施

| 组件 | 用途 |
| --- | --- |
| MySQL | 正式业务数据库，保存订单、退款申请、工单、人工审核、会话、Agent 执行记录、MQ 消息 |
| SQLite | 本地 Demo / 测试后端，接口与 MySQL 对齐 |
| Redis | 会话上下文缓存、Embedding 缓存、Agent 状态缓存、退款锁、退款幂等缓存 |
| MQ | 只服务退款异步链路，主题为 `refund.created` |

MySQL 表结构见 `docs/mysql_schema.sql`。`refund_requests` 使用 `idempotency_key` 唯一索引做数据库兜底，Redis 锁只负责削峰与并发互斥，不能作为唯一一致性保障。

## Hybrid RAG

```text
Query
  |
  +--> Vector Recall
  |
  +--> BM25 / Keyword Recall
  |
  v
Fusion
  |
  v
Business Rerank
  |
  v
Top K Evidence
```

`policy_search` 只调用 `HybridRetriever.retrieve()`。知识库覆盖商品售后规则、退款政策、物流规则、商品说明、客服 SOP、历史问题案例等文档，并通过证据 Guardrail 校验来源、关键词和置信度，避免低置信检索结果触发自动业务动作。

## 本地启动

```powershell
copy .env.example .env
py -3.13 -m pip install -r requirements.txt
docker compose -f docker-compose.dev.yml up -d
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

`.env` 中设置：

```text
DATABASE_BACKEND=mysql
MYSQL_DSN=mysql+pymysql://user:password@127.0.0.1:3306/customer_support
REDIS_URL=redis://127.0.0.1:6379/0
```

访问：

```text
Web:     http://127.0.0.1:8012/
Swagger: http://127.0.0.1:8012/docs
```

## 关键接口

| 接口 | 说明 |
| --- | --- |
| `POST /agent/chat` | 非流式多 Agent 对话 |
| `POST /agent/stream` | SSE 流式多 Agent 对话 |
| `GET /agent/state/{conversation_id}` | 查看 Redis/本地缓存中的 Agent 状态 |
| `GET /knowledge/search` | 调试 Hybrid RAG 检索 |
| `GET /knowledge/catalog` | 查看企业知识库目录 |
| `GET /refunds` | 查看退款申请 |
| `GET /manual-reviews` | 查看人工审核单 |
| `GET /mq/messages` | 查看 MQ 消息 |
| `POST /refund-tasks/process` | 触发 Refund Worker 消费 MQ |
| `GET /observability/metrics` | 查看 Token、耗时、工具链路和错误信息 |

## 验证命令

```powershell
py -3.13 scripts\agent_routing_test.py
py -3.13 scripts\multi_agent_workflow_test.py
py -3.13 scripts\tool_permission_test.py
py -3.13 scripts\high_risk_manual_review_test.py
py -3.13 scripts\run_rag_eval.py
py -3.13 scripts\mq_refund_worker_test.py
py -3.13 scripts\refund_concurrency_stress_test.py
py -3.13 scripts\api_smoke_test.py
```

并发退款压测会模拟 50 个请求同时提交同一订单退款。旧流程会让 50 个请求都通过资格判断，新流程通过 Redis RefundLock、Redis 幂等缓存、数据库活跃退款检查和 `idempotency_key` 唯一索引保证只复用或创建一条有效退款申请。
