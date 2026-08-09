# 优化记录

本文档记录中文电商智能售后客服 Agent 的关键工程优化。重点保留对项目能力、业务可靠性和面试表达有价值的优化，不记录临时调试、语法修复和低价值过程记录。

## 当前评估基线

截至当前版本，项目回归结果如下：

| 评估脚本 | 评估内容 | 结果 |
| --- | --- | --- |
| `scripts/run_eval.py` | 路由决策和工具调用 | 16/16 |
| `scripts/run_rag_eval.py` | RAG 来源命中和关键词命中 | 8/8 |
| `scripts/run_answer_eval.py` | Citation 引用和风险控制 | 16/16 |
| `scripts/multi_turn_smoke_test.py` | 多轮槽位补全 | 通过 |
| `scripts/api_smoke_test.py` | API 主链路 | 通过 |

这些结果表示当前评估集全部通过，不表示生产环境永远 100% 正确。真实线上场景仍需要持续接入 trace、feedback 和失败样例回流。

## 1. 路由与工具调用评估闭环

### 问题

早期 Agent 能完成基本问答，但缺少稳定评估机制，无法量化判断：

- 是否应该查询订单。
- 是否应该检索政策。
- 是否应该创建工单。
- 是否存在误建工单或漏建工单。

初始评估中，工具调用通过率只有 `58.33%`，主要问题包括保修、物流异常、地址修改漏建工单，以及会员咨询、缺货咨询误建工单。

### 优化

新增 `scripts/run_eval.py` 和 `data/eval/customer_support_eval.jsonl`，覆盖：

- 保修维修。
- 物流异常。
- 定制商品退货。
- 退款时效。
- 支付异常。
- 发票。
- 地址修改。
- 缺货补发。
- 会员权益。
- 投诉升级。
- 提示词注入。
- 缺订单号追问。
- 高风险转人工。

同时优化 Router：

- 区分政策咨询和具体订单处理。
- 工单创建通常需要订单号承载。
- 高风险动作进入人工审核链路。
- 对缺失信息进行追问，不直接执行工具。

### 结果

```text
Agent eval: 15/15
路由通过率: 1.0
工具通过率: 1.0
```

### 价值

这一步建立了 Agent 优化的基础闭环：

```text
失败样例 -> eval 复现 -> 定位 Router 问题 -> 优化规则 -> 回归测试
```

## 2. 高风险动作与兜底策略

### 问题

售后客服涉及真实资金和履约状态，不能让 Agent 自动完成：

- 退款。
- 赔付。
- 取消订单。
- 修改地址。
- 发放优惠券。
- 修改发票。

早期策略容易只追问订单号，忽略“该动作需要人工审核”的业务边界。

### 优化

在 `app/agent/fallback_policy.py` 和 `app/agent/agent_core.py` 中引入风险控制：

- 高风险动作必须提示人工审核。
- 提示词注入直接拒绝。
- 信息不足时追问必要槽位。
- 当“缺信息”和“高风险”同时存在时，同时说明两件事：
  - 需要补充哪些信息。
  - 该动作不能由 Agent 直接执行。

### 结果

新增 `scripts/run_answer_eval.py`，检查最终回答是否满足：

- 调用了 `policy_search` 时必须引用 citation。
- 高风险请求必须包含人工审核或不能直接执行的表达。

当前结果：

```text
Answer eval: 15/15
Citation 通过率: 1.0
风险控制通过率: 1.0
```

### 价值

这一步把 Agent 从“能回答”推进到“有业务边界地回答”。在客服场景中，这比单纯回答流畅更重要。

## 3. RAG 检索质量评估

### 问题

Agent 主评估只能看到是否调用了 `policy_search`，不能判断 RAG 召回的文档是否正确。

如果 RAG 召回错误，最终回答即使格式正确，也可能引用错误政策。

### 优化

新增：

```text
scripts/run_rag_eval.py
data/eval/rag_eval.jsonl
```

评估指标：

- `Top1 来源命中率`。
- `Top3 来源命中率`。
- `关键词命中率`。

覆盖场景：

