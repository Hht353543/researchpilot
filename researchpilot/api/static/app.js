/* ResearchPilot frontend: vanilla JS, no build step, no CDN (works offline). */

const SAMPLES = [
  "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。",
  "MCP 基于什么协议，它的核心方法有哪些？",
  "Agent 的三种主流控制流分别是什么，各自代价是什么？",
  "如何防止工具滥用？请给出权限分级与调用预算的设计。",
  "公司内部代号 Proxima 的调度算法参数是多少？",
];

const el = (id) => document.getElementById(id);
let currentTaskId = null;
let asyncTimer = null;

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : text;
    throw new Error(`${response.status} ${detail}`);
  }
  return payload;
}

function setStatus(message, kind = "") {
  const node = el("runStatus");
  node.textContent = message;
  node.className = `status ${kind}`;
}

function chip(label, value) {
  return `<div class="chip">${label} <strong>${value}</strong></div>`;
}

/* ---------------------------- markdown (tiny) ---------------------------- */

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function inline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const out = [];
  let inList = false, inCode = false, inTable = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      closeList(); closeTable();
      out.push(inCode ? "</pre>" : "<pre>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(escapeHtml(line)); continue; }
    if (!line.trim()) { closeList(); closeTable(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList(); closeTable();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      closeTable();
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line) && !line.includes("|")) {
      closeTable();
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(line.replace(/^\s*\d+\.\s+/, ""))}</li>`);
      continue;
    }
    if (line.trim().startsWith("|")) {
      closeList();
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (/^\|?[\s:-]+\|/.test(lines[i + 1] || "") && !inTable) {
        inTable = true;
        out.push("<table><thead><tr>" + cells.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>");
        i += 1;
        continue;
      }
      if (inTable) {
        out.push("<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
        continue;
      }
    }
    if (/^\s*>\s?/.test(line)) { closeList(); closeTable(); out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`); continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { closeList(); closeTable(); out.push("<hr/>"); continue; }

    closeList(); closeTable();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); closeTable();
  if (inCode) out.push("</pre>");
  return out.join("\n");
}

/* ------------------------------ rendering -------------------------------- */

function renderBadges(config, health) {
  el("badges").innerHTML = [
    `<div class="badge">provider <strong>${config.provider}</strong></div>`,
    `<div class="badge">model <strong>${config.default_model}</strong></div>`,
    `<div class="badge">embedding <strong>${config.embedding_provider}</strong></div>`,
    `<div class="badge">mcp <strong>${config.mcp_transport}</strong></div>`,
    `<div class="badge">web <strong>${config.web_search_mode}</strong></div>`,
    `<div class="badge">tools <strong>${(health.tools || []).length}</strong></div>`,
  ].join("");
  el("footerInfo").textContent =
    `ResearchPilot v${health.version} · provider=${config.provider} · kb=${health.knowledge_base.documents} docs / ${health.knowledge_base.chunks} chunks`;
}

function renderMetrics(metrics) {
  if (!metrics) { el("metrics").innerHTML = "<p class='hint'>运行一次研究后显示。</p>"; return; }
  const rows = [
    ["latency", `${(metrics.latency_ms / 1000).toFixed(2)}s`],
    ["llm calls", metrics.llm_calls],
    ["tool calls", metrics.tool_calls],
    ["tool failures", metrics.tool_failures],
    ["retrievals", metrics.retrieval_calls],
    ["mcp calls", metrics.mcp_calls],
    ["retries", metrics.retries],
    ["iterations", metrics.iterations],
    ["tokens", (metrics.usage?.total_tokens ?? 0).toLocaleString()],
    ["cost usd", `$${(metrics.usage?.cost_usd ?? 0).toFixed(6)}`],
  ];
  el("metrics").innerHTML = rows
    .map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
}

