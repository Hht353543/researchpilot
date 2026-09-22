/* ResearchPilot frontend: vanilla JS, no build step, no CDN (works offline). */

const SAMPLES = [
  "比较三个主流 AI coding agent 的定位、优势和适用团队。",
  "调研企业部署生成式 AI 时最常见的安全风险与应对方案。",
  "分析一个技术团队从单体应用迁移到微服务的收益与代价。",
  "总结 MCP 的工作方式、典型使用场景和落地注意事项。",
];

const STATUS = {
  pending: { label: "准备中", title: "正在准备研究", kind: "pending" },
  running: { label: "研究中", title: "正在搜索和整理信息", kind: "running" },
  completed: { label: "已完成", title: "研究完成", kind: "completed" },
  failed: { label: "失败", title: "研究未能完成", kind: "failed" },
  cancelled: { label: "已取消", title: "任务已取消", kind: "cancelled" },
  timed_out: { label: "已超时", title: "研究已超时", kind: "timed_out" },
};

const ERROR_MESSAGES = {
  authentication_required: ["需要访问令牌", "请输入管理员提供的平台访问令牌，然后重新连接。"],
  llm_config: ["模型尚未配置", "请联系管理员检查模型和 API Key 配置。"],
  llm: ["模型连接失败", "请稍后重试；如果问题持续，请联系管理员检查模型服务。"],
  token_budget: ["本次研究达到资源上限", "请缩小问题范围，或联系管理员调整研究预算。"],
  capacity_exceeded: ["当前研究任务较多", "请稍后再试，已有任务不会受到影响。"],
  request_too_large: ["输入内容过长", "请缩短研究问题或知识库内容后重试。"],
  persistence_unavailable: ["暂时无法保存数据", "请稍后重试；如果问题持续，请联系管理员检查存储。"],
  persistence_corruption: ["已有数据无法读取", "请联系管理员检查已保存的研究数据。"],
  storage_unavailable: ["存储服务暂时不可用", "请稍后重试；如果问题持续，请联系管理员。"],
  shutting_down: ["服务正在重启", "请等待片刻后重新开始研究。"],
  network: ["无法连接 ResearchPilot", "请确认服务正在运行并检查网络连接。"],
  timed_out: ["研究超过最大执行时间", "任务已停止。可以缩小问题范围后重新研究。"],
  cancelled: ["任务已取消", "你可以修改问题后重新开始研究。"],
  unknown: ["操作未能完成", "请稍后重试；如果问题持续，可展开技术详情并联系管理员。"],
};

const el = (id) => document.getElementById(id);
let currentTaskId = null;
let pollTimer = null;
let pollFailures = 0;
let privateApiReady = false;
let currentReportMarkdown = "";

class ApiError extends Error {
  constructor(message, kind = "unknown", status = 0, detail = "") {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail || message;
  }
}