- 定制商品七天无理由。
- 保修维修。
- 物流 48 小时不更新。
- 支付扣款未同步。
- 电子发票。
- 会员售后权益限制。
- 修改收货地址。
- 缺货补发。

### 结果

初始 RAG eval 为：

```text
7/8
```

失败集中在缺货补发场景。定位后发现 hybrid search 的业务关键词缺少库存类词汇，补充：

```text
缺货、补发、补货、继续等待、拆单、预售
```

优化后：

```text
RAG eval: 8/8
Top1 来源命中率: 1.0
Top3 来源命中率: 1.0
关键词命中率: 1.0
```

### 价值

这一步把 RAG 从“凭感觉检索”升级为可量化评估。后续更换 embedding、chunk 策略或 rerank 方式时，可以直接用同一套评估集回归。

## 4. Evidence Context 与 Citation 可追溯

### 问题

直接把工具返回的原始 JSON 塞进 prompt，会带来几个风险：

- 订单信息、政策证据和工单结果混在一起。
- 模型不一定稳定引用来源。
- 证据边界不清晰，容易生成无依据承诺。

### 优化

在 `app/agent/agent_core.py` 中将工具结果整理为结构化上下文：

```text
[订单信息]
订单号、商品名称、订单状态、物流状态、签收日期、备注

[售后政策证据]
来源、相关分数、政策正文

[工单信息]
工单状态、风险提示、关联订单、问题类型、下一步
```

同时在 prompt 中要求：

- 使用政策证据时必须引用来源。
- 证据不足时不能编造。
- 高风险操作只能解释规则或创建待审核工单。

### 结果

最终回答质量评估：

```text
Answer eval: 15/15
Citation 通过率: 1.0
风险控制通过率: 1.0
```

### 价值

这一步提升了回答的可解释性和可追溯性。用户、客服主管或审核系统都可以看到回答依据来自哪份政策文档和哪个章节。

## 5. Query Enrichment 条款级检索优化

### 问题

RAG 文档级命中率较高，但具体章节仍可能误排序。

典型例子：

```text
用户：帮我改收货地址。订单 10009
订单状态：待发货
```

原始检索可能命中同一份文档里的：

```text
订单取消与修改政策.md - 已发货订单修改
```

但更准确的条款是：

```text
订单取消与修改政策.md - 修改收货地址
```

### 优化

在执行 `policy_search` 前构造增强 query：

```text
用户原始问题
+ 用户意图
+ 订单状态
+ 物流状态
+ 商品名称
+ 订单备注
+ 风险边界
```

示例：

```text
帮我改收货地址。订单 10009
用户意图：修改收货地址 地址修改 出库前 仓库确认
风险边界：高风险操作 需要人工审核 不能直接执行
订单状态：待发货
物流状态：待发货，仓库尚未出库
商品名称：儿童安全座椅 B6
订单备注：待发货订单可能支持修改地址，但需要人工客服确认仓库是否已出库。
```

### 结果

修改地址场景的首条引用从：

```text
订单取消与修改政策.md - 已发货订单修改
```

优化为：

```text
订单取消与修改政策.md - 修改收货地址
```

三套评估保持通过：

```text
Agent eval: 15/15
RAG eval: 8/8
Answer eval: 15/15
```

### 价值

这一步说明项目不是只追求“找到相关文档”，而是进一步优化到“找到正确业务条款”。

## 6. 多轮多槽位补全

### 问题

真实客服任务往往需要多轮补齐信息。地址修改场景至少需要：

```text
order_id
new_address
```

只有订单号就创建工单，会导致工单信息不完整，人工客服仍需二次追问。

### 优化

新增 `app/agent/pending_task.py`，将 pending task 从主流程拆成独立模块，并支持：

- `required_slots`：当前任务需要的槽位。
- `slots`：已经收集到的槽位。
- `missing_slots`：仍然缺失的槽位。
- 多轮合并用户输入和历史任务。

地址修改流程：

```text
用户：帮我改收货地址。
Agent：请提供订单号和新的收货地址。

用户：10009
Agent：订单号已收到，请继续提供新的收货地址。

用户：新地址是北京市朝阳区望京街道88号
Agent：查询订单、检索政策、创建地址修改工单。
```

### 结果

