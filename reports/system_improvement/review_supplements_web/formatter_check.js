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
const assert = require('node:assert/strict');
const review = {review_id:'H-test', user_request:'original', status:'approved', supplements:[
 {submitted_at:'now', submitted_by:'owner', text:'before-decision', review_status_at_submission:'pending_review', context:{policy_evidence:[[{citation:'source',text:'observed-evidence'}]]}},
 {submitted_at:'later', submitted_by:'owner', text:'after-decision', review_status_at_submission:'approved'}]};
const output=reviewSummary(review);
for (const text of ['original','before-decision','after-decision','observed-evidence','不属于原决定依据']) assert.ok(output.includes(text));
process.stdout.write(JSON.stringify({passed:true, rendered:output}));
