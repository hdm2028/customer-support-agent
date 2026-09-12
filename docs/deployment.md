# 部署说明

## 运行方式

FastAPI 入口为 `main:app`：

```powershell
py -3.13 -m uvicorn main:app --host 127.0.0.1 --port 8012
```

容器入口：

```text
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8012}
```

## 配置

生产环境通过环境变量配置：

- `LLM_API_KEY`
- `DATABASE_BACKEND`
- `MYSQL_DSN` 或 `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`
- `REDIS_URL` 或 `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD`
- `MQ_BACKEND`

本机 MySQL 默认端口为 `3306`。本机 Redis 默认端口为 `6379`。

## MySQL

表结构参考 [mysql_schema.sql](mysql_schema.sql)。

推荐配置：

```env
DATABASE_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=customer_support
MYSQL_PASSWORD=change-me
MYSQL_DATABASE=customer_support
SEED_DEMO_DATA=false
```

## Redis

Redis 用于会话状态、缓存、退款锁和退款幂等结果。

Redis 客户端默认连接与读写超时均为 1 秒，不隐式重试；显式 URL 超时参数可覆盖默认值。退款提交后的幂等缓存写入、解锁失败会记录 warning，并保留数据库已提交的结果；运行中提交前的缓存故障仍按工具失败处理。配置了 Redis 但实际使用内存后端时，就绪检查失败。

真实 Redis 隔离检查：`python -m scripts.reliability.run_isolated_redis_checks --output <新的报告目录>`。需要 Docker、本地 redis:7.4-alpine 镜像及专用 TEST_MYSQL_DSN；运行器新建临时 Redis 和 MySQL 测试库，并清理自己的资源，不操作项目 Redis。验证范围见 [Redis 一致性报告](../reports/system_improvement/redis_consistency_report.md)。

```env
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
```

## Docker

生产 API/worker、隔离发布检查、备份恢复与版本切回见 [恢复与发布说明](recovery_and_release.md)。以下单容器命令用于开发启动。

```powershell
docker build -t customer-support-agent .
docker run --rm -p 8012:8012 --env-file .env customer-support-agent
```

开发依赖容器：

```powershell
docker compose -f docker-compose.dev.yml up -d
```

## 健康检查

```text
GET /live
GET /ready
```

live 用作存活探针；ready（health 同义）依赖未就绪时返回 503，用作流量就绪检查。接口详细口径、管理员指标和告警轮询见 [运行观测](observability.md)。`GET /info` 需要访问令牌。
# API 身份配置

受保护接口需要 `Authorization: Bearer <token>`。在服务端 `.env` 或环境变量配置 `AUTH_TOKENS`，值是 JSON：随机令牌映射到 `{"user_id":"实际用户ID","role":"customer"}` 或管理员角色 `admin`。每个令牌至少 32 字符，应使用安全随机值，不要使用用户名或示例字符串。用户 ID 应与订单的 user_id 一致。

未配置身份时接口返回 503；缺少或错误令牌返回 401；越权返回 403。网页顶部输入令牌后连接，令牌只留在当前页面内存。普通用户仅访问自己的订单、会话、退款和工单。审核、任务处理、消息队列、追踪及知识管理接口仅限管理员。旧无归属会话只能由管理员查看，不能被普通用户抢先认领。

新增 conversation_owners 表由数据库启动初始化创建。此改动未自动部署或向当前环境写入真实令牌。对公网部署应通过 HTTPS 传递令牌；可在后续替换 authenticate 适配已有身份系统。

人工接管请求现保存为工单待办；管理员可在工作台认领、结案或转派。转派目标来自 `AUTH_TOKENS` 中已配置的管理员用户 ID；`GET /admin/operators` 不暴露令牌。`POST /admin/tickets/{ticket_id}/reassign` 必须携带目标用户、当前认领人及交接说明，避免旧页面覆盖新状态。这里只记录本应用的人工待办和处理结果，不向外部客服平台发送消息。验证及使用边界见 [人工接管报告](../reports/system_improvement/human_handoff_report.md)。

## 专用测试 MySQL

本机已用安装的 MySQL 8.4.11 二进制建立独立实例，监听 127.0.0.1:13307。它不注册 Windows 服务，不读取业务 my.ini，不改变 MYSQL_DSN。测试账号仅有 support_test_* 库的权限；测试逐项创建随机库并清理。

