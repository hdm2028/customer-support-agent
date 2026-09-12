# Redis 与退款一致性实测

本阶段完成原进度表中未实测的 Redis 竞争和故障检查，修复“退款及消息已提交，但提交后的缓存/解锁异常返回工具失败”的问题。没有连接支付渠道，也没有修改业务数据或项目 Redis。

## 环境及范围

使用 Docker 本地已有镜像 `redis:7.4-alpine` 的固定 image ID，实际服务版本 7.4.10；Python redis 客户端版本 8.1.0。每次运行新建仅绑定 127.0.0.1 的临时容器，关闭持久化；测试后核对容器 ID 和运行标签再删除。MySQL 沿用专用测试连接，每项新建并清理随机 support_test_* 库。完整源码快照保存在每个报告目录，不复制 .env 或业务数据库。

实际测试把锁 TTL 固定为 2 秒、等待时间 4 秒，以触发过期情况；没有修改生产默认 15 秒 TTL 和 3 秒等待配置。运行期间项目原有 MySQL/Redis 容器继续运行。

## 发现和修改

固定端口诊断运行中，跨进程创建、令牌保护、持锁进程退出共 3 项通过。真实停止 Redis 后，数据库中的退款及 MQ 消息均为 1 条，但 `refund_apply` 返回工具失败，定位到事务提交后的幂等缓存写入和锁释放异常。

修改仅涉及缓存故障边界：

- 已持久化退款的幂等缓存写入失败记录 warning，不改写已提交结果。
- 释放锁失败记录 warning，保留业务返回值或原业务异常，锁由 TTL 过期回收。
- Redis 连接及读写默认各限制为 1 秒，不由客户端隐式重试；显式 URL 超时配置仍可覆盖默认值。

数据库唯一键和事务消息是防止重复退款及漏消息的最终保证。锁过期或启动时降级到各进程的内存锁，都不能仅凭缓存保证跨进程一致性。本阶段没有把运行中 Redis 读取/抢锁失败自动转换为新退款执行：提交前故障仍返回失败且不新增业务记录；提交后的缓存维护故障不再误报已落库申请失败。

## 最终结果

|检查|实际结果|
|---|---|
|4 个进程使用真实 Redis 同时首次申请|同一 refund_id，1 笔退款、1 条 MQ 消息，仅 1 个首次创建结果|
|旧锁过期，新持有人获得锁，旧持有人释放|旧令牌不能删除新持有人的锁|
|首个退款执行超过锁 TTL，第二个请求完成后首个继续|数据库唯一键确保同一申请；1 笔退款、1 条消息，迟到执行返回幂等结果|
|强制终止持锁子进程|锁到期后新持有人可以获取|
|已有连接运行中停机；随后启动 4 个冷进程|已有连接提交前失败且无写入；冷进程实际退回内存缓存，仍由 MySQL 约束只创建 1 笔退款及消息|
|数据库提交后真实停机 Redis|返回已创建结果，缓存及解锁失败有日志；重启 Redis 后重试返回已有申请，不新增消息|

最终 Redis 专项 **6 项通过**，运行 23.544 秒（pytest 22.58 秒）。[专项清单及源码快照](redis_consistency_final/manifest.json)、[逐项结果和 warning](redis_consistency_final/tests.txt)。真实 Redis 停机/重启、进程终止与数据库唯一性都实际执行；不代表真实支付退款完成。

完整隔离回归 **202 项测试、17 个子测试通过**，9 项环境专属用例跳过；其中 6 项 Redis 用例已在专项中实跑，其余 3 项 MySQL 用例在 MySQL 检查执行。MySQL 全回归 **63 项通过**、3 项 SQLite 专属用例跳过。现存 3 个弃用警告保留。[全量检查](release_checks_redis/manifest.json)。编译、全量、MySQL 耗时分别为 0.704、5.916、53.26 秒。

## 原始运行记录

- [最初运行](redis_consistency_before/manifest.json)：2 通过、2 失败。除了提交后错误，还遇到 Docker 随机端口在容器重启后改变，导致恢复及下一项连接失败；不能将这两项都归为产品逻辑失败。
- [固定端口诊断](redis_consistency_diagnostic/manifest.json)：3 通过、1 失败，隔离出提交后缓存异常。
- [修复后同四项检查](redis_consistency_after/manifest.json)：4 通过。
- [最终六项检查](redis_consistency_final/manifest.json)：额外覆盖过期重叠写入及停机冷启动降级，全部通过。

各阶段保留原结果，没有覆盖失败记录。最后核实临时容器均已移除；运行前已有的 customer-support-mysql 与 customer-support-redis 保持运行。

## 复核与剩余边界

```powershell
python -m scripts.reliability.run_isolated_redis_checks --output reports/system_improvement/redis_new_run
```

要求 Docker 已运行、本地已有 redis:7.4-alpine 镜像，以及配置专用 TEST_MYSQL_DSN。运行器不使用业务 DSN，不拉取其他镜像，不清理其他容器。报告路径必须为新目录。

可回退本阶段缓存异常处理及连接参数，不涉及数据库迁移；没有自动部署。此结果覆盖单 Redis 实例的停机/重启和上述竞争场景，未验证 Redis 集群故障转移、跨机网络分区或生产配置。生产就绪检查仍会把已配置 Redis 却退回内存的情况报告为依赖未就绪，不能将内存降级称为 Redis 正常运行。

原返回字段中 `concurrency_control.strategy` 的历史名称仍包含 redis；本次判断实际后端使用直接读取的 `cache_backend_name()` 和服务行为，不以该名称作为 Redis 生效证据。没有重跑模型评测、validation/final_test 或改变 RAG 实验默认值。