`scripts/multi_turn_smoke_test.py` 通过：

```text
第三轮工具：
['order_lookup', 'policy_search', 'create_ticket']

第三轮引用来源：
订单取消与修改政策.md - 修改收货地址
```

并同步调整 `eval_007` 的业务预期：已有订单号但缺新地址时，应继续追问，不能直接创建工单。

### 价值

这一步让 Agent 具备真实客服任务的连续性，不再是每轮独立问答。它可以围绕一个业务目标持续收集必要信息，直到满足执行条件。

## 7. 模块化重构

### 问题

随着功能增加，`agent_core.py` 承担了过多职责，不利于维护和扩展。

### 优化

将关键能力拆成独立模块：

```text
agent_core.py        Agent 主流程编排
router.py            路由判断、订单号提取、工单类型判断
pending_task.py      多轮槽位补全
fallback_policy.py   信息不足和高风险策略
guardrails.py        输入安全防护
memory.py            会话记忆和 pending task 存储
```

### 结果

重构后所有回归通过：

```text
Agent eval: 15/15
RAG eval: 8/8
Answer eval: 15/15
Multi-turn smoke test: 通过
API smoke test: 通过
```

### 价值

模块边界清晰后，后续可以独立升级：

- 将规则 Router 替换为 LLM Router。
- 将内存 pending task 替换为 Redis 或数据库。
- 将本地向量索引替换为 Chroma、Milvus 或 FAISS。
- 对 Router、RAG、Answer 分别做持续评估。

## 8. SQLite 持久化改造

### 问题

早期版本中，项目的数据状态分散在不同位置：

- 订单来自 `data/orders.json`。
- 会话消息和 pending task 保存在进程内存中。
- 工单只作为工具返回值存在，没有真正落库。
- feedback 追加写入 JSONL 文件。

这会带来几个问题：

- 服务重启后会话和 pending task 丢失。
- 创建过的工单无法查询和复盘。
- 数据读写入口不统一，后续接管理后台或部署环境不方便。
- feedback 难以和会话、工单进行关联分析。

### 优化

新增：

```text
app/storage/database.py
```

使用 SQLite 作为本地持久化数据库：

```text
data/customer_support.db
```

数据库表：

| 表 | 作用 |
| --- | --- |
| `orders` | 保存订单数据，启动时从 `orders.json` 同步种子数据 |
| `tickets` | 保存工单草稿 |
| `conversation_messages` | 保存多轮会话消息 |
| `pending_tasks` | 保存待补全任务和槽位状态 |
| `feedback` | 保存用户评分和反馈 |

同步改造：

- `store.py`：订单查询从 SQLite 读取。
- `support_tools.py`：`create_ticket()` 创建工单后写入 `tickets` 表。
- `memory.py`：会话消息和 pending task 持久化到 SQLite。
- `feedback_store.py`：用户反馈写入 `feedback` 表。
- `main.py`：启动时初始化数据库，并新增 `/tickets` 工单查询接口。

### 结果

数据库表已正常写入：

```text
orders: 10
tickets: 已生成多条工单草稿
conversation_messages: 已保存多轮会话
pending_tasks: 已保存待补全任务
feedback: 已保存用户评分
```

回归结果：

```text
Agent eval: 15/15
RAG eval: 8/8
Answer eval: 15/15
Multi-turn smoke test: 通过
API smoke test: 通过
```

### 价值

这一步让项目从“进程内状态的 Agent 服务”升级为“具备基础持久化能力的客服系统”：

- 工单可以查询和追踪。
- 会话可以跨请求、跨重启保存。
- pending task 不再依赖进程内存。
- feedback 可以用于后续评估集构建和失败案例回流。
- 后续可以平滑替换为 MySQL、PostgreSQL 或云数据库。

## 9. Docker 与云部署配置

### 问题

本地项目可以通过 uvicorn 启动，但如果要给面试官或线上环境演示，还需要解决：

- 运行环境如何固定。
- 依赖如何安装。
- 云平台如何知道启动命令。
- API Key 如何安全注入。
- SQLite 数据库文件如何在容器重启后保留。
- 哪些本地文件不应该进入镜像。

### 优化