```powershell
python -m scripts.dev.local_test_mysql start
python -m scripts.reliability.run_isolated_refund_checks --mysql --output reports/system_improvement/mysql_new_run.txt
python -m scripts.dev.local_test_mysql stop
```

start 幂等，重启后可再次执行。stop 核对数据目录、端口和实例 UUID 后才请求关闭，只关闭测试实例。报告路径必须是新路径，以保留旧结果。

TEST_MYSQL_DSN 自动写入已被 Git 忽略的本地 .env，仅供隔离测试 runner 使用；其数据库路径是占位名，runner 会改为每项新建的 support_test_* 库。随机凭据和实例状态保存在 data/cache/local_test_mysql.json；不要提交该文件或 .env。Windows 数据文件和日志位于 %LOCALAPPDATA%/customer-support-agent/test-mysql。脚本不会删除已有数据目录，也不会覆盖指向其他实例的 TEST_MYSQL_DSN。其他主机可直接提供专用 TEST_MYSQL_DSN，无需使用本机实例管理脚本。

## 正式图执行与恢复

安装更新后的 `requirements.txt`。`AGENT_RUNTIME=durable` 为正式 HTTP 接口默认值：
`/agent/chat` 和 `/agent/stream` 共用带 SQLite checkpoint 的图。
`AGENT_RUNTIME=legacy` 可将这两个入口切回原执行路径；已有 checkpoint 不会删除，恢复接口仍可使用。
直接调用内部 `run_workflow` 的脚本不因此自动获得持久化。

`AGENT_CHECKPOINT_PATH` 默认是项目下 `data/checkpoints/agent.sqlite`。
容器中应将该目录挂载到持久卷；`docker-compose.dev.yml` 只启动 MySQL/Redis，不负责保存应用文件。
checkpoint 和业务数据库分别保存图进度与订单/会话数据，应在停止写入后一起备份、恢复，不能只备份其中一个。
数据库初始化会创建 `agent_persisted_turns`，用于原子记录一轮消息是否已经写入。

恢复调用需携带原有身份认证：

- `POST /agent/runs`：先创建任务并取得 `run_id`，尚不执行。需要可靠掌握恢复标识时使用此路径。
- `GET /agent/runs/{run_id}`：查询节点进度和结果。
- `POST /agent/runs/{run_id}/resume`：执行或恢复任务；也可使用 `/resume/stream` 接收 SSE。

只有发起人可以恢复；其他用户不可查看，管理员可以查看但不能代替发起人恢复。
同一会话有未完成任务时，新请求返回 409，需先恢复原任务。
普通接口响应和 SSE 的 `X-Run-ID` 提供恢复标识；网络在收到标识前中断时，建议采用先创建再执行的调用方式。

当前使用线程锁及同一 checkpoint 路径的文件锁串行执行，适用于单机持久盘。
这不是多机器分布式调度实现，长任务会阻塞其他图请求。
恢复粒度是图节点：节点内部执行到一半仍可能重试工具，业务幂等与支付对账依然必须保留。
真实验证范围见 [本轮报告](../reports/semantic_durable_integration/report.md)。

## RAG 实验配置（不作为发布默认值）

`RAG_SEMANTIC_CONTEXT` 默认为空，沿用原向量查询；实验值 `route` 复用已有结构化 `rerank_query` 作为查询 embedding 输入，不改变文档向量、召回权重或候选数量。缺少结构化查询时仍使用原语义查询。

当前政策工具只有在已有 `RAG_EVIDENCE_SELECTION=complementary` 实验路径下才传递完整查询，并固定 Top20/Top5。本次召回 A/B 在该路径下比较，两组只改变 `RAG_SEMANTIC_CONTEXT`。虽然 V2 必要证据补齐，但 V1 和主目标排序退化，因此两个实验开关均未写入默认配置；退出实验时清空 `RAG_SEMANTIC_CONTEXT` 即回到原向量查询。候选缓存按实际 embedding 输入区分。

结果和限制见 [003 独立实验报告](../reports/system_improvement/rag_semantic_context_report.md)。请勿将该配置用于默认发布或宣称已通过整链路回答验收。

`SEMANTIC_ROUTE_REPAIR` 是独立的语义格式修复实验，默认空；`once` 允许在格式校验失败后补一次模型调用，并保持原合法主字段。固定首轮响应实验未提高通过数，且增加一次被原评测禁止的工单调用，因此当前不建议启用。清空此变量即恢复原路由降级行为。原始输出、开销和限制见 [格式修复实验](../reports/system_improvement/semantic_contract_report.md)。
