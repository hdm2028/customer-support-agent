# 中文电商智能售后客服 Agent

面向电商售后的多 Agent 客服服务，覆盖咨询、订单查询、政策检索、风险判断、退款申请、人工审核、工单和退款异步处理。

## Project Overview

服务入口为 FastAPI。用户请求进入 Router 后，由 Orchestrator 调度 Customer、AfterSales、Risk 三个 Agent。业务动作全部通过受控工具执行，数据写入 MySQL 或 SQLite，Redis 负责状态缓存、锁和幂等缓存，退款事件通过数据库 MQ 表异步处理。

## Architecture

```text
User
  -> FastAPI
  -> LangGraph Workflow
  -> Router
  -> AgentOrchestrator
  -> CustomerAgent / AfterSalesAgent / RiskAgent
  -> Tool Executor
  -> Storage / RAG / MQ / Redis
  -> Response
```

核心入口：

- `main.py`: API 入口
- `app/agent/entry/workflow.py`: 非流式执行流程
- `app/agent/entry/stream_runner.py`: SSE 流式执行流程
- `app/agent/orchestrator.py`: Agent 调度
- `app/agent/routing/router.py`: 路由与工具计划
- `app/tools/executor.py`: 工具权限、参数校验、异常边界、trace

## Refund Workflow

```text
退款请求
  -> order_lookup
  -> policy_search
  -> risk_check
  -> refund_apply
  -> Redis lock / idempotency cache
  -> refund_requests
  -> refund.created MQ
  -> Refund Worker
  -> refund_requests / orders / notifications
```

高风险、人工审核、工具失败和低置信 RAG 证据由现有业务规则处理。

## Project Structure

```text
app/
  agent/              Router、Workflow、Orchestrator、Agent、回复构造
  concurrency/        退款锁与幂等缓存
  core/               配置与 API schema
  domain/             退款资格与风险规则
  mq/                 数据库 MQ 发布、领取、ack/fail
  observability/      trace 与指标落库
  rag/                文档加载、向量召回、BM25、关键词融合、重排
  services/           退款处理服务
  storage/            MySQL / SQLite / Redis 数据访问
  tools/              订单、政策、风控、退款、工单、人工审核工具
data/
  eval/               评测样本
  knowledge/          售后知识库
  orders.json         订单种子数据
docs/
  mysql_schema.sql    MySQL 表结构
scripts/
  eval/               评测脚本
  observability/      trace 与失败案例检查
  reliability/        可靠性脚本
  setup/              环境检查与知识库 ingest
```

## Configuration

复制模板后填写本机配置：

```powershell
copy .env.example .env
```

常用配置：

```env
DATABASE_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=customer_support
MYSQL_PASSWORD=change-me
MYSQL_DATABASE=customer_support
SEED_DEMO_DATA=false

REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0

MQ_BACKEND=database
```

`MYSQL_DSN` 优先级高于拆分的 MySQL 配置。`REDIS_URL` 优先级高于拆分的 Redis 配置。

## Running

```powershell
py -3.13 -m pip install -r requirements.txt
py -3.13 -m scripts.setup.check_local_services
py -3.13 -m scripts.setup.ingest_knowledge
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

访问地址：

```text
Web:     http://127.0.0.1:8012/
Swagger: http://127.0.0.1:8012/docs
```

## MySQL / Redis / MQ

- MySQL 保存订单、退款申请、工单、人工审核、会话、指标和 MQ 消息。
- Redis 保存会话状态、缓存、退款锁和退款幂等结果。
- MQ 当前使用业务数据库表承载，主题为 `refund.created`。
- MySQL 表结构见 `docs/mysql_schema.sql`。

## Evaluation

```powershell
py -3.13 -m scripts.eval.eval_routing
py -3.13 -m scripts.eval.eval_rag
py -3.13 -m scripts.eval.eval_tools
py -3.13 -m scripts.eval.eval_answer
py -3.13 -m scripts.eval.eval_e2e
py -3.13 -m scripts.eval.generate_report
```

`eval_e2e.py` 会写入数据库、缓存、trace，并可能发布退款 MQ 消息。

## Reliability

```powershell
py -3.13 -m scripts.reliability.test_refund_idempotency
py -3.13 -m scripts.reliability.test_refund_concurrency
py -3.13 -m scripts.reliability.test_mq_duplicate_delivery
py -3.13 -m scripts.reliability.test_high_risk_review
py -3.13 -m scripts.reliability.test_tool_failure
py -3.13 -m scripts.reliability.test_service_degradation
```

退款相关可靠性脚本会写入业务库并发布或消费 MQ 消息。

## Observability

```powershell
py -3.13 -m scripts.observability.analyze_traces
py -3.13 -m scripts.observability.inspect_failed_cases
```

API：

- `GET /observability/metrics`
- `GET /mq/messages`
- `POST /refund-tasks/process`