新增部署相关文件：

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 固定 Python 3.13 环境，安装依赖并启动 FastAPI |
| `.dockerignore` | 排除 `.env`、数据库、缓存、日志等本地文件 |
| `.env.example` | 提供环境变量模板，不包含真实密钥 |
| `render.yaml` | Render Blueprint 配置 |
| `docs/deployment.md` | 部署说明文档 |

同时把 SQLite 路径改成环境变量：

```text
DATABASE_PATH
```

本地默认：

```text
data/customer_support.db
```

云部署时：

```text
/var/data/customer_support.db
```

这样数据库文件可以写到持久化磁盘路径，避免容器重启后丢失数据。

### 结果

本地代码回归通过：

```text
compileall: 通过
db_smoke_test: 通过
Agent eval: 15/15
```

由于当前机器没有安装 Docker，未执行本地镜像构建；部署文件已完成配置级验证。

### 价值

这一步让项目具备部署交付能力：

- 可以用 Docker 固定运行环境。
- 可以通过 `.env.example` 清楚说明必需配置。
- 可以通过 Render Blueprint 进行云部署。
- 可以通过 `DATABASE_PATH` 适配本地数据库和云端持久化磁盘。
- 可以避免 `.env`、数据库、缓存和日志进入镜像。

## 10. LangGraph 状态图编排

### 问题

早期 Agent 主流程集中在一个较长的函数里，虽然能跑通业务，但存在几个工程问题：

- 路由、工具执行、上下文构造、回复生成和持久化混在一起，后续插入新节点不够清晰。
- 中间变量分散在函数局部变量里，不利于 tracing 和前端执行轨迹展示。
- 如果后续要扩展 Planner / Executor、人工审核节点、失败重试节点或条件分支，线性函数会越来越难维护。

### 优化

引入 LangGraph，把 Agent 主链路改造成 `StateGraph`：

```text
load_context
-> route
-> execute_tools
-> build_model_context
-> generate_reply
-> persist_result
```

新增 `AgentWorkflowState` 作为共享状态，统一承载：

```text
user_message
conversation_id
history
pending_task
slots / missing_slots
route
tool_results
model_messages
reply
trace
result
```

每个节点只负责一件事：

- `load_context`：加载历史消息和 pending task，生成有效用户请求。
- `route`：执行 Router，并结合槽位要求决定是否追问。
- `execute_tools`：按 route 调用订单查询、RAG 检索和工单创建。
- `build_model_context`：把工具结果整理成模型上下文。
- `generate_reply`：调用真实 LLM 或 fallback 回复。
- `persist_result`：保存会话、trace，并组装 API 返回结果。

### 结果

重构后业务行为保持一致，回归结果：

```text
compileall: 通过
Agent eval: 16/16
RAG eval: 8/8
Answer eval: 16/16
multi_turn_smoke_test: 通过
api_smoke_test: 通过
```

### 价值

这一步把 Agent 从“函数串联”升级成“状态图编排”：

- 共享 state 让中间状态更集中，便于调试和展示。
- 节点边界更清晰，方便后续插入条件边和人工审核节点。
- 面试中可以讲清楚 LangGraph 的核心价值：不是替代业务逻辑，而是让复杂 Agent 流程更可维护、更可观测、更容易扩展。

## 11. 节点级 SSE 流式输出

### 问题

早期前端虽然使用 SSE 展示流式回复，但后端实现是先完整执行 Agent：

```text
run_customer_support_agent()
-> 完成路由、工具调用、模型回复
-> 再把结果拆成 token 发给前端
```

当开启真实智谱模型时，用户需要等待模型完整返回后才能看到页面变化，体验上容易误以为系统卡住。

### 优化

基于 LangGraph `stream(..., stream_mode="updates")` 改造 `/agent/stream`：

```text
route 节点完成 -> 立即推送路由判断
execute_tools 节点完成 -> 立即推送订单查询、RAG、工单结果
generate_reply 节点完成 -> 推送最终客服回复
persist_result 节点完成 -> 推送 done 事件
```

前端不需要改变协议，仍然消费 SSE 事件：

```text
route
tool_result
token / message
done
```

### 结果

验证结果：