async function api(path, options) {
  const request = { ...(options || {}) };
  request.headers = { ...(request.headers || {}) };
  const token = el("accessToken")?.value.trim();
  if (token) request.headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(path, request);
  } catch (error) {
    throw new ApiError("network request failed", "network", 0, String(error));
  }
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : (text || response.statusText);
    const kind = payload && payload.kind
      ? payload.kind
      : ({ 401: "authentication_required", 413: "request_too_large", 429: "capacity_exceeded" }[response.status] || "unknown");
    throw new ApiError(`${response.status} ${detail}`, kind, response.status, detail);
  }
  return payload;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
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
      closeList(); closeTable(); out.push(inCode ? "</pre>" : "<pre>"); inCode = !inCode; continue;
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
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      if (/^\|?[\s:-]+\|/.test(lines[i + 1] || "") && !inTable) {
        inTable = true;
        out.push("<table><thead><tr>" + cells.map((cell) => `<th>${inline(cell)}</th>`).join("") + "</tr></thead><tbody>");
        i += 1;
        continue;
      }
      if (inTable) {
        out.push("<tr>" + cells.map((cell) => `<td>${inline(cell)}</td>`).join("") + "</tr>");
        continue;
      }
    }
    if (/^\s*>\s?/.test(line)) {
      closeList(); closeTable(); out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`); continue;
    }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      closeList(); closeTable(); out.push("<hr/>"); continue;
    }
    closeList(); closeTable(); out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); closeTable();
  if (inCode) out.push("</pre>");
  return out.join("\n");
}

function statusMeta(status) {
  return STATUS[status] || { label: status || "未知", title: "任务状态未知", kind: "failed" };
}

function friendlyError(errorOrKind) {
  const kind = typeof errorOrKind === "string" ? errorOrKind : (errorOrKind?.kind || "unknown");
  const [title, message] = ERROR_MESSAGES[kind] || ERROR_MESSAGES.unknown;
  const detail = typeof errorOrKind === "string"
    ? errorOrKind
    : (errorOrKind?.detail || errorOrKind?.message || String(errorOrKind || ""));
  return { kind, title, message, detail };
}

function showError(errorOrKind, extraDetail = "") {
  const message = friendlyError(errorOrKind);
  el("errorTitle").textContent = message.title;
  el("errorMessage").textContent = message.message;
  el("errorDetails").textContent = [message.detail, extraDetail].filter(Boolean).join("\n");
  el("errorPanel").classList.remove("hidden");
}

function clearError() {
  el("errorPanel").classList.add("hidden");
  el("errorDetails").textContent = "";
}

function setBusy(busy) {
  el("run").disabled = busy || !privateApiReady;
  el("run").textContent = busy ? "正在启动…" : "开始研究";
  el("cancelResearch").classList.toggle("hidden", !busy || !currentTaskId);
}

function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function renderHealth(health) {
  const warning = health.status !== "ok" || !health.provider_ready;
  el("badges").innerHTML = `<span class="system-pill ${warning ? "warning" : ""}"><span>${warning ? "需要配置" : "服务正常"}</span></span>`;
  el("footerInfo").textContent = `ResearchPilot v${health.version} · ${health.provider} / ${health.model}`;
  if (!health.provider_ready) {
    el("providerNotice").textContent = "开始第一次研究前，请联系管理员完成模型配置。";
    el("providerNotice").classList.remove("hidden");
  }
  if (health.auth_required) {
    el("accessTokenGroup").classList.remove("hidden");
  }
}

function renderConfig(config) {
  el("model").value = config.default_model;
  el("temperature").value = config.temperature;
  el("presence").value = config.presence_penalty;
  el("frequency").value = config.frequency_penalty;
  el("topk").value = config.top_k;
  el("iterations").value = config.max_iterations;
  el("maxtokens").value = config.max_tokens;
  const credential = config.provider === "mock"
    ? "演示模式无需 API Key"
    : (config.api_key_configured ? "API Key 已由管理员配置" : "API Key 尚未配置");
  el("modelStatus").innerHTML = [
    `<span class="model-chip">服务商 <strong>${escapeHtml(config.provider)}</strong></span>`,
    `<span class="model-chip">模型 <strong>${escapeHtml(config.default_model)}</strong></span>`,
    `<span class="model-chip">凭据 <strong>${escapeHtml(credential)}</strong></span>`,
  ].join("");
  el("settingsSummary").textContent = `${config.provider} · ${config.default_model}`;
  el("configHint").textContent =
    `embedding=${config.embedding_provider} · mcp=${config.mcp_transport} · web=${config.web_search_mode} · ` +
    `token budget=${config.token_budget} · concurrency=${config.max_concurrent_tasks}`;
}

function metric(label, value) {
  return `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`;
}

function renderMetrics(metrics) {
  if (!metrics) {
    el("metrics").innerHTML = '<div class="empty-state small">暂无运行数据。</div>';
    return;
  }
  el("metrics").innerHTML = [
    metric("用时", `${(Number(metrics.latency_ms || 0) / 1000).toFixed(1)} 秒`),
    metric("模型调用", metrics.llm_calls ?? 0),
    metric("工具调用", metrics.tool_calls ?? 0),
    metric("资料检索", metrics.retrieval_calls ?? 0),
    metric("研究轮次", metrics.iterations ?? 0),
    metric("Token", Number(metrics.usage?.total_tokens || 0).toLocaleString()),
  ].join("");
}

function renderTimeline(trace) {
  if (!trace || !(trace.spans || []).length) {
    el("timeline").innerHTML = '<div class="empty-state small">暂无执行详情。</div>';
    return;
  }
  const children = new Map();
  trace.spans.forEach((span) => {
    const key = span.parent_id || "root";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(span);
  });
  const walk = (parentId, depth) => (children.get(parentId) || []).map((span) => {
    const tokens = span.usage?.total_tokens ? ` · ${span.usage.total_tokens} tokens` : "";
    const error = span.error ? `<div class="span-error">${escapeHtml(span.error)}</div>` : "";
    return `<div class="span kind-${escapeHtml(span.kind)}" style="margin-left:${depth * 16}px">
      <div class="span-head"><strong>${escapeHtml(span.name)}</strong><span class="span-meta">${Number(span.latency_ms || 0).toFixed(0)} ms${tokens}</span></div>
      ${error}</div>${walk(span.span_id, depth + 1)}`;
  }).join("");
  el("timeline").innerHTML = walk("root", 0);
}

function renderSources(sources) {
  const items = sources || [];
  el("sourceCount").textContent = String(items.length);
  if (!items.length) {
    el("sources").innerHTML = '<div class="empty-state small">这次研究没有可展示的来源。</div>';
    return;
  }
  el("sources").innerHTML = items.map((source, index) => {
    const url = safeExternalUrl(source.url);
    const title = escapeHtml(source.title || source.locator || `来源 ${index + 1}`);
    const heading = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${index + 1}. ${title}</a>`
      : `<strong>${index + 1}. ${title}</strong>`;
    const location = source.url || source.locator || source.doc_id || source.id;
    return `<div class="source-item">${heading}<div class="source-meta">${escapeHtml(location)}</div></div>`;
  }).join("");
}

