# P2 恢复与发布实测（2026-09-08）

本阶段完成 SQLite/MySQL 备份恢复、隔离发布检查、容器启动失败后的版本切回演练，并修复演练暴露的启动播种与业务缓存问题。未替换现有业务进程，未部署生产凭据、真实支付或外部通知渠道。

## 发现与处理

|实际问题|修复及证据|
|---|---|
|启动种子同步会把退款处理中订单重置|SQLite 尊重 SEED_DEMO_DATA；两种数据库播种都只插入缺失记录，不覆盖已有订单/画像。[冷缓存失败记录](backup_restart_cold_cache_before.txt)，后续 SQLite/MySQL 重启测试通过。最初复用进程缓存的检查未暴露 DB 重置，已修正测试方法|
|worker 更新订单后，API 进程内缓存仍返回旧状态|业务数据在内存模式直接读 DB；Redis 后端保留共享缓存。[首次容器演练失败](container_rollback/result.json)；修复后同一检查通过|
|幂等缓存中的退款快照可能落后于其他进程的处理|缓存只定位 refund_id，校验 order_id 并返回数据库当前状态；新增状态更新后的幂等重放测试，不能用旧快照替代当前退款状态|
|缺少备份与恢复操作|新增 SQLite 在线快照、MySQL 单事务表快照；恢复仅创建新文件/新库。损坏备份和已存在目标被拒绝|
|缺少独立 worker、自动检查及回退入口|新增可停止的 worker、API/worker compose 配置、隔离检查脚本和 GitHub Actions；容器入口使用 exec 传递终止信号|
|构建目录包含本地无关文件|排除 .venv、.env.*、报告、测试数据、备份与本地技能目录|

## 实际结果

- [最终隔离发布检查](release_checks_cache_fixed/manifest.json)：编译成功；[154 passed、15 subtests passed、2 skipped](release_checks_cache_fixed/unit_integration.txt)；[MySQL 22 passed、3 skipped](release_checks_cache_fixed/mysql_integration.txt)。SQLite 阶段跳过 MySQL 专用项，MySQL 阶段跳过 SQLite 文件检查；不存在把依赖失败改成通过。
- MySQL 恢复逐表比较全部列和行，包括订单、退款、消息、通知、审核和会话表；已有目标库返回错误码 1007，未覆盖。SQLite 验证已提交 WAL、源库备份后变化、恢复内容及损坏拒绝。
- [最终镜像构建](docker_build_cache_fixed.txt)原生命令退出码 0；[镜像记录](container_cache_fixed_metadata.json)保留实际 ID。原始首次构建的 PowerShell 包装命令曾返回 1，但完整导出日志和 image inspect 证明镜像已生成；后续用 subprocess 保存原生命令退出码，未把包装状态当作镜像结论。
- [实际容器回滚](container_rollback_cache_fixed/result.json)：37.396 秒。先在正常镜像创建并处理演示退款，再运行启动退出码 23 的故障候选，检查未就绪后切回原镜像。401/403 边界、恢复就绪、业务状态保留和完成消息不再消费均通过。本次测试容器、卷与故障镜像已清理；原测试镜像保留。
- Compose 配置通过 docker compose config --quiet；CI YAML 已解析。GitHub Actions 文件采用[官方 setup-python 示例](https://github.com/actions/setup-python/blob/main/README.md)及[upload-artifact 用法](https://github.com/actions/upload-artifact)，尚未提交触发远端 CI，不能宣称远端通过。

## 范围与剩余工作

容器演练使用独立 SQLite 数据卷、原镜像和故障入口镜像，验证运行层切回，不能证明任意历史 schema 兼容性或生产 MySQL/Redis 容器组合。演练没有真实模型调用或支付提交。MySQL 数据恢复则使用本机独立 8.4.11 实例完成。

备份保留、加密/异地存放、生产配置、远端 CI 和外部告警通知仍需部署环境接入。完整命令及状态切换前的支付对账要求见 [恢复与发布说明](../../docs/recovery_and_release.md)。Docker Desktop 现已启动；既有 MySQL/Redis 开发容器由 Desktop 自动恢复，未修改或清理这些容器。

等待构建时完成了 [003 只读召回定位](rag_003_recall_note.md)：退款资格证据位于全库第 26，原 Top20 已逐项复现；主目标和人工审核证据没有缺失。未修改召回、数据或原索引，未运行 validation/final_test。剩余 P1 售后闭环、回答与召回联调继续推进，整体目标尚未完成。
