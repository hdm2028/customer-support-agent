
      const state = {
        conversationId: localStorage.getItem("agent_conversation_id") || "",
        route: null,
        tools: [],
        timings: {},
        tokenUsage: null,
        dashboard: {
          refunds: [],
          tickets: [],
          reviews: [],
          mq: [],
          metrics: [],
        },
      };

      const els = {
        serviceStatus: document.querySelector("#serviceStatus"),
        modelName: document.querySelector("#modelName"),
        ragMode: document.querySelector("#ragMode"),
        databaseBackend: document.querySelector("#databaseBackend"),
        cacheBackend: document.querySelector("#cacheBackend"),
        mqBackend: document.querySelector("#mqBackend"),
        llmKeyState: document.querySelector("#llmKeyState"),
        architectureList: document.querySelector("#architectureList"),
        agentStrip: document.querySelector("#agentStrip"),
        messages: document.querySelector("#messages"),
        emptyState: document.querySelector("#emptyState"),
        conversationId: document.querySelector("#conversationId"),
        routeIntent: document.querySelector("#routeIntent"),
        routeSummary: document.querySelector("#routeSummary"),
        toolCount: document.querySelector("#toolCount"),
        toolList: document.querySelector("#toolList"),
        businessObjects: document.querySelector("#businessObjects"),
        timingList: document.querySelector("#timingList"),
        metricsList: document.querySelector("#metricsList"),
        tracePanel: document.querySelector("#tracePanel"),
        chatForm: document.querySelector("#chatForm"),
        messageInput: document.querySelector("#messageInput"),
        sendBtn: document.querySelector("#sendBtn"),
        streamToggle: document.querySelector("#streamToggle"),
        traceToggle: document.querySelector("#traceToggle"),
        llmToggle: document.querySelector("#llmToggle"),
        newChatBtn: document.querySelector("#newChatBtn"),
      };

      const AGENTS = [
        { name: "Orchestrator", role: "路由", tools: "状态管理 / 汇总" },
        { name: "客服 Agent", role: "问答", tools: "Hybrid RAG" },
        { name: "售后 Agent", role: "业务", tools: "订单 / 退款 / 工单" },
        { name: "风控 Agent", role: "审核", tools: "风险规则 / 人工审核" },
      ];

      const ARCHITECTURE = [
        ["FastAPI", "统一 API 与 SSE 流式响应"],
        ["Agent Orchestrator", "请求路由、状态管理、结果汇总"],
        ["客服 Agent", "意图识别、知识库检索、回复生成"],
        ["售后 Agent", "订单查询、退款申请、工单创建"],
        ["风控 Agent", "高频退款、异常账号、投诉升级"],
        ["Hybrid RAG", "向量召回 + BM25/关键词召回 + 业务重排"],
        ["MySQL / SQLite", "订单、退款、工单、会话、执行记录"],
        ["Redis", "会话、Embedding、Agent 状态、退款幂等"],
        ["MQ", "退款任务异步处理"],
      ];

      const TOOL_LABELS = {
        order_lookup: "订单查询",
        policy_search: "知识检索",
        risk_check: "风控检测",
        refund_apply: "退款申请",
        create_manual_review: "人工审核",
        create_ticket: "工单创建",
        transfer_to_human: "转人工",
        ticket_decision: "工单判断",
        tool_plan_validation: "计划校验",
        tool_chain_validation: "链路校验",
      };

      function escapeHtml(value) {
        return String(value ?? "")
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#039;");
      }

      function formatBool(value) {
        return value ? "是" : "否";
      }

      function setPill(el, text, type = "") {
        el.textContent = text;
        el.className = `pill ${type}`.trim();
      }

      function updateConversationId(id) {
        if (!id) return;
        state.conversationId = id;
        localStorage.setItem("agent_conversation_id", id);
        els.conversationId.textContent = id;
      }

      function renderArchitecture(activeAgents = []) {
        const active = new Set(["FastAPI", "Agent Orchestrator", ...activeAgents]);
        els.architectureList.innerHTML = ARCHITECTURE.map(([name, sub]) => `
          <div class="arch-node ${active.has(name) ? "active" : ""}">
            <span class="arch-dot"></span>
            <div>
              <div class="arch-name">${escapeHtml(name)}</div>
              <div class="arch-sub">${escapeHtml(sub)}</div>
            </div>
          </div>
        `).join("");
      }

      function renderAgents(activeAgents = [], route = null) {
        const active = new Set(["Orchestrator", ...activeAgents]);
        const toolPlan = route?.tool_plan || [];
        els.agentStrip.innerHTML = AGENTS.map((agent) => {
          const tools = agent.name === "Orchestrator" ? toolPlan.join(" / ") || agent.tools : agent.tools;
          return `
            <div class="agent-card ${active.has(agent.name) ? "active" : ""}">
              <div class="agent-role">${escapeHtml(agent.role)}</div>
              <div class="agent-name">${escapeHtml(agent.name)}</div>
              <div class="agent-tools">${escapeHtml(tools)}</div>
            </div>
          `;
        }).join("");
      }

      function resetCurrentRun() {
        state.route = null;
        state.tools = [];
        state.timings = {};
        state.tokenUsage = null;
        setPill(els.routeIntent, "路由中", "warn");
        setPill(els.toolCount, "0");
        els.routeSummary.innerHTML = `<div class="soft-note">等待路由结果。</div>`;
        els.toolList.innerHTML = `<div class="soft-note">暂无工具调用。</div>`;
        els.businessObjects.innerHTML = `<div class="soft-note">暂无业务对象。</div>`;
        els.timingList.innerHTML = `<div class="soft-note">暂无耗时数据。</div>`;
        els.metricsList.innerHTML = `<div class="soft-note">暂无指标。</div>`;
        renderAgents();
        renderArchitecture();
      }

      function addMessage(role, content = "") {
        els.emptyState?.classList.add("hidden");
        const wrapper = document.createElement("div");
        wrapper.className = `message ${role}`;
        wrapper.innerHTML = `
          <div class="speaker">${role === "user" ? "用户" : "Agent"}</div>
          <div class="bubble"></div>
        `;
        const bubble = wrapper.querySelector(".bubble");
        bubble.textContent = content;
        els.messages.appendChild(wrapper);
        els.messages.scrollTop = els.messages.scrollHeight;
        return { wrapper, bubble };
      }

      function addRating(wrapper) {
        const rating = document.createElement("div");
        rating.className = "rating";
        rating.innerHTML = `
          <span>本轮评分</span>
          ${[1, 2, 3, 4, 5].map((score) => `<button type="button" data-score="${score}">${score}</button>`).join("")}
        `;
        rating.addEventListener("click", async (event) => {
          const button = event.target.closest("button[data-score]");
          if (!button || !state.conversationId) return;
          await apiFetch("/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              conversation_id: state.conversationId,
              score: Number(button.dataset.score),
            }),
          });
          rating.innerHTML = `<span>已评分 ${button.dataset.score}</span>`;
        });
        wrapper.appendChild(rating);
      }

      function renderRoute(route) {
        if (!route) return;
        const riskType = route.risk_level === "high" ? "danger" : route.risk_level === "medium" ? "warn" : "ok";
        setPill(els.routeIntent, route.intent || "general_support", riskType);
        const rows = [
          ["订单号", route.order_id || "-"],
          ["置信度", route.confidence ?? "-"],
          ["需订单", formatBool(route.need_order)],
          ["需 RAG", formatBool(route.need_policy)],
          ["需退款", formatBool(route.need_refund_request)],
          ["需风控", formatBool(route.need_risk_check)],
          ["需工单", formatBool(route.need_ticket)],
          ["人工审核", formatBool(route.manual_review_required || route.handoff_required)],
          ["风险等级", route.risk_level || "low"],
        ];
        els.routeSummary.innerHTML = rows.map(([label, value]) => `
          <div class="data-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
        `).join("");
        renderAgents(route.agent_plan || [], route);
        renderArchitecture(route.agent_plan || []);
      }

      function summarizeTool(tool) {
        const result = tool.result;
        if (tool.tool_name === "order_lookup" && tool.success && result) {
          return `订单 ${result.order_id}，状态：${result.order_status}，物流：${result.shipping_status || "-"}`;
        }
        if (tool.tool_name === "policy_search" && tool.success && Array.isArray(result)) {
          return result.slice(0, 2).map((item) => item.citation || item.source).join("；");
        }
        if (tool.tool_name === "risk_check" && tool.success && result) {
          return `风险等级：${result.risk_level}，原因：${(result.risk_flags || []).join("、") || result.review_reason}`;
        }
        if (tool.tool_name === "refund_apply" && result) {
          return tool.success
            ? `退款单 ${result.refund_id}，状态：${result.status}，MQ：${result.mq_message_id || "-"}`
            : result.reason || JSON.stringify(result);
        }
        if (tool.tool_name === "create_ticket" && tool.success && result) {
          return `工单 ${result.ticket_id}，类型：${result.issue_type}，状态：${result.status}`;
        }
        if (tool.tool_name === "create_manual_review" && tool.success && result) {
          return `审核单 ${result.review_id}，风险：${result.risk_level}，状态：${result.status}`;
        }
        if (tool.tool_name === "transfer_to_human" && tool.success && result) {
          return result.handoff_summary || result.reason;
        }
        if (tool.tool_name === "ticket_decision" && result) {
          return result.reason || JSON.stringify(result);
        }
        return typeof result === "string" ? result : JSON.stringify(result);
      }

      function renderTools() {
        setPill(els.toolCount, String(state.tools.length), state.tools.some((item) => !item.success) ? "warn" : "ok");
        if (!state.tools.length) {
          els.toolList.innerHTML = `<div class="soft-note">暂无工具调用。</div>`;
          return;
        }
        els.toolList.innerHTML = state.tools.map((tool) => `
          <div class="tool-item">
            <div class="tool-head">
              <span class="tool-name">${escapeHtml(TOOL_LABELS[tool.tool_name] || tool.tool_name)}</span>
              <span class="pill ${tool.success ? "ok" : "danger"}">${tool.success ? "成功" : "失败"}</span>
            </div>
            <div class="tool-summary">${escapeHtml(summarizeTool(tool))}</div>
          </div>
        `).join("");
      }

      function latestTool(name) {
        return [...state.tools].reverse().find((item) => item.tool_name === name && item.success);
      }

      function renderBusinessObjects() {
        const rows = [];
        const order = latestTool("order_lookup")?.result;
        const refund = latestTool("refund_apply")?.result;
        const ticket = latestTool("create_ticket")?.result;
        const review = latestTool("create_manual_review")?.result;

        if (order) {
          rows.push(["订单", `${order.order_id} · ${order.order_status}`]);
        }
        if (refund) {
          rows.push(["退款", `${refund.refund_id} · ${refund.status}`]);
          rows.push(["MQ", refund.mq_message_id || "-"]);
        }
        if (ticket) {
          rows.push(["工单", `${ticket.ticket_id} · ${ticket.issue_type}`]);
        }
        if (review) {
          rows.push(["审核", `${review.review_id} · ${review.risk_level}`]);
        }

        if (!rows.length) {
          els.businessObjects.innerHTML = `<div class="soft-note">暂无业务对象。</div>`;
          return;
        }

        els.businessObjects.innerHTML = rows.map(([label, value]) => `
          <div class="data-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
        `).join("");
      }

      function renderTimings() {
        const items = Object.values(state.timings);
        if (!items.length) {
          els.timingList.innerHTML = `<div class="soft-note">暂无耗时数据。</div>`;
          return;
        }
        const max = Math.max(...items.map((item) => Number(item.duration_ms || 0)), 1);
        els.timingList.innerHTML = items.map((item) => {
          const width = Math.max(4, Math.min(100, Number(item.duration_ms || 0) / max * 100));
          const label = String(item.step || "").replace("node.", "").replace("tool.", "");
          return `
            <div class="trace-item">
              <span class="trace-step">${escapeHtml(label)}</span>
              <div title="${escapeHtml(item.duration_ms)} ms">
                <div class="trace-value"><div class="trace-bar" style="width:${width}%"></div></div>
              </div>
            </div>
          `;
        }).join("");
      }

      function renderMetrics(doneContent = null) {
        if (doneContent?.token_usage) {
          state.tokenUsage = doneContent.token_usage;
        }
        const token = state.tokenUsage || {};
        const latestMetric = state.dashboard.metrics[0] || {};
        const rows = [
          ["上下文 token（估算）", token.prompt_tokens_estimated ?? "-"],
          ["回复 token（估算）", token.completion_tokens_estimated ?? "-"],
          ["总 token（估算）", token.total_tokens_estimated ?? "-"],
          ["模型返回 token", token.provider_reported?.tokens?.total_tokens ?? "未返回"],
          ["用量报告完整性", token.provider_reported ? `${token.provider_reported.reported_calls}/${token.provider_reported.call_count} 次调用` : "未知"],
          ["本轮耗时", doneContent?.duration_ms ? `${doneContent.duration_ms}ms` : latestMetric.duration_ms ? `${latestMetric.duration_ms}ms` : "-"],
          ["退款记录", state.dashboard.refunds.length],
          ["MQ 消息", state.dashboard.mq.length],
          ["人工审核", state.dashboard.reviews.length],
        ];
        els.metricsList.innerHTML = rows.map(([label, value]) => `
          <div class="metric-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
        `).join("");
      }

      function handleEvent(event, assistantBubble) {
        if (!event || event.type === "status") return;
        if (event.conversation_id) updateConversationId(event.conversation_id);

        if (event.type === "route") {
          state.route = event.content;
          renderRoute(state.route);
          return;
        }

        if (event.type === "tool_result") {
          state.tools.push(event.content);
          renderTools();
          renderBusinessObjects();
          return;
        }

        if (event.type === "timing") {
          state.timings[event.content.step] = event.content;
          renderTimings();
          return;
        }

        if (event.type === "token") {
          assistantBubble.textContent += event.content;
          els.messages.scrollTop = els.messages.scrollHeight;
          return;
        }

        if (event.type === "message") {
          assistantBubble.textContent = event.content;
          els.messages.scrollTop = els.messages.scrollHeight;
          return;
        }

        if (event.type === "done") {
          if (event.content?.reply) {
            assistantBubble.textContent = event.content.reply;
          }
          if (event.content?.route) {
            state.route = event.content.route;
            renderRoute(state.route);
          }
          if (Array.isArray(event.content?.tool_results)) {
            state.tools = event.content.tool_results;
            renderTools();
            renderBusinessObjects();
          }
          if (event.content?.timings) {
            state.timings = event.content.timings;
            renderTimings();
          }
          renderMetrics(event.content);
          refreshDashboard();
        }

        if (event.type === "error") {
          assistantBubble.textContent = `请求失败：${event.content}`;
          setPill(els.routeIntent, "错误", "danger");
        }
      }

      async function sendByStream(message, assistantBubble) {
        const response = await apiFetch("/agent/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            conversation_id: state.conversationId || null,
            use_llm: els.llmToggle.checked,
            stream_tokens: true,
          }),
        });

        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() || "";

          for (const chunk of chunks) {
            const line = chunk.split("\n").find((item) => item.startsWith("data: "));
            if (!line) continue;
            const raw = line.slice(6);
            if (raw === "[DONE]") continue;
            handleEvent(JSON.parse(raw), assistantBubble);
          }
        }
      }

      async function sendByJson(message, assistantBubble) {
        const response = await apiFetch("/agent/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            conversation_id: state.conversationId || null,
            use_llm: els.llmToggle.checked,
          }),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.detail || `HTTP ${response.status}`);
        }
        handleEvent({ type: "done", content: result, conversation_id: result.conversation_id }, assistantBubble);
      }

      async function submitMessage(message) {
        const text = message.trim();
        if (!text) return;
        resetCurrentRun();
        addMessage("user", text);
        const assistant = addMessage("agent", "正在处理...");
        els.messageInput.value = "";
        els.sendBtn.disabled = true;

        try {
          if (els.streamToggle.checked) {
            await sendByStream(text, assistant.bubble);
          } else {
            await sendByJson(text, assistant.bubble);
          }
          addRating(assistant.wrapper);
        } catch (error) {
          assistant.bubble.textContent = `请求失败：${error.message}`;
          setPill(els.routeIntent, "错误", "danger");
        } finally {
          els.sendBtn.disabled = false;
          els.messageInput.focus();
        }
      }

      let currentRole = "customer";
      let currentUserId = "";
      let selectedTicket = null;
      let ticketCursor = null;
      let nextTicketCursor = null;
      let ticketPreviousCursors = [];
      let dashboardRequest = 0;
      let availableOperators = [];
      let identityEpoch = 0;
      let selectedReview = null;
      let pendingSupplement = null;

      document.querySelector("#submitSupplement").addEventListener("click", async () => {
        const epoch = identityEpoch;
        const reviewId = document.querySelector("#supplementReviewId").value.trim();
        const text = document.querySelector("#supplementText").value.trim();
        const status = document.querySelector("#supplementStatus");
        if (!reviewId || !text) { status.textContent = "请填写审核单号和补充说明。"; return; }
        if (!pendingSupplement || pendingSupplement.review_id !== reviewId || pendingSupplement.text !== text) {
          pendingSupplement = {review_id: reviewId, text, submission_id: crypto.randomUUID()};
        }
        const body = {...pendingSupplement};
        document.querySelector("#submitSupplement").disabled = true;
        try {
          const result = await apiFetch("/review-supplements", {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
          }).then(r => r.json());
          if (epoch !== identityEpoch || document.querySelector("#supplementReviewId").value.trim() !== reviewId ||
              document.querySelector("#supplementText").value.trim() !== text) return;
          status.textContent = result.review_status === "pending_review" ? "补充说明已保存，等待人工查看。" :
            "补充说明已保存为审核后的记录，原决定未改变；如需继续处理，请联系人工客服。";
          pendingSupplement = null;
          document.querySelector("#supplementText").value = "";
          if (selectedReview?.review_id === reviewId) await loadSelectedReview();
        } catch (error) {
          if (epoch === identityEpoch) status.textContent = error.message;
        } finally {
          if (epoch === identityEpoch) document.querySelector("#submitSupplement").disabled = false;
        }
      });

      function reviewSummary(review) {
        const context = review.context || {};
        const order = context.order_snapshot || {};
        const evidence = (context.policy_evidence || []).flat();
        return [
          `审核单：${review.review_id}　状态：${review.status}`,
          `订单：${review.order_id || "未关联"}　风险：${review.risk_level}`,
          `诉求：${review.user_request}`,
          `风险原因：${(review.risk_flags || []).join("、") || "无"}`,
          order.product_name ? `商品：${order.product_name}　金额：${order.amount}　订单状态：${order.order_status}` : "",
          "\n最近对话：",
          ...(context.history || []).map(m => `${m.role === "user" ? "用户" : "客服"}：${m.content}`),
          "\n政策依据：",
          ...(evidence.length ? evidence.map(e => `${e.citation || e.source || "来源未标记"}\n${e.text || e.content || "正文未保存"}`) : ["未保存政策证据，请补充核实后再决定。"]),
          "\n后续补充：",
          ...((review.supplements || []).length ? review.supplements.flatMap(s => [
            `${s.submitted_at} · ${s.submitted_by || "申请人"} · ${s.review_status_at_submission === "pending_review" ? "审核中补充" : "决定作出后补充，不属于原决定依据"}`,
            s.text,
            ...(s.context?.history || []).map(m => `${m.role === "user" ? "用户" : "客服"}：${m.content}`),
            ...(s.context?.policy_evidence || []).flat().map(e => `${e.citation || e.source || "来源未标记"}\n${e.text || e.content || "正文未保存"}`)
          ]) : ["暂无补充材料。"]),
          review.resolution ? `\n处理记录：${review.resolution.operator_id} · ${review.resolution.at}\n${review.resolution.note}` : "",
          `\n下一步：${review.next_step || "等待审核"}`,
        ].filter(Boolean).join("\n");
      }

      function refundSummary(refund) {
        const observation = refund.last_payment_observation || {};
        const labels = {queued: "申请已受理，等待处理", pending_manual_review: "等待人工审核", refund_processing: "处理中，尚未完成退款",
          payment_submitting: "已提交支付处理", refund_unknown: "渠道结果待核实", refund_succeeded: "渠道已确认成功",
          failed: "退款处理失败", rejected: "审核未通过", cancelled: "申请已撤销", canceled: "申请已撤销"};
        return [`退款申请：${refund.refund_id}`, `状态：${labels[refund.status] || "待核实"}`, `金额：${refund.amount} ${refund.payment_currency || "CNY"}`,
          `订单：${refund.order_id}`, `渠道凭证：${observation.provider_reference || "尚未确认"}`,
          `核对时间：${observation.observed_at || "尚未查询"}`, `说明：${observation.reason || refund.cancellation?.note || "以实际处理状态为准"}`].join("\n");
      }

      let selectedRefund = null;
      async function loadSelectedRefund() {
        const epoch = identityEpoch;
        const id = document.querySelector("#refundSelect").value;
        selectedRefund = null;
        document.querySelector("#cancelRefund").disabled = true;
        if (!id) { document.querySelector("#refundDetails").textContent = "请选择退款申请。"; return; }
        try {
          const refund = (await apiFetch(`/refunds/${encodeURIComponent(id)}`).then(r => r.json())).data;
          if (epoch !== identityEpoch || document.querySelector("#refundSelect").value !== id) return;
          selectedRefund = refund;
          document.querySelector("#refundDetails").textContent = refundSummary(selectedRefund);
          document.querySelector("#cancelRefund").disabled = !["queued", "pending_manual_review", "refund_processing"].includes(selectedRefund.status)
            || selectedRefund.reason === "approved_order_cancellation";
        } catch (error) {
          if (epoch === identityEpoch && document.querySelector("#refundSelect").value === id)
            document.querySelector("#refundActionStatus").textContent = error.message;
        }
      }
      function renderRefundChoices() {
        const select = document.querySelector("#refundSelect");
        const previous = select.value;
        select.replaceChildren(new Option("请选择退款申请", ""));
        for (const refund of state.dashboard.refunds) select.add(new Option(`${refund.refund_id} · 订单 ${refund.order_id}`, refund.refund_id));
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
      }
      document.querySelector("#refundSelect").addEventListener("change", () => {
        document.querySelector("#refundCancelNote").value = "";
        document.querySelector("#refundActionStatus").textContent = "";
        loadSelectedRefund();
      });
      document.querySelector("#cancelRefund").addEventListener("click", async () => {
        if (!selectedRefund) return;
        const epoch = identityEpoch;
        const id = selectedRefund.refund_id;
        const isCurrent = () => epoch === identityEpoch && document.querySelector("#refundSelect").value === id;
        const note = document.querySelector("#refundCancelNote").value.trim();
        if (!note) { document.querySelector("#refundActionStatus").textContent = "请填写撤销原因。"; return; }
        document.querySelector("#cancelRefund").disabled = true;
        try {
          await apiFetch(`/refunds/${encodeURIComponent(id)}/cancel`, {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({note})
          });
          if (!isCurrent()) return;
          document.querySelector("#refundActionStatus").textContent = "退款申请已撤销。";
          await refreshDashboard(); await loadSelectedRefund();
        } catch (error) {
          if (!isCurrent()) return;
          document.querySelector("#refundActionStatus").textContent = error.message; await loadSelectedRefund();
        }
      });

      function renderTicketChoices() {
        document.querySelector("#ticketManagement").classList.toggle("hidden", currentRole !== "admin");
        const assignees = document.querySelector("#ticketAssignee");
        const oldAssignee = assignees.value;
        assignees.replaceChildren(new Option("请选择新的处理人", ""));
        for (const operator of availableOperators) assignees.add(new Option(operator, operator));
        if ([...assignees.options].some(o => o.value === oldAssignee)) assignees.value = oldAssignee;
        const select = document.querySelector("#ticketSelect");
        const previous = select.value;
        select.replaceChildren(new Option("请选择工单", ""));
        for (const ticket of state.dashboard.tickets) {
          select.add(new Option(`${ticket.ticket_id} · ${ticket.status} · ${ticket.issue_type}`, ticket.ticket_id));
        }
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
        if (!select.value) {
          selectedTicket = null;
          document.querySelector("#ticketDetails").textContent = "请选择工单。";
          document.querySelector("#claimTicket").disabled = true;
          document.querySelector("#resolveTicket").disabled = true;
          document.querySelector("#reassignTicket").disabled = true;
        }
        document.querySelector("#previousTickets").disabled = ticketPreviousCursors.length === 0;
        document.querySelector("#nextTickets").disabled = !nextTicketCursor;
        document.querySelector("#ticketPageStatus").textContent = `第 ${ticketPreviousCursors.length + 1} 页，本页 ${state.dashboard.tickets.length} 条`;
      }

      function resetTicketPage() {
        ticketCursor = null; nextTicketCursor = null; ticketPreviousCursors = [];
      }
      for (const [id, event] of [["ticketFilter", "change"], ["refreshTickets", "click"]]) {
        document.querySelector(`#${id}`).addEventListener(event, () => { resetTicketPage(); refreshDashboard(); });
      }
      document.querySelector("#nextTickets").addEventListener("click", () => {
        if (!nextTicketCursor) return;
        ticketPreviousCursors.push(ticketCursor); ticketCursor = nextTicketCursor;
        refreshDashboard();
      });
      document.querySelector("#previousTickets").addEventListener("click", () => {
        if (!ticketPreviousCursors.length) return;
        ticketCursor = ticketPreviousCursors.pop(); refreshDashboard();
      });

      async function loadSelectedTicket() {
        const epoch = identityEpoch;
        const id = document.querySelector("#ticketSelect").value;
        selectedTicket = null;
        document.querySelector("#claimTicket").disabled = true;
        document.querySelector("#resolveTicket").disabled = true;
        document.querySelector("#reassignTicket").disabled = true;
        document.querySelector("#ticketDetails").textContent = id ? "正在读取工单…" : "请选择工单。";
        if (!id) return;
        const isCurrent = () => epoch === identityEpoch && document.querySelector("#ticketSelect").value === id;
        try {
          const ticket = (await apiFetch(`/tickets/${encodeURIComponent(id)}`).then(r => r.json())).data;
          if (!isCurrent()) return;
          selectedTicket = ticket;
          const statuses = {pending_human_takeover: "等待人工接管", pending_human_review: "待人工处理", pending_manual_review: "待人工跟进", in_progress: "已认领，处理中", resolved: "已结案"};
          const outcomes = {answered: "已核实并答复", rejected: "诉求不予受理", withdrawn: "用户撤回诉求"};
          const lines = [`工单：${id}`, `订单：${ticket.order_id || "未关联"}`, `诉求：${ticket.user_request}`,
            `状态：${statuses[ticket.status] || ticket.status}`, `处理人：${ticket.assigned_to || "待认领"}`, ticket.next_step || ""];
          if (ticket.resolution) lines.push(`结果：${outcomes[ticket.resolution.outcome] || ticket.resolution.outcome}`, `说明：${ticket.resolution.note}`);
          if (currentRole === "admin") {
            if (ticket.review_id) lines.push(`来源审核：${ticket.review_id}`, `批准说明：${ticket.approval_note || ""}`);
            if (ticket.context) lines.push("创建时上下文与证据：", JSON.stringify(ticket.context, null, 2));
            if (ticket.audit) lines.push("操作记录：", JSON.stringify(ticket.audit, null, 2));
          }
          document.querySelector("#ticketDetails").textContent = lines.join("\n");
          document.querySelector("#claimTicket").disabled = currentRole !== "admin" || !["pending_human_review", "pending_manual_review", "pending_human_takeover"].includes(ticket.status);
          document.querySelector("#resolveTicket").disabled = currentRole !== "admin" || ticket.status !== "in_progress" || ticket.assigned_to !== currentUserId;
          document.querySelector("#reassignTicket").disabled = currentRole !== "admin" || ticket.status !== "in_progress";
        } catch (error) { if (isCurrent()) document.querySelector("#ticketActionStatus").textContent = error.message; }
      }
      document.querySelector("#ticketSelect").addEventListener("change", () => {
        document.querySelector("#ticketNote").value = "";
        document.querySelector("#ticketActionStatus").textContent = "";
        document.querySelector("#ticketOutcome").value = "answered";
        document.querySelector("#ticketAssignee").value = "";
        loadSelectedTicket();
      });
      for (const [button, action] of [["claimTicket", "claim"], ["resolveTicket", "resolve"], ["reassignTicket", "reassign"]]) {
        document.querySelector(`#${button}`).addEventListener("click", async () => {
          if (!selectedTicket) return;
          const epoch = identityEpoch;
          const id = selectedTicket.ticket_id;
          const isCurrent = () => epoch === identityEpoch && document.querySelector("#ticketSelect").value === id;
          const body = {outcome: document.querySelector("#ticketOutcome").value, note: document.querySelector("#ticketNote").value.trim()};
          if (action !== "claim" && !body.note) { document.querySelector("#ticketActionStatus").textContent = "请填写处理或交接说明。"; return; }
          if (action === "reassign") {
            body.target_user_id = document.querySelector("#ticketAssignee").value;
            body.expected_assignee = selectedTicket.assigned_to;
            if (!body.target_user_id) { document.querySelector("#ticketActionStatus").textContent = "请选择新的处理人。"; return; }
          }
          document.querySelector(`#${button}`).disabled = true;
          try {
            await apiFetch(`/admin/tickets/${encodeURIComponent(id)}/${action}`, {
              method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(action === "claim" ? {} : body)
            });
            if (!isCurrent()) return;
            document.querySelector("#ticketActionStatus").textContent = action === "claim" ? "已认领此工单。" : action === "reassign" ? "交接说明已记录，工单已转派。" : "结案结果已保存。";
            await refreshDashboard();
          } catch (error) { if (isCurrent()) document.querySelector("#ticketActionStatus").textContent = error.message; }
          if (isCurrent()) await loadSelectedTicket();
        });
      }

      function reviewRefundId(review) {
        return review?.review_type === "refund" ? review.related_id : review?.continuation?.refund_id;
      }

      function renderReviewWorkbench() {
        document.querySelector("#reviewWorkbench").classList.toggle("hidden", currentRole !== "admin");
        const select = document.querySelector("#reviewSelect");
        const previous = select.value;
        select.replaceChildren(new Option("请选择审核单", ""));
        for (const review of state.dashboard.reviews) {
          select.add(new Option(`${review.review_id} · ${review.status} · ${review.order_id || "无订单"}`, review.review_id));
        }
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
      }

      async function loadSelectedReview() {
        const epoch = identityEpoch;
        const id = document.querySelector("#reviewSelect").value;
        selectedReview = null;
        for (const name of ["approveReview", "rejectReview", "queryReviewRefund", "reconcileReviewRefund"]) document.querySelector(`#${name}`).disabled = true;
        if (!id) { document.querySelector("#reviewDetails").textContent = "请选择审核单。"; return; }
        try {
          const review = (await apiFetch(`/manual-reviews/${encodeURIComponent(id)}`).then(r => r.json())).data;
          if (epoch !== identityEpoch || document.querySelector("#reviewSelect").value !== id) return;
          selectedReview = review;
          document.querySelector("#reviewDetails").textContent = reviewSummary(selectedReview);
          document.querySelector("#approveReview").disabled = selectedReview.status !== "pending_review";
          document.querySelector("#rejectReview").disabled = selectedReview.status !== "pending_review";
          const action = selectedReview.review_type === "risk_control" ? selectedReview.context?.route?.intent : selectedReview.review_type;
          document.querySelector("#reviewAddressFields").classList.toggle("hidden", action !== "address_change");
          document.querySelector("#queryReviewRefund").disabled = !reviewRefundId(selectedReview);
          document.querySelector("#reconcileReviewRefund").disabled = !reviewRefundId(selectedReview);
        } catch (error) {
          if (epoch === identityEpoch && document.querySelector("#reviewSelect").value === id)
            document.querySelector("#reviewActionStatus").textContent = error.message;
        }
      }
      document.querySelector("#reviewSelect").addEventListener("change", () => {
        document.querySelector("#reviewNewAddress").value = "";
        document.querySelector("#reviewNote").value = "";
        loadSelectedReview();
      });
      for (const [button, decision] of [["approveReview", "approve"], ["rejectReview", "reject"]]) {
        document.querySelector(`#${button}`).addEventListener("click", async () => {
          if (!selectedReview) return;
          const epoch = identityEpoch;
          const id = selectedReview.review_id;
          const isCurrent = () => epoch === identityEpoch && document.querySelector("#reviewSelect").value === id;
          const note = document.querySelector("#reviewNote").value.trim();
          if (!note) { document.querySelector("#reviewActionStatus").textContent = "请先填写审核说明。"; return; }
          document.querySelector(`#${button}`).disabled = true;
          try {
            const body = {decision, note, supplement_version: selectedReview.supplement_version || 0};
            if (decision === "approve" && !document.querySelector("#reviewAddressFields").classList.contains("hidden")) {
              body.new_address = document.querySelector("#reviewNewAddress").value.trim();
            }
            const result = await apiFetch(`/manual-reviews/${encodeURIComponent(id)}/resolve`, {
              method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
            }).then(r => r.json());
            if (!isCurrent()) return;
            document.querySelector("#reviewActionStatus").textContent = result.review.next_step;
            await refreshDashboard(); await loadSelectedReview();
          } catch (error) {
            if (!isCurrent()) return;
            document.querySelector("#reviewActionStatus").textContent = error.message; await loadSelectedReview();
          }
        });
      }
      for (const [button, reconcile] of [["queryReviewRefund", false], ["reconcileReviewRefund", true]]) {
        document.querySelector(`#${button}`).addEventListener("click", async () => {
          if (!reviewRefundId(selectedReview)) return;
          const epoch = identityEpoch;
          const reviewId = selectedReview.review_id;
          const isCurrent = () => epoch === identityEpoch && document.querySelector("#reviewSelect").value === reviewId;
          const id = encodeURIComponent(reviewRefundId(selectedReview));
          try {
            const result = await apiFetch(reconcile ? `/admin/refunds/${id}/reconcile-payment` : `/refunds/${id}`, {method: reconcile ? "POST" : "GET"}).then(r => r.json());
            if (!isCurrent()) return;
            document.querySelector("#reviewDetails").textContent = refundSummary(result.data || result.refund);
            document.querySelector("#reviewActionStatus").textContent = "退款状态已更新。";
          } catch (error) { if (isCurrent()) document.querySelector("#reviewActionStatus").textContent = error.message; }
        });
      }
      async function apiFetch(url, options = {}) {
        const headers = new Headers(options.headers || {});
        const token = document.querySelector("#accessToken").value.trim();
        if (token) headers.set("Authorization", `Bearer ${token}`);
        const response = await fetch(url, { ...options, headers });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `请求失败 (${response.status})`);
        }
        return response;
      }

      document.querySelector("#connectBtn").addEventListener("click", async () => {
        identityEpoch += 1;
        const epoch = identityEpoch;
        els.newChatBtn.click();
        currentRole = "customer";
        currentUserId = "";
        availableOperators = [];
        resetTicketPage();
        document.querySelector("#ticketFilter").value = "all";
        selectedTicket = null;
        document.querySelector("#ticketNote").value = "";
        document.querySelector("#ticketActionStatus").textContent = "";
        document.querySelector("#ticketDetails").textContent = "请选择工单。";
        document.querySelector("#claimTicket").disabled = true;
        document.querySelector("#resolveTicket").disabled = true;
        document.querySelector("#reassignTicket").disabled = true;
        state.dashboard = { refunds: [], tickets: [], reviews: [], mq: [], metrics: [] };
        renderMetrics();
        selectedReview = null;
        document.querySelector("#reviewDetails").textContent = "请选择审核单。";
        pendingSupplement = null;
        document.querySelector("#supplementReviewId").value = "";
        document.querySelector("#supplementText").value = "";
        document.querySelector("#supplementStatus").textContent = "";
        document.querySelector("#submitSupplement").disabled = false;
        document.querySelector("#reviewNote").value = "";
        document.querySelector("#reviewNewAddress").value = "";
        document.querySelector("#reviewAddressFields").classList.add("hidden");
        selectedRefund = null;
        document.querySelector("#refundSelect").replaceChildren(new Option("请选择退款申请", ""));
        document.querySelector("#refundDetails").textContent = "请选择退款申请。";
        document.querySelector("#refundCancelNote").value = "";
        document.querySelector("#refundActionStatus").textContent = "";
        document.querySelector("#cancelRefund").disabled = true;
        document.querySelector("#reviewActionStatus").textContent = "";
        renderReviewWorkbench();
        renderTicketChoices();
        try {
          const identity = await apiFetch("/me").then(r => r.json());
          if (epoch !== identityEpoch) return;
          currentRole = identity.role;
          currentUserId = identity.user_id;
          await loadServiceInfo();
          await refreshDashboard();
        } catch (error) {
          if (epoch !== identityEpoch) return;
          setPill(els.serviceStatus, error.message, "danger");
        }
      });

      async function loadServiceInfo() {
        try {
          const [infoResponse, healthResponse] = await Promise.all([
            apiFetch("/info"),
            fetch("/health"),
          ]);
          const info = await infoResponse.json();
          const health = await healthResponse.json();
          els.modelName.textContent = info.default_model || "-";
          els.ragMode.textContent = health.rag_retrieval_mode || info.rag_embedding_provider || "-";
          els.databaseBackend.textContent = health.database_backend || "-";
          els.cacheBackend.textContent = health.cache?.backend || (health.redis_enabled ? "redis" : "local");
          els.mqBackend.textContent = health.mq_backend || "-";
          els.llmKeyState.textContent = info.has_llm_key ? "已配置" : "本地回复";
          setPill(els.serviceStatus, health.success ? "就绪" : "依赖未就绪", health.success ? "ok" : "danger");
        } catch (error) {
          setPill(els.serviceStatus, "离线", "danger");
        }
      }

      async function refreshDashboard() {
        const epoch = identityEpoch;
        const request = ++dashboardRequest;
        document.querySelector("#previousTickets").disabled = true;
        document.querySelector("#nextTickets").disabled = true;
        const params = new URLSearchParams({limit: "20", status: document.querySelector("#ticketFilter").value});
        if (ticketCursor) params.set("cursor", ticketCursor);
        try {
          const [refunds, tickets, reviews, mq, metrics, operators] = await Promise.all([
            apiFetch("/refunds?limit=20").then((res) => res.json()),
            apiFetch(`/tickets?${params}`).then((res) => res.json()),
            currentRole === "admin" ? apiFetch("/manual-reviews?limit=20").then((res) => res.json()) : {},
            currentRole === "admin" ? apiFetch("/mq/messages?limit=20").then((res) => res.json()) : {},
            currentRole === "admin" ? apiFetch("/observability/metrics?limit=20").then((res) => res.json()) : {},
            currentRole === "admin" ? apiFetch("/admin/operators").then((res) => res.json()) : {},
          ]);
          if (epoch !== identityEpoch || request !== dashboardRequest) return;
          nextTicketCursor = tickets.next_cursor;
          availableOperators = operators.data || [];
          state.dashboard = {
            refunds: refunds.data || [],
            tickets: tickets.data || [],
            reviews: reviews.data || [],
            mq: mq.data || [],
            metrics: metrics.data || [],
          };
          renderMetrics();
          renderReviewWorkbench();
          renderRefundChoices();
          renderTicketChoices();
        } catch (error) {
          if (epoch !== identityEpoch || request !== dashboardRequest) return;
          document.querySelector("#ticketPageStatus").textContent = `工单列表读取失败：${error.message}。请刷新首页重试。`;
          els.metricsList.innerHTML = `<div class="soft-note">指标读取失败。</div>`;
        }
      }

      els.chatForm.addEventListener("submit", (event) => {
        event.preventDefault();
        submitMessage(els.messageInput.value);
      });

      els.messageInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          submitMessage(els.messageInput.value);
        }
      });

      document.querySelectorAll(".preset").forEach((button) => {
        button.addEventListener("click", () => {
          els.messageInput.value = button.textContent.trim();
          submitMessage(els.messageInput.value);
        });
      });

      els.traceToggle.addEventListener("change", () => {
        els.tracePanel.classList.toggle("hidden", !els.traceToggle.checked);
      });

      els.newChatBtn.addEventListener("click", () => {
        state.conversationId = "";
        localStorage.removeItem("agent_conversation_id");
        els.conversationId.textContent = "-";
        els.messages.innerHTML = `
          <div class="empty-state" id="emptyState">
            <h2>多 Agent 售后链路就绪</h2>
            <p>当前控制台只覆盖客服问答、售后处理、风控审核、Hybrid RAG、Redis 状态、MQ 退款任务和可观测评测链路。</p>
          </div>
        `;
        els.emptyState = document.querySelector("#emptyState");
        resetCurrentRun();
      });

      renderArchitecture();
      renderAgents();
      if (state.conversationId) {
        els.conversationId.textContent = state.conversationId;
      }
      setPill(els.serviceStatus, "请先连接", "warn");
    