function renderTimeline(trace) {
  if (!trace) { el("timeline").innerHTML = "<p class='hint'>暂无 Trace。</p>"; return; }
  const children = new Map();
  (trace.spans || []).forEach((span) => {
    const key = span.parent_id || "root";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(span);
  });
  const walk = (parentId, depth) => {
    const nodes = children.get(parentId) || [];
    return nodes.map((span) => {
      const indent = depth * 18;
      const tokens = span.usage?.total_tokens ? ` · ${span.usage.total_tokens} tok` : "";
      const error = span.error ? `<div class="span-error">${escapeHtml(span.error)}</div>` : "";
      const agentOrTool = span.agent || span.tool || "";
      const body = `
        <div class="span kind-${span.kind}" style="margin-left:${indent}px">
          <div class="span-head">
            <span><strong>${escapeHtml(span.name)}</strong> <span class="span-meta">${span.kind} ${agentOrTool ? "· " + escapeHtml(agentOrTool) : ""}</span></span>
            <span class="span-meta">${Number(span.latency_ms || 0).toFixed(0)} ms${tokens}</span>
          </div>
          ${error}
        </div>`;
      return body + walk(span.span_id, depth + 1);
    }).join("");
  };
  const html = walk("root", 0) || "<p class='hint'>暂无 span。</p>";
  const metrics = trace.metrics || {};
  const summary = `<div class="stats" style="margin-bottom:8px">
      ${chip("spans", (trace.spans || []).length)}
      ${chip("tokens", (metrics.usage?.total_tokens ?? 0).toLocaleString())}
      ${chip("tools", metrics.tool_calls ?? 0)}
      ${chip("llm", metrics.llm_calls ?? 0)}
    </div>`;
  el("timeline").innerHTML = summary + html;
}

function renderSources(sources) {
  if (!sources || !sources.length) { el("sources").innerHTML = "<p class='hint'>暂无来源。</p>"; return; }
  el("sources").innerHTML = sources.map((source) => `
    <div class="list-item">
      <div><code>${escapeHtml(source.id)}</code> ${escapeHtml(source.title || "")}</div>
      <div class="meta">${escapeHtml(source.kind)} · score ${Number(source.score || 0).toFixed(3)} · ${escapeHtml(source.url || source.locator || "")}</div>
    </div>`).join("");
}

function renderKbStats(stats) {
  el("kbStats").innerHTML = [
    chip("docs", stats.documents),
    chip("chunks", stats.chunks),
    chip("chars", (stats.characters || 0).toLocaleString()),
    chip("avg chunk", stats.avg_chunk_chars),
    chip("dim", stats.dimension),
  ].join("");
}

function renderKbDocuments(documents) {
  el("kbDocuments").innerHTML = documents.map((doc) => `
    <div class="list-item">
      <div>${escapeHtml(doc.title)}</div>
      <div class="meta">${doc.doc_id} · ${doc.chunks} chunks · ${doc.source}</div>
    </div>`).join("") || "<p class='hint'>知识库为空。</p>";
}

function renderKbSearch(result) {
  renderKbStats({ documents: "-", chunks: "-", characters: 0, avg_chunk_chars: "-", dimension: "-" });
  el("kbResults").innerHTML = `
    <div class="list-item"><div class="meta">query: ${escapeHtml(result.query)} → rewritten: ${escapeHtml(result.rewritten_query)} · ${result.strategy} · ${result.latency_ms} ms</div></div>
    ` + result.hits.map((hit) => `
    <div class="list-item">
      <div><strong>#${hit.rank}</strong> ${escapeHtml(hit.title)} <span class="meta">${Number(hit.score).toFixed(3)}</span></div>
      <div class="meta">${escapeHtml(hit.chunk_id)} · ${escapeHtml(hit.section || "")}</div>
      <div class="meta">${escapeHtml((hit.content || "").slice(0, 180))}…</div>
    </div>`).join("");
}

function renderReport(report) {
  if (!report) { el("report").innerHTML = "<p class='hint'>暂无报告。</p>"; return; }
  el("report").innerHTML = renderMarkdown(report.markdown || "_report.markdown 为空_");
}

async function loadTask(taskId) {
  currentTaskId = taskId;
  const [result, trace] = await Promise.all([
    api(`/research/${taskId}`),
    api(`/research/${taskId}/trace`).catch(() => null),
  ]);
  renderReport(result.report);
  renderTimeline(trace);
  renderMetrics(result.metrics);
  renderSources(result.evidence ? result.evidence.sources : []);
  setStatus(`task ${taskId} · status=${result.status}${result.errors && result.errors.length ? " · errors=" + result.errors.length : ""}`, result.status === "succeeded" ? "ok" : "error");
  return result;
}