```text
compileall: 通过
api_smoke_test: 通过
Agent eval: 16/16
RAG eval: 8/8
Answer eval: 16/16
```

### 价值

- 用户可以更早看到 Agent 的路由和工具执行进度。
- 面试展示时能更清晰体现 Agent 的执行链路。
- 后续如果接入真正的 LLM token streaming，只需要继续改造 `generate_reply` 节点，不需要推翻整体 SSE 协议。

## 12. 高风险请求强制规则兜底

### 问题

线上演示时发现，当用户输入“我想直接退款并绕过审核”并开启真实智谱模型时，Router 能识别这是高风险请求，但后续仍然进入 `generate_reply` 节点调用大模型。由于这类请求没有订单号，也没有可用的工具证据，模型可能生成泛化回复，甚至给出不该承诺的处理步骤。

这个问题本质不是模型能力问题，而是 Agent 控制流边界不够清楚：安全拦截、信息缺失和高风险人工审核，应该优先于 LLM 生成。

### 优化

新增 `should_force_fallback(route)`，将以下情况强制切到确定性回复：

```text
blocked_by_guardrail = true
need_clarification = true
handoff_required = true 且没有 order_id
```

同时把退款、退货、赔付、换货加入“必须提供订单号”的关键词范围。这样用户要求退款但没有订单号时，系统会同时做到两件事：

- 追问订单号，避免无订单上下文处理。
- 明确提示需要人工审核，不能绕过审核或自动退款。

流式接口也补充了 `error` SSE 事件。当真实 LLM 或网络调用失败时，前端可以展示明确错误，而不是只有 `TypeError: network error`。

### 结果

新增高风险评估样例：

```text
我想直接退款并绕过审核
```

当前回归结果：

```text
compileall: 通过
Agent eval: 17/17
RAG eval: 8/8
Answer eval: 17/17
multi_turn_smoke_test: 通过
api_smoke_test: 通过
```

### 价值

- 降低高风险业务场景下的大模型幻觉和越权承诺风险。
- 将“能不能调用 LLM”从前端开关，升级为后端根据业务风险统一决策。
- 面试中可以表达为：我没有把安全完全交给 prompt，而是在工作流节点中做了确定性控制，保证高风险路径优先走规则兜底和人工审核。

## 13. 真实 LLM Token 级流式输出

### 问题

早期 `/agent/stream` 只能做到节点级流式：页面可以先看到路由和工具结果，但最终回答仍然要等 `call_zhipu_chat()` 完整返回后，再把整段文本拆成字符推给前端。

这会造成一个体验问题：当真实大模型响应较慢时，用户虽然看到了前面的执行轨迹，但最终回复区域仍会空等几秒到十几秒。

### 优化

新增 `call_zhipu_chat_stream()`：

```text
请求智谱 Chat Completions
-> 设置 stream=true
-> 读取 SSE data 行
-> 解析 choices[].delta.content
-> 逐段 yield 给 /agent/stream
```

同时调整 `/agent/stream`：

```text
load_context
-> route，推送 route 事件
-> execute_tools，推送 tool_result 事件
-> build_model_context
-> 如果可调用真实 LLM，则推送 status 事件，再转发智谱 token
-> persist_result，保存会话和 trace
-> 推送 done 事件
```

前端新增 `status` 事件处理：在模型首个 token 返回前显示“正在调用智谱大模型生成客服回复...”，首个 token 到达后自动替换成真实回复。

### 结果

本地验证真实模型流式返回：

```text
HTTP 200
route event: 1
token event: 66
```

完整回归：

```text
compileall: 通过
Agent eval: 17/17
RAG eval: 8/8
Answer eval: 17/17
multi_turn_smoke_test: 通过
api_smoke_test: 通过
```

### 价值

- 用户不必等待完整回复生成，可以更早看到模型输出。
- 路由、工具、模型 token 都能在一个 SSE 通道里展示，演示效果更接近真实客服工作台。
- 面试中可以说明：我区分了“节点级流式”和“模型原生 token 流式”，并把两者组合到同一个 Agent 执行链路中。

## 14. Agent 性能 Trace 与耗时拆解

### 问题

当线上页面出现“回答慢”时，单看用户体验无法判断慢点在哪里：

