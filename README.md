# 中文电商智能售后客服 Agent

## 项目简介

这是一个面向中文电商售后场景的多 Agent 智能客服服务。项目目标不是做通用聊天机器人，而是把用户售后诉求转成一条可控、可观测、可评测的业务自动化链路：

```text
用户问题
-> FastAPI
-> Agent Orchestrator
-> 客服 Agent / 售后 Agent / 风控 Agent
-> RAG / 业务 Tool / 风险规则
-> Redis 状态缓存
-> MQ 退款任务
-> 业务处理服务
-> 结果反馈与数据分析
```

系统支持普通客服问答、售后政策 RAG、多轮槽位补全、订单查询、退款申请、MQ 异步处理、人工审核、商品推荐、快捷回复、转人工、工单草稿、流式输出、执行轨迹、Token 估算和自动化评估。

## 技术栈

| 方向 | 技术 |
| --- | --- |
| 后端服务 | FastAPI, Uvicorn |
| Agent 编排 | LangGraph |
| 大模型 | 智谱 GLM |
| Embedding | 智谱 `embedding-3` / 本地 hash embedding |
| 知识库 | Markdown / TXT / PDF, RAG Chunk 策略 |
| 业务库 | 统一业务数据库门面：MySQL 生产适配 / SQLite 本地后端 |
| 缓存 | Redis / 本地 TTL 缓存 |
| MQ | SQLite 模拟队列，可替换 RabbitMQ/Kafka |
| 前端 | 原生 HTML/CSS/JavaScript, SSE |
| 部署 | Docker, Render |

## 核心链路

```text
FastAPI API
-> app.agent.entry.workflow
   -> Agent Orchestrator    统一路由和多 Agent 分派
   -> load_context          加载会话历史和 pending task
   -> route                 识别意图、订单号、风险等级、Agent 计划和工具计划
   -> execute_tools         客服/售后/风控 Agent 协作执行工具链
   -> build_model_context   整理订单信息、政策证据和工具结果
   -> generate_reply        调用 LLM 或使用确定性 fallback 回复
   -> persist_result        保存会话、工单和执行 trace
-> 返回 API / SSE 响应
```

工具调用采用白名单机制：工具必须先在 `tool_registry.py` 注册，执行前会校验参数和工具链，避免在订单不存在、政策证据不足或高风险场景下继续执行错误动作。

## 多 Agent 架构

```text
用户
 |
FastAPI
 |
Agent Orchestrator
 |
--------------------------------
|              |               |
客服 Agent      售后 Agent      风控 Agent
|              |               |
知识库 RAG     业务 Tool       风险规则
|              |
Vector DB      MySQL/SQLite
 |
Redis
 |
MQ 消息队列
 |
业务处理服务
```

- 客服 Agent：普通咨询、意图识别、知识库 RAG、快捷回复、商品推荐。
- 售后 Agent：订单查询、退款申请、工单创建、人工审核流转、MQ 退款任务。
- 风控 Agent：高频退款、异常账号、恶意投诉、虚假描述、大额退款审核。

## 项目结构

```text
.
├─ main.py                    FastAPI 入口
├─ web/                       浏览器客服工作台
├─ app/
│  ├─ agent/                  Agent 入口、编排、路由、工具、安全和回复
│  │  ├─ entry/               API 入口、非流式/流式工作流
│  │  ├─ orchestration/       Orchestrator 与客服/售后/风控 Agent
│  │  ├─ routing/             意图识别、槽位补全、上下文和记忆
│  │  ├─ tools/               工具注册、执行、校验和结果处理
│  │  ├─ policies/            工单策略、安全规则和 RAG 证据校验
│  │  └─ response/            Prompt 与回复上下文构造
│  ├─ mq/                     MQ 消息队列适配层
│  ├─ services/               退款等业务处理服务
│  ├─ rag/                    知识库加载、切片、Embedding、检索、重排
│  ├─ tools/                  订单查询、政策检索、工单、商品、转人工工具
│  ├─ storage/                SQLite 表结构和数据读写
│  ├─ llm/                    智谱大模型调用
│  ├─ workbench/              商品、快捷回复、多平台会话样例
│  ├─ observability/          trace 和耗时记录
│  └─ core/                   配置和 API schema
├─ data/
│  ├─ knowledge/              售后知识库
│  ├─ orders.json             订单种子数据
│  ├─ workbench/              商品和快捷回复数据
│  ├─ eval/                   自动化评估样本
│  ├─ eval_reports/           评估报告
│  ├─ cache/                  Embedding cache 和知识库 manifest
│  └─ traces/                 Agent 执行日志
├─ scripts/                   知识库 ingest、评估脚本、冒烟测试
├─ docs/                      部署说明和优化记录
├─ Dockerfile
├─ render.yaml
├─ requirements.txt
└─ .env.example
```

## 核心模块说明