function renderReport(report) {
  if (!report) {
    currentReportMarkdown = "";
    el("report").innerHTML = '<div class="empty-state">这次研究没有生成报告。</div>';
    return;
  }
  currentReportMarkdown = report.markdown || "";
  el("resultTitle").textContent = report.title || "最终报告";
  el("report").innerHTML = renderMarkdown(currentReportMarkdown || report.executive_summary || "暂无报告内容。");
}

function renderHistory(runs) {
  if (!runs || !runs.length) {
    el("history").innerHTML = '<div class="empty-state small">还没有研究记录。<br />输入一个问题开始第一次研究。</div>';
    return;
  }
  el("history").innerHTML = runs.map((run) => {
    const state = statusMeta(run.status);
    return `<button class="history-item ${run.task_id === currentTaskId ? "active" : ""}" type="button" data-history-task="${escapeHtml(run.task_id)}">
      <span class="history-question">${escapeHtml(run.question)}</span>
      <span class="history-meta"><span class="history-status ${state.kind}">${state.label}</span><span>${formatDate(run.completed_at || run.created_at)}</span></span>
    </button>`;
  }).join("");
}

function renderKbStats(stats) {
  el("kbStats").innerHTML = `<strong>${Number(stats?.documents || 0)}</strong> 份资料`;
}

function renderKbDocuments(documents) {
  if (!documents || !documents.length) {
    el("kbDocuments").innerHTML = '<div class="empty-state small">团队知识库目前为空。添加资料后，研究时会自动利用这些内容。</div>';
    return;
  }
  el("kbDocuments").innerHTML = documents.map((doc) => `<div class="document-item">
    <strong>${escapeHtml(doc.title)}</strong>
    <div class="meta">${Number(doc.chunks || 0)} 个内容片段 · ${escapeHtml(doc.source || "团队资料")}</div>
    <button class="link-button" type="button" data-delete-doc="${escapeHtml(doc.doc_id)}">删除资料</button>
  </div>`).join("");
}

function renderKbSearch(result) {
  if (!result?.hits?.length) {
    el("kbResults").innerHTML = '<div class="empty-state small">没有找到相关资料，试试更换关键词。</div>';
    return;
  }
  el("kbResults").innerHTML = result.hits.map((hit) => `<div class="kb-hit">
    <strong>${escapeHtml(hit.title)}</strong>
    <p>${escapeHtml((hit.content || "").slice(0, 220))}${(hit.content || "").length > 220 ? "…" : ""}</p>
  </div>`).join("");
}

function renderEvaluation(payload) {
  if (!payload || payload.available === false) {
    el("evaluation").innerHTML = '<p class="muted">暂无离线评估结果。</p>';
    return;
  }
  const metrics = payload.metrics || {};
  el("evaluation").innerHTML = `<p class="muted">离线评估：${metrics.tasks ?? 0} 个任务 · 成功率 ${Math.round(Number(metrics.task_success_rate || 0) * 100)}%</p>`;
}