```text
可能是 Render 冷启动
可能是 Router 或槽位补全
可能是 RAG 检索
可能是订单 / 工单数据库操作
可能是智谱 Chat 生成
```

如果没有耗时拆解，只能凭感觉猜，无法支撑后续优化。

### 优化

在 `tracing.py` 中新增 `timings` 字段和 `add_trace_timing()`：

```text
trace
├─ events: 发生了什么
└─ timings: 每一步花了多久
```

在 Agent 主链路记录节点耗时：

```text
node.load_context
node.route
node.execute_tools
node.build_model_context
node.generate_reply
node.persist_result
```

在工具层记录细分耗时：

```text
tool.order_lookup
tool.policy_search
tool.create_ticket
```

同时修复 `duration_ms` 计算问题：原来字段名是毫秒，但实际记录的是秒级整数；现在统一使用真实毫秒值。

前端新增 `timing` 事件展示，并在 `done` 事件中展示总耗时。`scripts/analyze_traces.py` 也增加了阶段平均耗时和最大耗时统计。

### 结果

本地流式接口验证：

```text
HTTP 200
timing_count: 9
```

历史 trace 分析报告可以输出：

```text
平均耗时
路由触发次数
工具调用次数
回复模式
各阶段平均耗时和最大耗时
```

完整回归：

```text
compileall: 通过
Agent eval: 17/17
RAG eval: 8/8
Answer eval: 17/17
multi_turn_smoke_test: 通过
api_smoke_test: 通过
```

### 价值

- 用户反馈慢时，可以用 trace 判断慢在 RAG、LLM、数据库还是平台冷启动。
- 前端演示能展示 Agent 不只是“会回答”，还知道自己每一步做了什么、花了多久。
- 面试中可以表达为：我为 Agent 建设了基础可观测性，把执行链路、工具结果和阶段耗时统一记录到 trace，并提供分析脚本支持慢请求定位。

## 15. 业务可读的 Agent 执行轨迹

### 问题

早期前端虽然能展示路由和工具结果，但默认内容主要是原始 JSON：

```text
工具结果：policy_search
{
  "tool_name": "policy_search",
  "success": true,
  "result": [...]
}
```

这种形式方便开发调试，但面试演示或业务人员体验时不够直观。用户需要自己读字段，才能理解 Agent 为什么要查订单、为什么要检索政策、为什么要创建工单。

### 优化

在前端增加业务摘要转换函数：

```text
buildRouteSummary(route)
buildOrderSummary(toolResult)
buildPolicySummary(toolResult)
buildTicketSummary(toolResult)
buildToolSummary(toolResult)
```

展示效果变为：

```text
路由判断
- 订单号：10002
- 需要订单查询：是
- 需要政策检索：是
- 需要创建工单：是

订单查询
- 订单状态：已发货
- 物流状态：2026-08-05 09:12 快件离开发货仓

政策检索
- 命中 2 条政策证据
- 物流配送政策.md - 物流查询

创建工单
- 工单状态：pending_human_review
- 下一步：请人工客服核对订单、凭证和售后政策后再处理
```

原始 JSON 没有删除，仍然保留在同一个折叠面板下方，方便开发者继续排查细节。

### 结果

本地浏览器验证：

```text
路由判断：显示业务摘要
订单查询：显示订单状态和物流状态
政策检索：显示命中的 citation 和相关分数
工单创建：显示工单状态、问题类型和下一步
聊天区不再显示耗时面板，耗时只保留在左侧性能摘要
console error: 0
```

### 价值

- 提升项目演示效果，让面试官能直接看懂 Agent 的执行链路。
- 区分“业务解释”和“调试数据”：摘要给人看，JSON 给开发调试。
- 面试中可以表达为：我没有简单把工具返回值打印到页面，而是把 Router 和 Tool Result 转换成业务可读的执行轨迹，展示 Agent 每一步的判断、证据和动作。

## 16. 售后任务订单优先策略

### 问题

线上测试发现，用户只输入：

```text
我的手表坏了
```

没有提供订单号、购买时间、商品型号和签收信息时，Agent 仍然调用真实大模型生成回复，并出现“仍在保修期内”这类判断。

