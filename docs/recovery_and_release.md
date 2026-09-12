# 备份、恢复与发布

## 数据恢复

SQLite 使用在线 backup API，包含已提交 WAL；MySQL 使用 mysqldump 的 InnoDB 单事务快照。输出目录必须不存在，完成后才写 manifest.json，记录校验摘要、字节数和数据库版本。校验失败的备份不进入恢复。

本项目 MySQL 备份脚本覆盖现有全部业务表、索引和外键；发现非 InnoDB 表、视图、存储程序、事件或触发器时拒绝，以免把不完整备份记为成功。备份期间应冻结 DDL。恢复使用本工具生成且由你保管的可信备份；摘要用于检测损坏，不是来源认证。

```powershell
# SQLite：每次使用新备份目录，恢复到新的数据库文件。
python -m scripts.maintenance.database_backup backup --backend sqlite --sqlite-path data/customer_support.db --directory data/backups/sqlite_20260908
python -m scripts.maintenance.database_backup restore --backend sqlite --directory data/backups/sqlite_20260908 --sqlite-path data/restored/customer_support.db

# MySQL：连接值在本地环境变量中，不作为命令行参数。
python -m scripts.maintenance.database_backup backup --backend mysql --dsn-env MYSQL_DSN --directory data/backups/mysql_20260908
python -m scripts.maintenance.database_backup restore --backend mysql --dsn-env RESTORE_MYSQL_DSN --directory data/backups/mysql_20260908 --new-database support_restore_20260908
```

RESTORE_MYSQL_DSN 指向恢复环境，账号需要新建目标库和导入权限。脚本不覆盖已有数据库、不自动删除失败的恢复库；失败目标保持不可用，修正原因后另选新库重新恢复。恢复不自动切换应用连接。

恢复后先在隔离应用检查订单、退款、审核、会话归属、消息状态、通知与完整性，再切换连接。停止旧 API 和退款 worker，避免两个环境同时执行任务。使用新的 Redis DB/缓存命名空间，不把旧订单缓存、幂等结果或锁带入恢复环境。备份前到恢复时可能已发生外部支付：必须先按原 refund_id 幂等键向支付渠道核对；不能依据恢复出的 queued/processing 状态重新发起支付。当前 worker 只处理数据库事件，不会提交资金退款。

备份含业务数据，仅本地保管；data/backups 已加入 Git 和容器忽略规则。保留周期、加密存储及异地副本由部署环境配置。本轮只生成测试备份，未复制或上传真实业务库。知识文档与发布镜像/源码版本一起保留；实际访问凭据独立恢复，不能从源码快照恢复。

## 发布前检查

```powershell
python -m scripts.maintenance.check_release --mysql --output reports/release_20260908
```

该命令冻结源码后复制到临时目录，屏蔽业务连接、支付适配器、LLM 凭据，以隔离环境执行编译和 tests。--mysql 额外要求 TEST_MYSQL_DSN，执行并发、备份和观测专项；缺配置会失败，不视为跳过后通过。保存精确源码快照、依赖版本清单、各项退出码和日志。没有运行 validation/final_test，也没有复制 eval 数据。

GitHub Actions 配置位于 .github/workflows/checks.yml，push/PR 自动执行同一隔离检查并保存报告；远端流程须提交代码后才会运行，本地通过不代表远端已通过。依赖版本清单是运行证据，源码快照不是包含完整 Python 环境的可执行镜像。

## 容器运行与回滚

docker-compose.app.yml 包含 API 和退款 worker，共用同一镜像版本，数据库/Redis 由部署环境提供。数据库连接中的 localhost 是容器自身，应配置容器可达的主机名。生产容器强制 SEED_DEMO_DATA=false。开发模式显式设置 true 时，也只补充缺失的种子记录，已有订单/画像不会被覆盖。

```powershell
# 先构建并记录不可变镜像 ID；推送仓库后用其 registry digest。
docker build -t support-agent:release_20260908 .
docker image inspect support-agent:release_20260908 --format '{{.Id}}'

# SUPPORT_IMAGE 设置为已验证镜像的不可变引用，访问凭据由本地 .env 提供。
docker compose -f docker-compose.app.yml config --quiet
docker compose -f docker-compose.app.yml up -d
```

发布记录保留前一个已验证镜像引用、当前引用、配置版本和备份路径。上线后以 /ready 返回 200 作为接流量条件；/live 只用于存活检查。身份、数据库、缓存和索引未就绪时不接流量。

如果新版本未就绪，先停止新 API/worker，把 SUPPORT_IMAGE 恢复为之前记录的镜像引用，再执行同一个 compose up -d；核对 /ready、订单/退款状态、消息积压和权限。API 与 worker 一起切回。代码回滚通常保留当前数据库，不能用旧数据覆盖已发生的支付结果。涉及不兼容 schema 的版本，必须先有兼容迁移与回退方案；不能直接切旧镜像。

```powershell
# 隔离演练：独立命名卷、演示数据，注入一个启动失败的镜像后切回原镜像。
python -m scripts.maintenance.verify_container_rollback --image support-agent:release_20260908 --output reports/rollback_drill_20260908
```

演练检查就绪、401/403 权限边界、退款状态在容器替换后保留、已完成事件不再消费；只移除本次带唯一标签的测试资源，保留用户提供的原镜像。它验证运行层切回流程，不证明任意历史版本的数据库兼容性。演练配置了非真实模型凭据以检查配置就绪，不调用真实模型或支付渠道。

worker 可单独运行 `python -m scripts.maintenance.refund_worker`，或加 `--once` 只处理一批。它读取当前数据库配置并实际消费任务，不能用业务连接运行故障测试。正常轮询默认 `--interval 2` 秒、`--batch-size 10`（范围 1–100）；批次异常时按 2、4、8、16、30 秒退避，正常取得一批后恢复原间隔。`--max-backoff` 可指定退避上限，必须为有限值且不小于 interval，默认取 30 秒与 interval 的较大值。

单条失败的重试仍由数据库消息状态控制：`MQ_RETRY_SECONDS=30`、`MQ_LEASE_SECONDS=120`、`MQ_MAX_ATTEMPTS=3` 为默认值；达到上限进入死信，worker 不自动生成替代事件。收到 SIGINT/SIGTERM 后处理完当前批次退出，等待退避时可立即唤醒；强制终止后的未完成领取要等租约过期才能恢复。Windows 强制结束进程属于崩溃路径，不保证触发 Python 信号处理器。Compose 的 45 秒停止宽限期应结合实际批次耗时设置；超时强制终止仍依靠租约恢复。

标准输出每批一行 JSON，包含处理数、失败数、实际业务迁移数、耗时及 `next_poll_seconds`；批次异常只输出异常类型，不记录连接信息或消息正文。`--once` 退出码 0 表示本批无失败（可为空），1 表示消息处理失败，2 表示批次异常；常驻模式会继续退避重试。正常退出不表示队列已清空，也不表示支付成功。队列死信/过期租约/积压的观测轮询见 [观测说明](observability.md)；独立 worker 心跳和外部通知渠道尚未配置。

隔离进程检查：`python -m scripts.reliability.run_isolated_refund_checks --mysql --suite worker --output <新的报告路径>`。仅读取 `TEST_MYSQL_DSN` 并新建随机测试库；不加 `--mysql` 时使用临时 SQLite。覆盖真实子进程停止、强制终止后的领取恢复、重复启动与死信观测。业务处理和通知仅允许一次；此检查不接入真实支付。
