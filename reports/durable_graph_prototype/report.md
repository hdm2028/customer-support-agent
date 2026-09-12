# 统一图执行入口与持久化恢复：隔离原型

2026-09-11。已跑通普通 HTTP、SSE、进程重启恢复，共 **10/10 项运行冒烟检查通过**。这是运行机制验证，不是业务准确率；没有运行 Routing/RAG/Tool/Answer/E2E 五层测评。

## 实现

- `app/agent/entry/workflow.py` 的图构建函数增加可选 `checkpointer`、`node_adapter` 参数，复用原有六个节点和边。默认调用仍不启用持久化。
- `app/agent/entry/durable_runtime.py` 提供统一执行器：普通响应收集 `events()` 的最终结果，SSE 转发同一个 `events()`；两者都由 `graph.stream()` 执行图。
- 每轮请求生成一个 `run_id`，对应 LangGraph `thread_id`；`conversation_id` 继续关联多轮会话。先保存初始状态，再执行；恢复时传入 `None`，从 checkpoint 记录的后续节点继续。已完成的 run 直接返回保存结果。
- 使用官方 `SqliteSaver`，`durability="sync"`，序列化仅额外允许项目的 `RouteDecision`、`ToolResult` 两类。节点适配器复制输入并显式返回完整更新，保留原先在节点内部修改的路由和 Trace。恢复后的耗时按墙钟计算，包含暂停时间。
- `scripts/dev/durable_graph_demo.py` 启动独立 FastAPI 服务，普通、SSE、状态查询和恢复接口可在 `/docs` 操作。只绑定本机 `127.0.0.1`，单进程串行运行。

选择这个方案，是因为项目已经使用 LangGraph，可以复用现有业务图来验证恢复；SQLite 无需新增数据库服务，适合本轮隔离原型。正式 `main.py`、聊天接口和原 SSE 实现没有切换。

## 实际验证

成功产物：[smoke_20260911_204251_6cd8ce.json](smoke_20260911_204251_6cd8ce.json)，总用时 **7.392 秒**，包含服务启动、请求、强制结束进程及重启，不是请求延迟基准。

| 检查 | 结果 |
|---|---|
| 普通 HTTP 与 SSE 都经过同一六节点图 | 通过 |
| 同一查询的回复、路由、工具结果一致；各写入一轮会话 | 通过 |
| 退款工具节点结束后暂停，保存下一节点 | 通过，下一节点为 `build_model_context` |
| 强制结束服务后，新进程读回相同 checkpoint | 通过，服务 PID 从 28616 变为 40576 |
| 恢复只执行尚未完成的节点 | 通过，仅执行上下文构建、回答、结果保存三个节点 |
| 恢复不重复创建本次退款和 MQ 消息 | 通过，两张表各保持 1 条，会话由 0 条变为 2 条消息 |
| 对已完成 run 重复普通/SSE 恢复 | 通过，返回原结果，无新增执行或写入 |
| 恢复不存在的 run | 通过，HTTP 404 |
| 同会话新一轮使用独立 run，保留上一轮结果 | 通过 |
| 恢复前后 Trace 连续、节点完整、耗时可解释 | 通过 |

退款路径实际调用 `order_lookup → policy_search → risk_check → refund_apply`，执行现有业务代码并写入私有 SQLite。最终回复是“退款申请已受理，正在等待处理”，没有宣称资金已退回。

为了只验证运行机制，路由采用两个明确的固定输入：`查询订单10009`、`申请退款10009`；回答采用已有确定性分支。知识检索使用本地 embedding 和知识库副本；没有调用真实模型、支付渠道、MySQL、Redis，也没有启动退款消费器。

初次就绪检查误把 Windows 虚拟环境启动器 PID 当成服务 PID，未执行测试即超时，记录保留在 `smoke_20260911_204018_bdea91.json`。已修正就绪判断，并在重启测试中强制结束完整子进程树；上表来自修正后的完整运行。

## 数据位置

本次成功运行保留在 `reports/durable_graph_prototype/runtime/20260911_204251_6cd8ce/`：

- `business.sqlite`：隔离订单、退款、MQ、会话等业务表。
- `checkpoints.sqlite` 及其 WAL 文件：LangGraph 状态、待执行节点及中间写入。
- `data/traces/agent_trace.jsonl`：执行 Trace。
- `app/`、`data/knowledge/`：本次执行使用的源码和知识库副本，不含 `.env`。

该运行目录和 `.venv-durable/` 已忽略提交。普通演示默认使用 `data/durable_demo/`，重启时保留原来的源码快照和数据库；要验证新的代码版本，使用新的 `--workspace` 路径。

## 本机复现

本次已建立 `.venv-durable`，并单独安装可选依赖。新环境可执行：

```powershell
python -m venv --system-site-packages .venv-durable
.\.venv-durable\Scripts\python.exe -m pip install -r requirements-durable.txt
```

以上复用当前项目已安装的基础依赖；全新环境需要先安装 `requirements.txt`。原型固定 LangGraph 1.2.10、checkpoint 4.2.0、SQLite saver 3.1.1。

自动完成普通/SSE和跨进程恢复检查，每次创建新的隔离目录：

```powershell
.\.venv-durable\Scripts\python.exe scripts/dev/smoke_durable_graph.py
```

手动演示：

```powershell
.\.venv-durable\Scripts\python.exe scripts/dev/durable_graph_demo.py
```

打开 `http://127.0.0.1:8147/docs`，向 `POST /runs` 或 `/runs/stream` 提交：

```json
{"message":"申请退款10009","pause_after_tools":true}
```

保存响应的 `run_id`，停止并重新启动同一工作目录的服务。先用 `GET /runs/{run_id}` 查看保存状态，再调用 `POST /runs/{run_id}/resume` 或 `/resume/stream` 继续；无需重传原始输入。

## 本轮边界

验证的是**已提交 checkpoint 的节点边界恢复**。所有工具仍位于 `orchestrate_agents` 一个节点中，节点内部崩溃可能重放工具调用；业务写入与 checkpoint 也不是同一个事务，尤其会话写入与 checkpoint 之间中断仍有重复写入窗口。因此本原型不能作为任意崩溃场景下的 exactly-once 保证。

恢复需要显式调用接口；尚无后台自动恢复、跨进程执行锁或正式接口的身份权限集成。SSE 提供节点、路由、工具和完整回答事件，没有接入模型 token 流。这些都不影响本轮已验证的隔离运行路径，但正式接入前需要另行实现。

本轮没有替换默认方案。`before.zip` 保存本轮开始时已有文件的版本，便于核对；回退应仅撤销本轮改动，保留此前尚未提交的项目修改。