async function runResearch(mode) {
  const question = el("question").value.trim();
  if (question.length < 4) { setStatus("请输入至少 4 个字符的问题", "error"); return; }
  const payload = {
    question,
    mode,
    settings: {
      temperature: Number(el("temperature").value),
      presence_penalty: Number(el("presence").value),
      frequency_penalty: Number(el("frequency").value),
      top_k: Number(el("topk").value),
      max_iterations: Number(el("iterations").value),
    },
  };
  const model = el("model").value.trim();
  if (model) payload.settings.model = model;

  setStatus("运行中…");
  try {
    if (mode === "async") {
      const submitted = await api("/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setStatus(`已提交 task ${submitted.task_id}，轮询中…`);
      if (asyncTimer) clearInterval(asyncTimer);
      asyncTimer = setInterval(async () => {
        try {
          const result = await loadTask(submitted.task_id);
          if (result.status !== "pending" && result.status !== "running") clearInterval(asyncTimer);
        } catch (error) { /* keep polling */ }
      }, 1500);
      return;
    }
    const result = await api("/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadTask(result.task_id);
  } catch (error) {
    setStatus(`失败：${error.message}`, "error");
  }
}

async function boot() {
  el("samples").innerHTML = SAMPLES.map((sample, index) =>
    `<button data-sample="${index}">${escapeHtml(sample.slice(0, 26))}…</button>`).join("");
  el("samples").addEventListener("click", (event) => {
    const index = event.target?.dataset?.sample;
    if (index !== undefined) el("question").value = SAMPLES[Number(index)];
  });
  el("run").addEventListener("click", () => runResearch("sync"));
  el("runAsync").addEventListener("click", () => runResearch("async"));
  el("kbSearch").addEventListener("click", async () => {
    const q = el("kbQuery").value.trim();
    if (q.length < 2) return;
    try {
      renderKbSearch(await api(`/kb/search?q=${encodeURIComponent(q)}&strategy=${el("kbStrategy").value}`));
    } catch (error) { el("kbResults").innerHTML = `<p class='hint'>${escapeHtml(error.message)}</p>`; }
  });
  el("kbReindex").addEventListener("click", async () => {
    const report = await api("/kb/reindex", { method: "POST" });
    const docs = await api("/kb/documents");
    renderKbStats(docs.stats);
    renderKbDocuments(docs.documents);
    el("kbResults").innerHTML = `<p class='hint'>reindexed: ${report.documents} docs / ${report.chunks} chunks</p>`;
  });

  const [config, health, documents] = await Promise.all([
    api("/config"), api("/health"), api("/kb/documents"),
  ]);
  renderBadges(config, health);
  el("model").value = config.default_model;
  el("temperature").value = config.temperature;
  el("presence").value = config.presence_penalty;
  el("frequency").value = config.frequency_penalty;
  el("topk").value = config.top_k;
  el("iterations").value = config.max_iterations;
  el("configHint").textContent =
    `provider=${config.provider} · token_budget=${config.token_budget} · max_tokens=${config.max_tokens} · 模型/温度等参数通过环境变量或此处覆盖`;
  renderKbStats(documents.stats);
  renderKbDocuments(documents.documents);
  try {
    const tools = await api("/mcp/tools");
    el("mcpTools").innerHTML = (tools.tools || []).map((tool) => `
      <div class="list-item"><div><code>${escapeHtml(tool.name)}</code></div>
      <div class="meta">${escapeHtml(tool.description || "")}</div></div>`).join("")
      || "<p class='hint'>MCP 客户端不可用。</p>";
  } catch (error) { el("mcpTools").innerHTML = `<p class='hint'>${escapeHtml(error.message)}</p>`; }

  try {
    const runs = await api("/research?limit=1");
    if (runs.length) await loadTask(runs[0].task_id);
  } catch (error) { /* no previous runs */ }
}

document.addEventListener("DOMContentLoaded", boot);
