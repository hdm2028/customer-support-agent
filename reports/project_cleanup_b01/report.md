# 项目整理与重构 B01

完成日期：2026-09-10。基于当前未提交的工作区进行整理，修改前保存实际文件快照。本轮保持业务行为，清理无调用代码和重复结构，没有调整模型、评分口径或业务规则。

## 完成的整理

| 位置 | 原问题与调用证据 | 本轮修改 |
| --- | --- | --- |
| `app/agent/routing/router.py` | 当前 Orchestrator、评测入口均使用 V2；旧模块只有 `get_issue_type` 被 AfterSales 使用，其余规则路由无调用。 | 将名称映射与函数原样移至 `routing/labels.py`，更新调用方，删除旧路由文件。 |
| `app/storage/database.py` | 30 个函数先定义 SQLite 实现，再保存别名，最后以同名函数覆盖为公共入口。 | 28 个仍使用的 SQLite 实现直接命名为 `_sqlite_*`，删除 30 条别名赋值和未调用的旧初始化检查、MQ 领取实现。公共门面保持原样。 |
| `app/storage/mysql_database.py` | `claim_mq_messages_from_mysql` 无调用；公共 MQ 领取入口实际委托 `app/mq/recovery.py`。 | 删除旧的 pending-only 领取实现，继续使用现有租约、重试及超时恢复路径。 |
| `app/agent/routing/llm_router.py` | 1201 行文件中约 800 行为提示词。 | 将提示词原样移至 `semantic_prompt.py`，逻辑文件缩至 401 行；保留 `llm_router.SYSTEM_PROMPT` 导入名。 |
| 辅助代码 | 全仓引用检查未找到有效调用，也未发现对应动态注册或重导出。 | 删除 `load_orders`、`has_failed_policy_search`、`is_order_related`、`should_ask_order_id`、`split_text_with_overlap`，及其专属常量和无用导入。 |
| 根 README、Agent README | 仍指向旧路由和不存在的工具、回复文件。 | 按真实请求链路更新模块路径、记忆存储职责与隔离测试入口。 |

合计删除 12 个未使用的函数实现。Python 文件净减少 **546 行**（按物理行统计，包含空行；已扣除提示词与映射迁移新增的行）。这项收益是维护成本降低，不能解读为推理准确率或运行速度提升。

保留统一数据库入口，避免改变既有数据库路径、初始化标记、事务连接借用及缓存失效回调的归属。RAG 分块只删除无调用的旧接口包装；实际分块函数、策略与版本保持原样。有效的实验脚本、测试集和历史结果继续保留。

## 验证结果

使用相同 Python 和依赖，在冻结源码副本中分别运行 `scripts.maintenance.check_release`。该检查使用临时 SQLite、内存缓存，不携带业务或模型凭据。

| 检查项 | 整理前 `before` | 最终版本 `after_final` |
| --- | --- | --- |
| 编译检查 | 通过 | 通过 |
| pytest 测试 | 296 passed | 296 passed |
| pytest 子测试 | 83 passed | 83 passed |
| 跳过 | 11 | 11 |
| 警告 | 3 | 3 |
| 数据库顶层同名函数覆盖 | 30 组 | 0 组 |

11 项跳过包含 5 项专用 MySQL 检查和 6 项隔离 Redis 检查，本轮没有验证这些真实依赖。3 项警告来自既有 FastAPI/Starlette 弃用项。本轮复用现有退款、MQ、权限、记忆、RAG、路由及其他回归测试，没有改写测试断言或新增只检查代码形状的测试。

额外用 AST 对比确认：44 个数据库公共入口及共享函数未变；28 个 SQLite 实现除函数名外未变；语义解析与校验函数、允许值集合、AfterSales 类、工单标签和其他保留的函数体未变。提示词的 **9414 个字符完全一致**。具体记录见 [结构校验](structural_verification.json)。

源码快照对比显示，评测脚本、测试、知识库和订单种子数据均未变化，其余 197 个冻结文件也保持原字节；见 [范围校验](scope_verification.json)。未运行新的真实模型质量评测，不据此更新此前 Answer/E2E 的业务通过率。`after` 为清理无用导入之前的中间回归，最终验收使用 `after_final`。

## 产物与回退

- [整理前测试日志](before/unit_integration.txt)、[最终测试日志](after_final/unit_integration.txt)。
- [整理前清单](before/manifest.json)、[最终清单](after_final/manifest.json)，各目录包含实际源码快照。
- [本轮独立修改补丁](changes.patch)，相对于本轮开始时的工作区生成，而非 Git HEAD。
- [本轮回退补丁](rollback.patch)，已在最终工作区通过 `git apply --check`。
- `changed_files_before.zip` 保存本轮涉及的 11 个原文件字节，包含根 README；两个新增模块记录在补丁中。

需要回退本轮时，在仓库根目录执行：

```powershell
git apply --check reports/project_cleanup_b01/rollback.patch
git apply reports/project_cleanup_b01/rollback.patch
```

若之后又编辑这些文件，应先检查补丁是否仍适用，不要用 Git HEAD 覆盖此前未提交的成果。