这个问题的根因是：Router 没有把“商品故障”识别为必须依赖订单上下文的售后任务，导致请求绕过了追问订单号的规则，进入了 LLM 生成阶段。

### 优化

收紧售后业务的前置条件：只要是需要具体订单支撑的售后任务，第一步必须先拿到订单号。

新增或补充的订单号必需场景包括：

```text
保修 / 维修 / 检测
坏了 / 故障 / 质量问题
换货 / 换新
退款 / 退货 / 赔付
物流 / 发货 / 签收
发票 / 投诉 / 缺货 / 补货
```

调整后：

```text
用户：我的手表坏了
Agent：请您提供订单号，我才能继续查询订单状态并判断售后方案。
```

这类请求会触发 `need_clarification=true`，不会调用订单、RAG、工单工具，也不会进入真实模型自由判断。

同时补充 Router 关键词，使用户提供订单号后，“坏了 / 故障 / 质量问题 / 换新”等场景能正确进入政策检索和保修检测工单链路。

### 结果

新增评估样例：

```text
我的手表坏了
```

当前回归结果：

```text
compileall: 通过
Agent eval: 21/21
RAG eval: 8/8
Answer eval: 21/21
api_smoke_test: 通过
```

### 价值

- 避免 Agent 在缺少订单信息时判断保修期、退款资格、物流状态等强依赖订单的数据。
- 把“先查订单，再谈政策和工单”固化为 Router 规则，而不是依赖 prompt 约束模型。
- 面试中可以表达为：我发现模型会在无订单上下文时做售后判断，于是把售后任务的前置槽位统一收紧为订单号，确保所有业务动作先绑定订单再执行。

## 17. 订单查询失败短路策略

### 问题

线上测试发现，用户输入不存在的订单号时：

```text
订单 10025 手表坏了，还在保修期内吗？
```

`order_lookup` 已经返回“未找到订单号”，但执行链仍继续调用 `policy_search` 和 `create_ticket`。最终模型看到了保修政策证据和工单结果，就生成了“已创建保修检测工单”的错误回复。

根因是：Router 只负责判断“理论上需要哪些工具”，但 Executor 没有把订单查询成功作为后续业务动作的前置条件。

### 优化

新增订单校验闸门：

```text
route_tools 判断需要订单查询
-> execute_tools 调用 order_lookup
-> 如果订单不存在，立即停止 policy_search 和 create_ticket
-> generate_reply 强制走规则兜底，不调用真实大模型
```

这次优化新增了两个辅助函数：

```text
get_order_lookup_result：从工具结果中取出订单查询结果
has_failed_order_lookup：判断订单查询是否失败
```

同时在 `fallback_answer` 中增加失败订单回复：

```text
未找到订单号 10025，请核对订单号是否正确。请您核对订单号后重新提供，我再继续查询售后政策并判断是否需要创建工单。
```

### 结果

新增评估样例：

```text
订单 10025 手表坏了，还在保修期内吗？
```

预期工具调用从原来的：

```text
order_lookup -> policy_search -> create_ticket
```

收敛为：

```text
order_lookup
```

当前回归结果：

```text
compileall: 通过
Agent eval: 19/19
RAG eval: 8/8
Answer eval: 19/19
api_smoke_test: 通过
```

### 价值

- 避免对不存在订单生成保修、退款、物流或投诉工单。
- 把“订单存在性校验”从 prompt 约束升级为代码层业务闸门。
- 面试中可以表达为：我将 Agent 的工具执行链改成订单优先的短路流程，Router 只给计划，Executor 负责业务前置校验；如果订单查不到，后续 RAG 和工单节点不会执行，从而避免模型根据无效上下文生成虚假处理结果。

## 18. 工单创建前业务资格判断

### 问题

上一轮解决了“订单不存在不能继续处理”的问题，但系统仍有一个隐患：只要 Router 判断 `need_ticket=true`，Executor 就会直接调用 `create_ticket`。

这会导致两个业务风险：

```text
订单 10011 物流刚刚更新，但用户觉得太慢了
-> 不应该立刻创建物流异常工单，因为未超过 48 小时

订单 10002 商品还在运输中，用户说手环坏了
-> 不应该创建保修检测工单，因为订单尚未签收，保修期还不能判断
```

