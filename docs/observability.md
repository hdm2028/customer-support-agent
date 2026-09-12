# 运行观测

`GET /live` 只报告进程存活。`GET /ready`（以及兼容路径 `/health`）检查数据库、缓存、非空知识索引、身份配置和 LLM 凭据；任一未就绪返回 503。配置了 Redis 而实际退回内存缓存也属于未就绪。SQLite 探针实际以只读方式访问 orders 表，不会创建缺失文件。探针不会付费调用远端模型或支付渠道，因此不能证明两者可用。

进程内缓存不能同步其他 worker 的更新，因此订单/画像的业务缓存仅在 Redis 后端启用；内存模式直接读数据库。退款幂等缓存只用于定位申请，返回前读取申请当前状态，防止把其他进程已处理的退款继续显示为旧状态。

`GET /observability/summary?window_seconds=900` 需要管理员 Bearer token，输出聚合数据和 alerts。最大窗口 24 小时，最多读取 10000 条最近完成的 trace；超限会标记 truncated 并触发采样截断告警。返回中没有用户问句、订单号或原始对话。原 `/observability/metrics?limit=50` 仍提供管理员逐条 trace，limit 现在限制为 1–100。

## 口径

- 请求耗时：窗口内已完成 Agent 工作流的 p50、p95、最大值；分位数使用 nearest-rank。未完成工作流、网络传输和其他 HTTP 接口不在该口径；success 是执行是否完成，不是回答准确率。
- 工具：tool_result 数量及失败率；重试调用的 timing 单独参与耗时统计，不重复计为多次最终结果。ToolTransientError/ToolTimeout 分入依赖或超时，其余失败归为业务或逻辑，不能把业务拒绝算作基础设施不可用。
- 队列：所有状态的实时数量；pending/failed 为等待积压，按最早 created_at 计算等待时长；processing 按 MQ_LEASE_SECONDS 检查过期租约。dead_letter 单独告警。
- 支付：payment_submitting/refund_unknown 超过阈值未更新时告警；检查本身不重试提交或查询支付渠道。
- token：保留旧的上下文/回复本地估算，并明确标为不用于计费。llm_calls 记录每次实际 Chat 调用的模型、耗时和供应商 usage；汇总只累加已返回的 usage，流式累计 usage 不重复相加。未返回为未知，不补零或用估算替代。reported_calls/call_count 显示完整性；旧 trace 记为缺少用量埋点。不含 embedding 用量，也不是账单金额。

无请求样本时耗时和失败率为 null；没有供应商 usage 时实际 token 为 null。

## 告警阈值与轮询

|环境变量|默认值|含义|
|---|---|---|
|OBS_REQUEST_P95_MS|15000|请求 p95 毫秒|
|OBS_TOOL_FAILURE_RATE|0.1|工具最终结果失败比例|
|OBS_MIN_SAMPLES|20|耗时/失败率告警所需最少对应样本数|
|OBS_MQ_BACKLOG|100|等待消息数量|
|OBS_MQ_AGE_SECONDS|300|最老等待消息秒数|
|OBS_PAYMENT_STALE_SECONDS|900|支付结果待核实状态未更新秒数|

过期处理租约、死信或超期支付任一条即触发告警。阈值随响应返回，便于核对实际配置。阈值不是已测得的生产 SLO。

```powershell
python -m scripts.observability.check_operations --window-seconds 900
```

该命令读当前配置数据库并输出一份 JSON，不初始化或修改数据库。退出码 0 表示当前检查无告警，1 表示有告警，2 表示无法观测。适合每分钟由部署环境的调度器执行并采集标准输出；当前没有自动安装计划任务或接入邮件/聊天通知。调度器应同时监控自身存活和退出码 2，避免把采集失败当作正常。

隔离 MySQL 复核：

```powershell
python -m scripts.dev.local_test_mysql start
python -m scripts.reliability.run_isolated_refund_checks --mysql --suite observability --output reports/system_improvement/observability_new_run.txt
```

每次使用新的输出路径。不要在业务库中运行带故障注入的测试。