function renderMcp(tools) {
  const names = (tools?.tools || []).map((tool) => escapeHtml(tool.name));
  el("mcpTools").innerHTML = `<p class="muted">可用工具：${names.join("、") || "无"}</p>`;
}

function updateProgress(result, trace) {
  const state = statusMeta(result.status);
  el("progressTitle").textContent = state.title;
  el("runStatus").textContent = state.label;
  el("runStatus").className = `status-pill ${state.kind}`;
  el("progressPanel").classList.remove("hidden");
  const names = (trace?.spans || []).map((span) => String(span.name || "").toLowerCase());
  const writing = names.some((name) => name.includes("writer") || name.includes("report"));
  const researching = names.some((name) => name.includes("tool") || name.includes("search") || name.includes("retriev"));
  const stages = [el("stagePrepare"), el("stageResearch"), el("stageReport")];
  stages.forEach((stage) => { stage.classList.remove("active", "done"); });
  if (result.status === "completed") {
    stages.forEach((stage) => stage.classList.add("done"));
  } else if (writing) {
    stages[0].classList.add("done"); stages[1].classList.add("done"); stages[2].classList.add("active");
  } else if (researching || result.status === "running") {
    stages[0].classList.add("done"); stages[1].classList.add("active");
  } else {
    stages[0].classList.add("active");
  }
  el("progressMessage").textContent = {
    pending: "任务正在等待开始。",
    running: writing ? "正在整理已有信息并生成最终报告。" : "正在搜索和整理与问题相关的信息。",
    completed: "报告和来源已经准备好。",
    failed: "研究没有完成，请查看下方提示。",
    cancelled: "任务已取消。",
    timed_out: "任务超过最大执行时间并已停止。",
  }[result.status] || "正在更新任务状态。";
}

function renderTask(result, trace) {
  currentTaskId = result.task_id;
  updateProgress(result, trace);
  const terminal = !["pending", "running"].includes(result.status);
  setBusy(!terminal);
  if (!terminal) return;
  if (pollTimer) clearTimeout(pollTimer);
  if (result.status === "completed") {
    clearError();
    renderReport(result.report);
    renderSources(result.evidence?.sources || []);
    renderMetrics(result.metrics);
    renderTimeline(trace);
    const degraded = result.quality === "degraded";
    el("resultMeta").textContent = degraded
      ? "研究已完成，但部分信息不可用。报告中保留了可验证的结果。"
      : `${formatDate(result.completed_at)} · ${(result.evidence?.sources || []).length} 个来源`;
    el("resultSection").classList.remove("hidden");
    if (degraded || (result.errors || []).length) {
      const details = (result.errors || []).join("\n");
      el("providerNotice").textContent = "研究已完成，但部分模型或工具调用不可用。你仍可以阅读已生成的报告。";
      el("providerNotice").classList.remove("hidden");
      if (details) el("errorDetails").textContent = details;
    }
  } else {
    el("resultSection").classList.add("hidden");
    const kind = result.status === "timed_out" ? "timed_out" : (result.status === "cancelled" ? "cancelled" : "unknown");
    showError(kind, (result.errors || []).join("\n"));
  }
}

async function loadTask(taskId) {
  currentTaskId = taskId;
  const [result, trace] = await Promise.all([
    api(`/research/${taskId}`),
    api(`/research/${taskId}/trace`).catch(() => null),
  ]);
  renderTask(result, trace);
  return result;
}

function schedulePoll(taskId) {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const result = await loadTask(taskId);
      pollFailures = 0;
      if (["pending", "running"].includes(result.status)) {
        schedulePoll(taskId);
      } else {
        await loadHistory();
      }
    } catch (error) {
      pollFailures += 1;
      if (pollFailures < 3) {
        el("progressMessage").textContent = "暂时无法更新进度，正在重试…";
        schedulePoll(taskId);
      } else {
        setBusy(false);
        showError(error);
      }
    }
  }, 1500);
}