根因是：早期系统把“用户有工单意图”和“业务上允许创建工单”混在一起了。

### 优化

新增 `app/agent/ticket_policy.py`，在创建工单前增加业务资格判断：

```text
route_tools：判断用户是否可能需要工单
order_lookup：查询订单真实状态
policy_search：检索相关政策证据
evaluate_ticket_creation：结合订单状态判断是否允许建工单
create_ticket：只有判断通过才创建工单草稿
```

关键规则包括：

```text
保修检测：
- 未签收：不创建保修检测工单
- 无保修月数：不创建保修检测工单
- 超过保修期：不直接创建，提示转人工确认
- 已签收且在保修期内：允许创建工单草稿

物流异常：
- 已签收：不创建物流异常工单
- 物流更新未超过 48 小时：不创建物流异常工单
- 长时间未更新：允许创建物流异常工单草稿

高风险动作：
- 退款、退货、投诉、地址修改等仍然进入待人工审核工单，不直接执行业务动作
```

当前如果不满足创建条件，会返回 `ticket_decision`：

```text
工具结果：ticket_decision
是否创建工单：否
原因：订单尚未签收，暂时不能创建保修检测工单
```

前端也新增了 `ticket_decision` 展示，让演示时能看到 Agent 为什么没有继续创建工单。

### 结果

新增评估样例：

```text
订单 10011 物流刚刚更新但我觉得太慢了，能创建物流异常工单吗？
订单 10002 手环坏了，还能创建保修检测工单吗？
```

当前回归结果：

```text
compileall: 通过
Agent eval: 21/21
RAG eval: 8/8
Answer eval: 21/21
api_smoke_test: 通过
```

### 价值

- 把“关键词触发工单”升级为“订单状态驱动的工单资格判断”。
- 降低误建工单、错误承诺售后处理的风险。
- 面试中可以表达为：我在工具执行链中增加了业务资格判断层，Router 只决定可能需要哪些工具，真正创建工单前会结合订单状态、签收状态、物流更新时间和保修期做二次校验，不满足条件时输出 `ticket_decision` 并给出原因。

## 面试表达摘要

可以将项目概括为：

```text
我实现了一个中文电商智能售后客服 Agent，后端基于 FastAPI，支持流式输出、订单查询、RAG 政策检索、工单草稿、多轮槽位补全、提示词注入防护和高风险转人工。

项目重点不是只做聊天回复，而是建立工程闭环。我使用 LangGraph StateGraph 编排 Agent 主链路，将加载上下文、路由判断、工具执行、上下文构造、回复生成和结果持久化拆成节点，并通过共享 state 传递 route、slots、tool_results、model_messages 等中间状态。我把评估拆成三层：Router 工具调用评估、RAG 检索评估、最终回答质量评估。通过 eval 和 tracing 定位问题，再针对 Router、RAG query、evidence context 和 pending task 做优化。

在 RAG 上，我做了文档切分、metadata/citation 保留、智谱 embedding 接入、向量相似度和业务关键词混合检索、query enrichment，以及 citation 检查。

在多轮对话上，我实现了 pending task 和多槽位补全。例如修改地址必须收集订单号和新地址，只有槽位补齐后才会查询订单、检索政策并创建待人工审核的地址修改工单。

在安全边界上，退款、赔付、取消订单、修改地址等高风险动作不会由 Agent 自动执行，只会解释政策或创建待审核工单。

数据层使用 SQLite 做本地持久化，订单、工单、会话、pending task 和 feedback 都有数据库表承载。这样项目不仅能演示 Agent 推理和工具调用，也能展示真实客服系统所需的数据闭环。

部署层补充了 Dockerfile、`.dockerignore`、`.env.example` 和 Render Blueprint 配置。Dockerfile 固定 Python 3.13 环境并用 uvicorn 启动服务；云端通过 `PORT` 适配平台端口，通过 `DATABASE_PATH` 把 SQLite 写入持久化磁盘路径，通过环境变量注入智谱 API Key，避免密钥写入代码或镜像。
```
