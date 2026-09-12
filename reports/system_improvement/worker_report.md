# 退款 Worker 运行验证

本轮确认专用测试 MySQL 仍运行在 `127.0.0.1:13307`，使用本地 `TEST_MYSQL_DSN`，逐项创建并清理 `support_test_*` 测试库。业务连接和服务未切换。

核查发现常驻入口 `scripts/maintenance/refund_worker.py` 和 Compose worker 服务早已存在；支付文档中“尚无常驻 Worker”的描述已经过时。本轮没有重写消费者，只补异常轮询的有限退避、配置检查和独立进程测试，并修正文档。

## 修改与验证

|项目|实际结果|
|---|---|
|数据库等批次异常|默认等待 2、4、8、16、30 秒，上限可配置；一次正常批次后恢复原间隔。注入三次异常、恢复、再次异常，实测等待序列为 2、4、5、2、2 秒（测试上限 5 秒）|
|参数检查|拒绝非正数及 NaN/Infinity 间隔；退避上限不得小于正常间隔。原版实际接受 NaN/Infinity，见修改前日志|
|停止|独立常驻子进程接收到停止 Event 后完成已经开始的一批，订单、退款、消息完成状态及一条通知均落库；空闲等待可被停止请求唤醒|
|强制终止与重启|子进程持久领取消息后被强制终止；新 CLI 进程在租约未过期时处理 0 条，将测试租约时间置为过去后恢复 1 条，attempts=2；再次启动处理 0 条，通知仍为 1 条|
|失败消息|真实 CLI 对不存在的退款执行三次失败领取；重试间隔内不再领取，最终转死信；只读运维摘要返回 mq_dead_letter 告警|
|退出状态与日志|--once 区分空/成功批次 0、消息失败 1、批次异常 2；异常日志仅含异常类型，未输出注入的私密连接文本。新增 next_poll_seconds|

单条消息重试、租约、事务、幂等与支付逻辑均未改动。退避针对批次级异常；单条处理失败仍使用既有持久重试规则。测试通过跨进程 Event 验证停止路径，没有在本机 Windows 验证 Unix SIGTERM 的实际投递。租约恢复通过修改隔离测试记录的时间确定性触发，没有等待默认 120 秒墙钟时间。

## 实际产物

- [最终冻结检查](release_checks_worker_final/manifest.json)：编译成功；[完整隔离回归](release_checks_worker_final/unit_integration.txt) **263 passed、11 skipped、52 subtests passed**，pytest 9.33 秒；[专用 MySQL](release_checks_worker_final/mysql_integration.txt) **103 passed、3 skipped、31 subtests passed**。运行器计时分别为编译 0.799 秒、全量 10.393 秒、MySQL 97.993 秒。
- [Worker SQLite 专项](worker_sqlite_cleanup_fixed.txt)：7 passed、10 subtests passed，1.28 秒。MySQL 全量检查也包含全部 7 项 worker 测试。
- [修改前检查](worker_control_before.txt)：非有限间隔校验失败，旧接口没有可配置退避上限。该记录不是模型质量对照结果。
- [首次进程检查](worker_sqlite_after.txt)：4 项控制测试后卡在测试清理。原因是强制终止子进程后，夹具继续通知其 multiprocessing 同步对象；已停止本次测试进程，并改为只通知仍存活的子进程。修正后 SQLite 和 MySQL 检查均完成。未将此夹具问题归为业务消费者失败。
- [源码变化清单](worker_changes.json)、[可回退补丁](worker.patch)：相对上一阶段冻结版本，仅变化 worker、两个检查入口及新增测试四个源码文件；反向应用检查通过。当前源码与本次测试快照一致，app、main、web、知识库未变化。文档更新另外保留在工作区。

跳过项为原有 MySQL/Redis 专项和不适用于 MySQL 的 SQLite 文件检查；没有将依赖失败计作通过。原有三个弃用警告仍保留。

本轮没有重跑模型/Dev、validation 或 final_test，没有修改数据和评分口径，也没有启动业务库消费者或部署新镜像。当前改动不会提高此前 Dev 分数，完整项目尚未验收完成。

真实支付接入、独立 worker 心跳及告警外发、生产身份接入、远端 CI 与浏览器工作台验收仍有待完成。Worker 只将申请推进至退款处理中，不提交资金退款。操作方式见 [恢复与发布说明](../../docs/recovery_and_release.md)。