function researchPayload(question) {
  const payload = {
    question,
    mode: "async",
    settings: {
      temperature: Number(el("temperature").value),
      presence_penalty: Number(el("presence").value),
      frequency_penalty: Number(el("frequency").value),
      top_k: Number(el("topk").value),
      max_iterations: Number(el("iterations").value),
      max_tokens: Number(el("maxtokens").value),
    },
  };
  const model = el("model").value.trim();
  if (model) payload.settings.model = model;
  return payload;
}

async function runResearch() {
  clearError();
  const question = el("question").value.trim();
  if (!privateApiReady) {
    showError("authentication_required");
    el("settings").open = true;
    el("settings").scrollIntoView({ behavior: "smooth" });
    return;
  }
  if (question.length < 4) {
    showError(new ApiError("question must contain at least four characters", "unknown", 422, "请输入至少 4 个字符的研究问题。"));
    el("errorTitle").textContent = "研究问题太短";
    el("errorMessage").textContent = "请补充一些背景或说明你希望得到什么结果。";
    return;
  }
  currentTaskId = null;
  el("resultSection").classList.add("hidden");
  el("progressPanel").classList.remove("hidden");
  updateProgress({ status: "pending" }, null);
  setBusy(true);
  try {
    const submitted = await api("/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(researchPayload(question)),
    });
    currentTaskId = submitted.task_id;
    el("cancelResearch").classList.remove("hidden");
    schedulePoll(submitted.task_id);
    await loadHistory();
  } catch (error) {
    setBusy(false);
    el("progressPanel").classList.add("hidden");
    showError(error);
  }
}

async function cancelResearch() {
  if (!currentTaskId) return;
  el("cancelResearch").disabled = true;
  el("cancelResearch").textContent = "正在取消…";
  try {
    const result = await api(`/research/${currentTaskId}`, { method: "DELETE" });
    renderTask(result, null);
    await loadHistory();
  } catch (error) {
    showError(error);
  } finally {
    el("cancelResearch").disabled = false;
    el("cancelResearch").textContent = "取消研究";
  }
}

async function loadHistory() {
  try {
    const runs = await api("/research?limit=12");
    renderHistory(runs);
  } catch (error) {
    if (error.kind !== "authentication_required") showError(error);
  }
}

async function loadKnowledgeBase() {
  const documents = await api("/kb/documents");
  renderKbStats(documents.stats);
  renderKbDocuments(documents.documents);
}

async function loadPrivateData() {
  try {
    const [config, documents, runs, tools, evaluation] = await Promise.all([
      api("/config"),
      api("/kb/documents"),
      api("/research?limit=12"),
      api("/mcp/tools").catch(() => ({ tools: [] })),
      api("/evaluation/latest").catch(() => ({ available: false })),
    ]);
    privateApiReady = true;
    renderConfig(config);
    renderKbStats(documents.stats);
    renderKbDocuments(documents.documents);
    renderHistory(runs);
    renderMcp(tools);
    renderEvaluation(evaluation);
    clearError();
    el("providerNotice").classList.add("hidden");
    setBusy(false);
    return true;
  } catch (error) {
    privateApiReady = false;
    setBusy(false);
    if (error.kind === "authentication_required") {
      el("accessTokenGroup").classList.remove("hidden");
      el("providerNotice").textContent = "此平台需要访问令牌。打开“模型与高级设置”，输入管理员提供的令牌后即可开始研究。";
      el("providerNotice").classList.remove("hidden");
    }
    showError(error);
    return false;
  }
}

