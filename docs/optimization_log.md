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