| 模块 | 说明 |
| --- | --- |
| `app/agent/entry/workflow.py` | LangGraph 主工作流，串联上下文、Orchestrator、工具、回复和持久化 |
| `app/agent/entry/stream_runner.py` | SSE 流式工作流 |
| `app/agent/orchestration/orchestrator.py` | 多 Agent 编排入口，生成 Agent 计划并分发工具链 |
| `app/agent/orchestration/customer_agent.py` | 客服问答 Agent |
| `app/agent/orchestration/after_sales_agent.py` | 售后处理 Agent，包含退款资格判断 |
| `app/agent/orchestration/risk_agent.py` | 风控 Agent，输出风险等级、风险原因和人工审核判断 |
| `app/agent/routing/router.py` | 抽取订单号，识别售后意图，生成工具计划 |
| `app/agent/routing/memory.py` | 会话历史和 pending task 管理 |
| `app/agent/tools/tool_registry.py` | 维护工具 schema 和工具白名单 |
| `app/agent/tools/tool_executor.py` | 执行工具，捕获异常，校验工具链并做失败截断 |
| `app/agent/policies/evidence_guardrail.py` | 校验 RAG 证据来源和关键词是否足够支撑业务动作 |
| `app/agent/response/prompt_builder.py` | 将订单、政策证据、退款、审核和工单结果整理为 LLM 上下文 |
| `app/rag/document_loader.py` | 加载 Markdown/TXT/PDF 并切分 chunk |
| `app/rag/hybrid_index.py` | Hybrid RAG：向量召回 + BM25/关键词召回 + 候选融合 |
| `app/rag/vector_index.py` | 向量相似度工具和旧版内存索引 |
| `app/rag/reranker.py` | 基于业务规则对检索结果重排 |
| `app/tools/support_tools.py` | 具体业务工具实现 |
| `app/storage/database.py` | 业务数据库统一门面，按配置分发到 MySQL 或 SQLite |
| `app/storage/mysql_database.py` | MySQL 生产适配：建表、种子数据、业务 CRUD 和 MQ 状态流转 |
| `app/storage/cache.py` | Redis/本地 TTL 缓存，用于会话、热点知识和 Agent 状态 |
| `app/concurrency/refund_guard.py` | Redis 分布式锁和退款幂等控制，解决同一订单高并发重复退款 |
| `app/mq/queue.py` | MQ 发布、消费、ack/fail |
| `app/services/refund_service.py` | 退款处理服务，消费 MQ 并更新订单状态 |

## 数据表

| 表 | 作用 |
| --- | --- |
| `orders` | 订单数据 |
| `customer_profiles` | 客户画像和风控特征 |
| `tickets` | 工单草稿 |
| `refund_requests` | 退款申请 |
| `manual_reviews` | 人工审核单 |
| `mq_messages` | MQ 消息 |
| `notifications` | 用户通知 |
| `agent_metrics` | Token、耗时、错误等可观测指标 |
| `conversation_messages` | 多轮会话历史 |
| `pending_tasks` | 待补全槽位任务 |
| `feedback` | 用户评分和反馈 |

MySQL 表结构见 `docs/mysql_schema.sql`。本地默认使用 SQLite；生产替换时设置 `DATABASE_BACKEND=mysql` 并填写 `MYSQL_DSN=mysql+pymysql://user:password@host:3306/customer_support`。

## 本地启动

```powershell
copy .env.example .env
py -3.13 -m pip install -r requirements.txt
docker compose -f docker-compose.dev.yml up -d
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

`docker-compose.dev.yml` 会启动本地 MySQL 和 Redis。`.env` 中设置 `DATABASE_BACKEND=mysql`、`MYSQL_DSN` 和 `REDIS_URL` 后，项目启动时会自动创建 MySQL 业务表并导入订单/客户画像种子数据。

访问：

```text
Web:     http://127.0.0.1:8012/
Swagger: http://127.0.0.1:8012/docs
```

默认可以不配置真实大模型，接口参数 `use_llm=false` 时会走本地确定性回复。如需调用智谱模型，在 `.env` 中配置 `LLM_API_KEY`。

## 知识库更新

知识库目录是 `data/knowledge/`。更新 Markdown、TXT 或 PDF 后执行：

```powershell
py -3.13 scripts\ingest_knowledge.py
```

脚本会扫描文档、计算 hash、识别新增/修改/删除文件、重新切分 chunk，并预热 embedding cache。

## 自动化评估

```powershell
py -3.13 scripts\run_eval.py
py -3.13 scripts\run_rag_eval.py
py -3.13 scripts\run_answer_eval.py
py -3.13 scripts\run_e2e_eval.py
py -3.13 scripts\run_multi_agent_eval.py
py -3.13 scripts\run_metrics.py
```

评估样本在 `data/eval/`，报告输出到 `data/eval_reports/`。

## 关键接口

| 接口 | 说明 |
| --- | --- |
| `POST /agent/chat` | 非流式多 Agent 对话 |
| `POST /agent/stream` | SSE 流式多 Agent 对话 |
| `GET /agent/state/{conversation_id}` | 查看 Redis/本地缓存中的 Agent 状态 |
| `GET /cache/health` | 查看当前缓存后端、Redis 可达性和降级原因 |
| `GET /knowledge/catalog` | 查看企业知识库文档分类、chunk 策略和 Hybrid RAG 架构 |
| `GET /knowledge/search` | 调试 Hybrid RAG 检索结果和召回分数 |
| `GET /refunds` | 查看退款申请 |
| `POST /refund-tasks/process` | 触发退款处理服务消费 MQ |
| `GET /manual-reviews` | 查看人工审核单 |
| `GET /mq/messages` | 查看 MQ 消息 |
| `GET /observability/metrics` | 查看 Token、耗时、错误等指标 |

## 高并发退款压测

```powershell
py -3.13 scripts\refund_concurrency_stress_test.py
```

该脚本会模拟 50 个并发退款请求打到同一个订单。旧流程中所有请求都会通过退款资格判断，可能重复创建退款申请；新流程使用 Redis `SET NX EX` 分布式锁和退款幂等结果缓存，只允许一个请求创建退款申请和 MQ 消息，其余请求复用同一退款结果。

## Docker

```powershell
docker build -t customer-support-agent .
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

Render 部署配置见 `render.yaml`，详细说明见 `docs/deployment.md`。