function resetResearch() {
  currentTaskId = null;
  currentReportMarkdown = "";
  clearError();
  el("progressPanel").classList.add("hidden");
  el("resultSection").classList.add("hidden");
  el("question").value = "";
  el("questionCount").textContent = "0 / 2000";
  setBusy(false);
  el("question").focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function copyReport() {
  if (!currentReportMarkdown) return;
  try {
    await navigator.clipboard.writeText(currentReportMarkdown);
    el("copyReport").textContent = "已复制";
    setTimeout(() => { el("copyReport").textContent = "复制报告"; }, 1600);
  } catch (error) {
    showError(new ApiError("clipboard write failed", "unknown", 0, String(error)));
  }
}

function bindEvents() {
  el("question").addEventListener("input", () => {
    el("questionCount").textContent = `${el("question").value.length} / 2000`;
  });
  el("question").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") runResearch();
  });
  el("samples").innerHTML = SAMPLES.map((sample, index) =>
    `<button data-sample="${index}" type="button">${escapeHtml(sample)}</button>`).join("");
  el("samples").addEventListener("click", (event) => {
    const index = event.target?.dataset?.sample;
    if (index === undefined) return;
    el("question").value = SAMPLES[Number(index)];
    el("questionCount").textContent = `${el("question").value.length} / 2000`;
    el("question").focus();
  });
  el("run").addEventListener("click", runResearch);
  el("cancelResearch").addEventListener("click", cancelResearch);
  el("newResearch").addEventListener("click", resetResearch);
  el("copyReport").addEventListener("click", copyReport);
  el("refreshHistory").addEventListener("click", loadHistory);
  el("history").addEventListener("click", async (event) => {
    const button = event.target?.closest?.("[data-history-task]");
    const taskId = button?.dataset?.historyTask;
    if (!taskId) return;
    try {
      clearError();
      const result = await loadTask(taskId);
      renderHistory(await api("/research?limit=12"));
      if (["pending", "running"].includes(result.status)) schedulePoll(taskId);
      el("progressPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showError(error);
    }
  });
  el("applyAccessToken").addEventListener("click", async () => {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.setItem("researchpilot.accessToken", el("accessToken").value.trim());
    }
    const connected = await loadPrivateData();
    el("applyAccessToken").textContent = connected ? "已连接" : "重新连接";
  });
  el("kbSearch").addEventListener("click", async () => {
    const query = el("kbQuery").value.trim();
    if (query.length < 2) {
      el("kbResults").innerHTML = '<div class="empty-state small">请输入至少两个字符。</div>';
      return;
    }
    try {
      const result = await api(`/kb/search?q=${encodeURIComponent(query)}&strategy=${el("kbStrategy").value}`);
      renderKbSearch(result);
    } catch (error) {
      el("kbResults").innerHTML = `<div class="empty-state small">${escapeHtml(friendlyError(error).message)}</div>`;
    }
  });
  el("kbAdd").addEventListener("click", async () => {
    const title = el("kbTitle").value.trim();
    const content = el("kbContent").value.trim();
    if (title.length < 2 || content.length < 20) {
      el("kbFeedback").textContent = "请填写标题和至少 20 个字符的内容。";
      return;
    }
    el("kbAdd").disabled = true;
    el("kbFeedback").textContent = "正在添加…";
    try {
      await api("/kb/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, content, source: "web-ui://inline" }),
      });
      el("kbTitle").value = "";
      el("kbContent").value = "";
      el("kbFeedback").textContent = "已添加。";
      await loadKnowledgeBase();
    } catch (error) {
      el("kbFeedback").textContent = friendlyError(error).message;
    } finally {
      el("kbAdd").disabled = false;
    }
  });
  el("kbDocuments").addEventListener("click", async (event) => {
    const docId = event.target?.dataset?.deleteDoc;
    if (!docId) return;
    if (typeof window.confirm === "function" && !window.confirm("确定删除这份团队资料吗？")) return;
    event.target.disabled = true;
    try {
      await api(`/kb/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
      el("kbFeedback").textContent = "资料已删除。";
      await loadKnowledgeBase();
    } catch (error) {
      el("kbFeedback").textContent = friendlyError(error).message;
    }
  });
  el("kbReindex").addEventListener("click", async () => {
    el("kbReindex").disabled = true;
    try {
      await api("/kb/reindex", { method: "POST" });
      el("kbFeedback").textContent = "知识库已重新索引。";
      await loadKnowledgeBase();
    } catch (error) {
      el("kbFeedback").textContent = friendlyError(error).message;
    } finally {
      el("kbReindex").disabled = false;
    }
  });
}

async function boot() {
  const savedToken = typeof sessionStorage === "undefined"
    ? "" : (sessionStorage.getItem("researchpilot.accessToken") || "");
  el("accessToken").value = savedToken;
  bindEvents();
  setBusy(true);
  try {
    const health = await api("/health");
    renderHealth(health);
    await loadPrivateData();
  } catch (error) {
    privateApiReady = false;
    setBusy(false);
    showError(error);
    el("history").innerHTML = '<div class="empty-state small">连接服务后会显示研究记录。</div>';
  }
}

document.addEventListener("DOMContentLoaded", boot);
