/* ResearchPilot frontend: vanilla JS, no build step, no CDN (works offline). */

const SAMPLES = [
  "比较主流 AI Agent 平台的定位、优势和适用团队。",
  "调研企业部署生成式 AI 时最常见的安全风险与应对方案。",
  "分析企业采用 RAG 知识库的收益、成本和实施路线。",
];
const RESEARCH_SETTING_IDS = [
  "temperature", "presence", "frequency", "topk", "iterations", "maxtokens",
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
  authentication_required: [
    "需要访问令牌",
    "请输入管理员提供的平台访问令牌，然后重新连接。",
  ],
  llm_config: ["模型尚未配置", "请联系管理员检查模型和 API Key 配置。"],
  llm: ["模型连接失败", "请稍后重试；如果问题持续，请联系管理员检查模型服务。"],
  token_budget: [
    "本次研究达到资源上限",
    "请缩小问题范围，或联系管理员调整研究预算。",
  ],
  capacity_exceeded: ["当前研究任务较多", "请稍后再试，已有任务不会受到影响。"],
  request_too_large: ["输入内容过长", "请缩短研究问题或知识库内容后重试。"],
  document_scope: ["资料范围需要更新", "检查本次勾选的资料；追问会继承上次的资料范围。"],
  persistence_unavailable: [
    "暂时无法保存数据",
    "请稍后重试；如果问题持续，请联系管理员检查存储。",
  ],
  persistence_corruption: [
    "已有数据无法读取",
    "请联系管理员检查已保存的研究数据。",
  ],
  storage_unavailable: [
    "存储服务暂时不可用",
    "请稍后重试；如果问题持续，请联系管理员。",
  ],
  shutting_down: ["服务正在重启", "请等待片刻后重新开始研究。"],
  network: ["无法连接 ResearchPilot", "请确认服务正在运行并检查网络连接。"],
  timed_out: [
    "研究超过最大执行时间",
    "任务已停止。可以缩小问题范围后重新研究。",
  ],
  cancelled: ["任务已取消", "你可以修改问题后重新开始研究。"],
  unknown: [
    "操作未能完成",
    "请稍后重试；如果问题持续，可展开技术详情并联系管理员。",
  ],
};

const el = (id) => document.getElementById(id);
let currentTaskId = null;
let pollTimer = null;
let pollFailures = 0;
let privateApiReady = false;
let currentReportMarkdown = "";
let taskQuestion = "";
let historyRuns = [];
let observationVersion = 0;
let taskLoading = false;
let providerReady = false;
let desktopMode = false;
let maxImportBytes = 1048576;
let citationLinks = {};
let sourceNumbers = {};
let toastTimer = null;
let validationErrorField = null;
let knowledgeDocuments = [];
let selectedDocumentIds = null;
let exampleMode = false;
let parentResearch = null;
let currentResult = null;

function icon(name) {
  return `<svg aria-hidden="true"><use href="/static/icons.svg#${name}"></use></svg>`;
}

function emptyState(name, title, copy, action = "") {
  return `<div class="empty-state">${icon(name)}<strong>${escapeHtml(title)}</strong>${escapeHtml(copy)}${action}</div>`;
}

function toast(message) {
  el("toast").textContent = message;
  el("toast").classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el("toast").classList.add("hidden"), 2600);
}

function stopObservation() {
  observationVersion += 1;
  clearTimeout(pollTimer);
  pollTimer = null;
  pollFailures = 0;
}

function showView(view) {
  const ids = {
    research: "researchView",
    task: "taskView",
    history: "historyView",
    knowledge: "knowledge",
    settings: "settings",
  };
  Object.entries(ids).forEach(([name, id]) =>
    el(id).classList.toggle("hidden", name !== view),
  );
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === (view === "task" ? "research" : view);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  el("pageLabel").textContent = {
    research: "研究",
    task: "研究报告",
    history: "研究记录",
    knowledge: "我的资料",
    settings: "设置",
  }[view];
  window.scrollTo({ top: 0, behavior: "instant" });
}

function navigate(view) {
  if (window.location.hash === `#${view}`) showView(view);
  else window.location.hash = view;
}

async function openTask(taskId) {
  stopObservation();
  const version = observationVersion;
  clearError();
  showView("task");
  el("resultSection").classList.add("hidden");
  el("progressPanel").classList.remove("hidden");
  el("progressTitle").textContent = "正在打开研究";
  el("progressQuery").textContent = "";
  el("progressMessage").textContent = "正在读取已保存的报告与来源。";
  el("progressPanel").classList.remove("terminal");
  el("retryResearch").classList.add("hidden");
  el("progressActivity").classList.remove("hidden");
  taskLoading = true;
  setBusy(true);
  try {
    const result = await loadTask(taskId);
    if (!result) return;
    if (["pending", "running"].includes(result.status)) schedulePoll(taskId);
    renderHistory(historyRuns);
  } catch (error) {
    if (currentTaskId !== taskId || version !== observationVersion) return;
    currentTaskId = null;
    taskLoading = false;
    setBusy(false);
    el("progressPanel").classList.add("hidden");
    showError(error);
  }
}

async function handleRoute() {
  const hash = window.location.hash.slice(1);
  const taskId = hash.startsWith("task/") ? hash.slice(5) : "";
  if (taskId) {
    if (currentTaskId === taskId) showView("task");
    else await openTask(taskId);
    return;
  }
  showView(
    ["research", "history", "knowledge", "settings", "task"].includes(hash)
      ? hash
      : "research",
  );
}

function prepareCitations(result) {
  citationLinks = {};
  sourceNumbers = {};
  const sources = result.evidence?.sources || [];
  const evidence = result.evidence?.evidence || [];
  const markdown = result.report?.markdown || "";
  // The appendix binds each reference number to an evidence ID, which in turn
  // identifies the exact source chunk. Several chunks can share one locator.
  const sourceByEvidence = new Map(
    evidence.map((item) => [item.id, item.source_id]),
  );
  for (const match of markdown.matchAll(
    /^\|\s*(E\d+)\s*\|.*\|\s*\[(\d+)\]\s+.*\|\s*(?:\d+(?:\.\d+)?\s*\|\s*)?$/gm,
  )) {
    const sourceId = sourceByEvidence.get(match[1]);
    if (sourceId) sourceNumbers[sourceId] = Number(match[2]);
  }
  // Reports without an appendix can still be mapped when a locator is unique.
  for (const match of markdown.matchAll(
    /^(\d+)\. \*\*[^\n]+?\*\*[^\n]*?`([^`]+)`/gm,
  )) {
    const matchingSources = sources.filter(
      (item) =>
        (item.url || item.locator || item.doc_id || item.id) === match[2],
    );
    if (matchingSources.length === 1 && !sourceNumbers[matchingSources[0].id])
      sourceNumbers[matchingSources[0].id] = Number(match[1]);
  }
  const documentNumbers = new Map(sources.filter(source => source.doc_id && sourceNumbers[source.id])
    .map(source => [source.doc_id, sourceNumbers[source.id]]));
  sources.forEach(source => {
    if (!sourceNumbers[source.id] && source.doc_id && documentNumbers.has(source.doc_id))
      sourceNumbers[source.id] = documentNumbers.get(source.doc_id);
  });
  sourceGroups(sources).forEach(({ source, index, ids }) => {
    const anchor = `source-${index + 1}`;
    const number = sourceNumbers[source.id];
    if (number) citationLinks[String(number)] = anchor;
    (result.evidence?.evidence || [])
      .filter((item) => ids.includes(item.source_id))
      .forEach((item) => {
        citationLinks[item.id] = anchor;
      });
  });
}

function sourceGroups(sources) {
  const groups = new Map();
  sources.forEach((source, index) => {
    const number = sourceNumbers[source.id];
    const key = number ? `reference:${number}` : source.doc_id ? `document:${source.doc_id}` : `source:${source.id}`;
    if (!groups.has(key)) groups.set(key, { source, index, ids: [] });
    groups.get(key).ids.push(source.id);
  });
  return Array.from(groups.values());
}

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
  if (desktopMode) request.headers["X-ResearchPilot"] = "desktop";
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
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const rawDetail =
      payload && payload.detail ? payload.detail : text || response.statusText;
    const detail = typeof rawDetail === "object" ? rawDetail.message || JSON.stringify(rawDetail) : rawDetail;
    const kind =
      payload && (payload.kind || rawDetail?.kind)
        ? payload.kind || rawDetail.kind
        : {
            401: "authentication_required",
            413: "request_too_large",
            429: "capacity_exceeded",
          }[response.status] || "unknown";
    throw new ApiError(
      `${response.status} ${detail}`,
      kind,
      response.status,
      detail,
    );
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

const markdownRenderer = markdownit({ html: false, breaks: true, linkify: false });
markdownRenderer.disable("image");
markdownRenderer.validateLink = (url) => Boolean(safeExternalUrl(url)) || /^#source-\d+$/.test(url);
markdownRenderer.renderer.rules.text = (tokens, index) => escapeHtml(tokens[index].content)
  .replace(/(?:\[(?:E\d+|\d+)\][ \t]*)+/g, (group) => {
    const seen = new Set();
    const links = [];
    for (const match of group.matchAll(/\[(E\d+|\d+)\]/g)) {
      const key = match[1];
      const target = citationLinks[key];
      if (!target) { links.push(match[0]); continue; }
      if (seen.has(target)) continue;
      seen.add(target);
      const number = /^E/.test(key) ? Object.keys(citationLinks).find(k => /^\d+$/.test(k) && citationLinks[k] === target) : key;
      links.push(`<a class="citation-link" href="#${target}" data-citation="${target}">[${number || key}]</a>`);
    }
    return links.join(" ") + (group.match(/[ \t]+$/)?.[0] || "");
  });
markdownRenderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noreferrer");
  return self.renderToken(tokens, index, options);
};

function inline(text) {
  return DOMPurify.sanitize(markdownRenderer.renderInline(String(text)), { USE_PROFILES: { html: true } });
}

function renderMarkdown(markdown) {
  return DOMPurify.sanitize(markdownRenderer.render(String(markdown || "")), { USE_PROFILES: { html: true } });
}

function statusMeta(status) {
  return (
    STATUS[status] || {
      label: status || "未知",
      title: "任务状态未知",
      kind: "failed",
    }
  );
}

function friendlyError(errorOrKind) {
  const kind =
    typeof errorOrKind === "string"
      ? errorOrKind
      : errorOrKind?.kind || "unknown";
  const desktopMessages = {
    llm_config: ["AI 服务需要检查", "打开设置，检查 API Key 或服务地址，再测试并保存。"],
    llm: ["模型连接失败", "稍后重试，或打开设置测试连接。"],
    token_budget: ["本次研究达到资源上限", "缩小问题范围，或在高级设置中调整研究预算。"],
    storage_unavailable: ["暂时无法保存数据", "请检查磁盘空间，重试或打开诊断查看原因。"],
    persistence_unavailable: ["暂时无法保存数据", "请检查磁盘空间，重试或打开诊断查看原因。"],
    persistence_corruption: ["已有数据无法读取", "请打开诊断查看原因，已有文件会保留。"],
    unknown: ["操作未能完成", "请重试，或在设置中查看诊断。"],
  };
  const [title, message] = (desktopMode && desktopMessages[kind]) || ERROR_MESSAGES[kind] || ERROR_MESSAGES.unknown;
  const detail =
    typeof errorOrKind === "string"
      ? errorOrKind
      : errorOrKind?.detail ||
        errorOrKind?.message ||
        String(errorOrKind || "");
  return { kind, title, message, detail };
}

function showError(errorOrKind, extraDetail = "") {
  validationErrorField = null;
  const message = friendlyError(errorOrKind);
  el("errorTitle").textContent = message.title;
  el("errorMessage").textContent = message.message;
  el("errorDetails").textContent = [message.detail, extraDetail]
    .filter(Boolean)
    .join("\n");
  el("errorPanel").classList.remove("hidden");
}

function clearError() {
  validationErrorField = null;
  el("errorPanel").classList.add("hidden");
  el("errorDetails").textContent = "";
}

function setBusy(busy) {
  el("run").disabled = busy || !privateApiReady || !providerReady;
  el("run").innerHTML =
    `${busy ? "研究进行中" : "开始研究"}${icon("arrow-up-right")}`;
  el("cancelResearch").classList.toggle(
    "hidden",
    !busy || !currentTaskId || taskLoading,
  );
}

function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function renderHealth(health) {
  desktopMode = Boolean(health.desktop_mode);
  providerReady = Boolean(health.provider_ready);
  const warning = health.status !== "ok" || !providerReady;
  el("badges").innerHTML =
    `<span class="system-pill ${warning ? "warning" : ""}">${warning ? "需要配置" : "服务正常"}</span>`;
  el("footerInfo").textContent =
    `v${health.version} · ${health.provider === "mock" ? "离线演示" : health.model}`;
  if (health.auth_required) {
    el("accessTokenGroup").classList.remove("hidden");
    el("accessStatus").textContent = "此服务配置需要访问令牌。";
  }
}

function renderConfig(config) {
  maxImportBytes = config.max_request_body_bytes || 1048576;
  el("model").value = config.default_model;
  el("temperature").value = config.temperature;
  el("presence").value = config.presence_penalty;
  el("frequency").value = config.frequency_penalty;
  el("topk").value = config.top_k;
  el("iterations").value = config.max_iterations;
  el("maxtokens").value = config.max_tokens;
  const credential =
    config.provider === "mock"
      ? "演示模式无需 API Key"
      : config.api_key_configured
        ? "API Key 已由管理员配置"
        : "API Key 尚未配置";
  el("modelStatus").innerHTML = [
    `<span class="model-chip">服务商 <strong>${escapeHtml(config.provider)}</strong></span>`,
    `<span class="model-chip">模型 <strong>${escapeHtml(config.default_model)}</strong></span>`,
    `<span class="model-chip">凭据 <strong>${escapeHtml(credential)}</strong></span>`,
  ].join("");
  el("settingsSummary").textContent =
    config.provider === "mock" ? "离线演示模式" : "服务配置";
  el("timeoutSetting").textContent =
    `${Number(config.research_task_timeout_s)} 秒`;
  el("budgetSetting").textContent =
    `${Number(config.token_budget).toLocaleString()} tokens`;
  el("configHint").textContent =
    `embedding=${config.embedding_provider} · mcp=${config.mcp_transport} · web=${config.web_search_mode} · ` +
    `token budget=${config.token_budget} · concurrency=${config.max_concurrent_tasks}`;
}

function metric(label, value) {
  return `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`;
}

function renderMetrics(metrics) {
  if (!metrics) {
    el("metrics").innerHTML =
      '<div class="empty-state small">暂无运行数据。</div>';
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
    el("timeline").innerHTML =
      '<div class="empty-state small">暂无执行详情。</div>';
    return;
  }
  const children = new Map();
  trace.spans.forEach((span) => {
    const key = span.parent_id || "root";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(span);
  });
  const walk = (parentId, depth) =>
    (children.get(parentId) || [])
      .map((span) => {
        const tokens = span.usage?.total_tokens
          ? ` · ${span.usage.total_tokens} tokens`
          : "";
        const error = span.error
          ? `<div class="span-error">${escapeHtml(span.error)}</div>`
          : "";
        return `<div class="span kind-${escapeHtml(span.kind)}" style="margin-left:${depth * 16}px">
      <div class="span-head"><strong>${escapeHtml(span.name)}</strong><span class="span-meta">${Number(span.latency_ms || 0).toFixed(0)} ms${tokens}</span></div>
      ${error}</div>${walk(span.span_id, depth + 1)}`;
      })
      .join("");
  el("timeline").innerHTML = walk("root", 0);
}

function renderSources(sources, evidence = []) {
  const items = sources || [];
  el("sourceCount").textContent = String(sourceGroups(items).length);
  if (!items.length) {
    el("sources").innerHTML = emptyState(
      "book-open",
      "这次研究没有可展示的来源",
      "报告中的信息以可验证内容为准。",
    );
    return;
  }
  el("sources").innerHTML = sourceGroups(items)
    .sort(
      (a, b) =>
        (sourceNumbers[a.source.id] || Infinity) -
        (sourceNumbers[b.source.id] || Infinity),
    )
    .map(({ source, index, ids }) => {
      const url = safeExternalUrl(source.url);
      const bundledWeb = desktopMode && source.kind === "web";
      const title = escapeHtml(
        source.title || source.locator || `来源 ${index + 1}`,
      );
      const heading = url && !bundledWeb
        ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${title} ${icon("arrow-up-right")}</a>`
        : `<strong>${title}</strong>`;
      const domain = url
        ? new URL(url).hostname.replace(/^www\./, "")
        : "个人资料";
      const number = sourceNumbers[source.id];
      const uniqueQuotes = new Map();
      evidence.filter(item => ids.includes(item.source_id) && item.quote).forEach(item => {
        const key = item.quote.normalize("NFKC").replace(/\s+/g, " ").trim();
        if (!uniqueQuotes.has(key)) uniqueQuotes.set(key, item.quote);
      });
      const quotes = Array.from(uniqueQuotes.values());
      const excerpt = quotes.length ? `<details class="source-excerpt"><summary>查看引用原文</summary>${quotes.map(quote => `<blockquote>${escapeHtml(quote)}</blockquote>`).join("")}</details>` : "";
      return `<div class="source-item" id="source-${index + 1}"><span class="source-number">${number ? `[${number}]` : icon(url ? "globe" : "file-text")}</span><div class="source-body">${heading}<div class="source-meta">${icon(url ? "globe" : "file-text")}${bundledWeb ? "内置网页样本 · " : ""}${escapeHtml(domain)}${source.retrieved_at ? ` · ${formatDate(source.retrieved_at)}` : ""}</div>${bundledWeb ? '<span class="source-location">来自内置资料，未进行联网核验</span>' : !url ? '<span class="source-location">来自我的资料</span>' : ""}${excerpt}</div></div>`;
    })
    .join("");
}

function renderReport(report) {
  if (!report) {
    currentReportMarkdown = "";
    el("report").innerHTML =
      '<div class="empty-state">这次研究没有生成报告。</div>';
    return;
  }
  currentReportMarkdown = report.markdown || "";
  if (currentReportMarkdown) {
    const scope = currentResult?.use_examples ? "示例资料（体验报告）" : "本次选定的个人资料";
    currentReportMarkdown = `> 资料范围：${scope}，不包含实时联网搜索。\n\n` + currentReportMarkdown;
  }
  const title = report.title || "最终报告";
  el("resultTitle").textContent =
    title.length > 140 ? `${title.slice(0, 140)}…` : title;
  el("report").innerHTML = renderMarkdown(
    (currentReportMarkdown || report.executive_summary || "暂无报告内容。").split(/\n## (?:证据附录|引用原文)/)[0],
  );
}

function renderHistory(runs) {
  historyRuns = runs || [];
  const query = el("historySearch").value.trim().toLowerCase();
  const filtered = historyRuns.filter((run) =>
    String(run.question).toLowerCase().includes(query),
  );
  const row = (run) => {
    const state = statusMeta(run.status);
    return `<button class="history-item ${run.task_id === currentTaskId ? "active" : ""}" type="button" data-history-task="${escapeHtml(run.task_id)}"><span class="history-icon">${icon("file-text")}</span><span class="history-body"><span class="history-question">${escapeHtml(run.question)}</span><span class="history-meta"><span class="history-status ${state.kind}">${state.label}</span><span>${formatDate(run.completed_at || run.created_at)}</span>${run.citations ? `<span>${Number(run.citations)} 个来源</span>` : ""}${run.quality === "degraded" ? '<span class="status-pill partial">部分结果</span>' : ""}</span></span>${icon("chevron-right")}</button>`;
  };
  const empty = emptyState(
    "history",
    "还没有研究记录",
    "从首页输入一个问题，开始第一次研究。",
    '<a href="#research">开始研究</a>',
  );
  el("history").innerHTML = historyRuns.length
    ? filtered.length
      ? filtered.map(row).join("")
      : emptyState("search", "没有匹配的研究", "试试其他关键词。")
    : empty;
  el("recentHistory").innerHTML = historyRuns.length
    ? historyRuns.slice(0, 3).map(row).join("")
    : empty;
  el("historyCount").textContent = query
    ? `${filtered.length} / ${historyRuns.length} 项研究`
    : `最近 ${historyRuns.length} 项研究`;
  el("historyNavCount").textContent = historyRuns.length || "";
}

function renderKbStats(stats) {
  el("kbStats").innerHTML =
    `<strong>${Number(stats?.documents || 0)}</strong> 份资料`;
}

function renderDocumentSelection() {
  const available = knowledgeDocuments.filter(doc => Boolean(doc.metadata?.example) === exampleMode);
  if (selectedDocumentIds === null) selectedDocumentIds = new Set(available.map(doc => doc.doc_id));
  else if (!parentResearch) selectedDocumentIds = new Set(Array.from(selectedDocumentIds).filter(id => available.some(doc => doc.doc_id === id)));
  el("documentSelection").innerHTML = available.length ? available.map(doc =>
    `<label class="document-choice"><input type="checkbox" data-document="${escapeHtml(doc.doc_id)}" ${selectedDocumentIds.has(doc.doc_id) ? "checked" : ""} ${parentResearch ? "disabled" : ""}/> ${escapeHtml(doc.title)}</label>`
  ).join("") : '<p class="muted">先在“我的资料”导入文件，或载入示例体验。</p>';
  el("scopeLabel").textContent = exampleMode ? "示例体验：仅使用示例资料" : "本次研究的资料";
  el("exitExamples").classList.toggle("hidden", !exampleMode);
  el("followupNotice").classList.toggle("hidden", !parentResearch);
  el("followupNotice").textContent = parentResearch ? `继续追问：${parentResearch.question}（继承报告、证据和资料范围）` : "";
}

async function startExamples() {
  await api("/kb/examples", { method: "POST" });
  parentResearch = null;
  exampleMode = true;
  selectedDocumentIds = null;
  await loadKnowledgeBase();
  el("question").value = SAMPLES[0];
  el("questionCount").textContent = `${el("question").value.length} / 2000`;
  toast("示例体验已准备好；连接模型后会使用你的账户用量。");
}

function renderKbDocuments(documents) {
  knowledgeDocuments = documents || [];
  renderDocumentSelection();
  if (!documents || !documents.length) {
    el("kbDocuments").innerHTML = emptyState(
      "book-open",
      "我的资料目前为空",
      "导入自己的资料，然后在研究页选择使用范围。",
    );
    return;
  }
  el("kbDocuments").innerHTML = documents
    .map(
      (doc) =>
        `<div class="document-item"><span class="document-icon">${icon("file-text")}</span><div class="document-body"><strong>${escapeHtml(doc.title)}</strong><div class="meta">${{ web: "网页", document: "文档", knowledge_base: "个人资料", mcp: "关联资料" }[doc.kind] || "个人资料"}${doc.created_at ? ` · ${formatDate(doc.created_at)} 添加` : ""}</div><details><summary>资料详情</summary><p>${Number(doc.chunks || 0)} 个内容片段 · ${escapeHtml(doc.source || "个人资料")}</p></details></div><span class="status-pill completed">可供研究</span><button class="link-button" type="button" aria-label="删除资料：${escapeHtml(doc.title)}" data-delete-doc="${escapeHtml(doc.doc_id)}">删除资料</button></div>`,
    )
    .join("");
}

function renderKbSearch(result) {
  if (!result?.hits?.length) {
    el("kbResults").innerHTML =
      '<div class="empty-state small">没有找到相关资料，试试更换关键词。</div>';
    return;
  }
  el("kbResults").innerHTML = result.hits
    .map(
      (hit) => `<div class="kb-hit">
    <strong>${escapeHtml(hit.title)}</strong>
    <p>${escapeHtml((hit.content || "").slice(0, 220))}${(hit.content || "").length > 220 ? "…" : ""}</p>
  </div>`,
    )
    .join("");
}

function renderEvaluation(payload) {
  if (!payload || payload.available === false) {
    el("evaluation").innerHTML = '<p class="muted">暂无离线评估结果。</p>';
    return;
  }
  const metrics = payload.metrics || {};
  el("evaluation").innerHTML =
    `<p class="muted">离线评估：${metrics.tasks ?? 0} 个任务 · 成功率 ${Math.round(Number(metrics.task_success_rate || 0) * 100)}%</p>`;
}

function renderMcp(tools) {
  const names = (tools?.tools || []).map((tool) => escapeHtml(tool.name));
  el("mcpTools").innerHTML =
    `<p class="muted">可用工具：${names.join("、") || "无"}</p>`;
}

function updateProgress(result, trace) {
  const state = statusMeta(result.status);
  const terminal = !["pending", "running"].includes(result.status);
  el("progressTitle").textContent = state.title;
  el("runStatus").textContent = state.label;
  el("runStatus").className = `status-pill ${state.kind}`;
  el("progressPanel").classList.toggle("terminal", terminal);
  el("progressPanel").classList.toggle("hidden", result.status === "completed");
  el("progressActivity").classList.toggle("hidden", terminal);
  el("retryResearch").classList.toggle(
    "hidden",
    !terminal || result.status === "completed",
  );
  el("progressQuery").textContent = result.question || taskQuestion;
  const activeAgent = (trace?.spans || []).findLast(
    (span) => span.kind === "agent" && !span.end_time,
  );
  const writing = activeAgent && /writer/i.test(activeAgent.name);
  const stages = {
    planning: "正在理解问题并安排研究步骤。",
    retrieving: "正在检索你选定的资料并提取原文。",
    verifying: "正在核对原文、结论含义和资料中的冲突。",
    reviewing: "正在检查还缺少哪些依据。",
    supplementing: "正在针对证据缺口补充检索。",
    writing: "正在根据有依据的内容撰写报告。",
    checking_report: "正在复核报告中的结论与引用。",
  };
  el("progressMessage").textContent = (result.status === "running" && stages[result.stage]) ||
    {
      pending: "任务已提交，正在等待开始。",
      running: writing
        ? "正在根据已收集的证据撰写报告。"
        : "ResearchPilot 正在搜索、分析并整理相关信息。",
      completed: "报告和来源已经准备好。",
      failed: "研究未能完成。可根据上方提示调整问题后重试。",
      cancelled: "研究已停止。你可以调整问题，再次开始。",
      timed_out: "研究已停止。缩小问题范围后再试一次。",
    }[result.status] || "正在更新任务状态。";
}

function renderTask(result, trace) {
  currentResult = result;
  taskLoading = false;
  currentTaskId = result.task_id;
  taskQuestion = result.question || taskQuestion;
  el("resultQuestion").textContent = taskQuestion;
  updateProgress(result, trace);
  const terminal = !["pending", "running"].includes(result.status);
  setBusy(!terminal);
  if (!terminal) {
    el("resultSection").classList.add("hidden");
    return;
  }
  clearTimeout(pollTimer);
  if (result.status === "completed") {
    clearError();
    prepareCitations(result);
    renderReport(result.report);
    renderSources(
      result.evidence?.sources || [],
      (result.evidence?.evidence || []).filter(item =>
        result.verification?.checks?.some(check => check.evidence_id === item.id && check.status === "supported")),
    );
    renderMetrics(result.metrics);
    renderTimeline(trace);
    el("executionDetails").open = false;
    document
      .querySelectorAll("[data-section]")
      .forEach((link) =>
        link.classList.toggle("active", link.dataset.section === "report"),
      );
    const degraded = result.quality === "degraded";
    el("qualityBadge").classList.toggle("hidden", !degraded);
    el("resultMeta").textContent =
      `${formatDate(result.completed_at)} · ${sourceGroups(result.evidence?.sources || []).length} 个来源`;
    el("resultSection").classList.remove("hidden");
    if (degraded) toast("部分信息不可用，报告保留了可验证的结果。");
  } else {
    el("resultSection").classList.add("hidden");
    if (result.status === "cancelled") clearError();
    else
      showError(
        result.status === "timed_out"
          ? "timed_out"
          : result.errors?.some((item) => /llm_config/.test(item))
            ? "llm_config"
            : "unknown",
        (result.errors || []).join("\n"),
      );
  }
}

async function loadTask(taskId) {
  currentTaskId = taskId;
  const version = observationVersion;
  const result = await api(`/research/${encodeURIComponent(taskId)}`);
  if (version !== observationVersion || currentTaskId !== taskId) return null;
  // The API publishes traces at completion, so active polling must not request
  // unavailable traces or infer phases from an unfinished execution record.
  const trace =
    result.status === "completed"
      ? await api(`/research/${encodeURIComponent(taskId)}/trace`).catch(
          () => null,
        )
      : null;
  if (version !== observationVersion || currentTaskId !== taskId) return null;
  renderTask(result, trace);
  return result;
}

function schedulePoll(taskId) {
  clearTimeout(pollTimer);
  const version = observationVersion;
  pollTimer = setTimeout(async () => {
    if (version !== observationVersion || currentTaskId !== taskId) return;
    try {
      const result = await loadTask(taskId);
      if (!result) return;
      pollFailures = 0;
      if (["pending", "running"].includes(result.status)) schedulePoll(taskId);
      else await loadHistory();
    } catch (error) {
      if (version !== observationVersion || currentTaskId !== taskId) return;
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
  const scope = {
    document_ids: Array.from(selectedDocumentIds || []),
    use_examples: exampleMode,
    ...(parentResearch ? { parent_task_id: parentResearch.task_id } : {}),
  };
  if (desktopMode) return { question, mode: "async", ...scope };
  const payload = {
    question,
    mode: "async",
    ...scope,
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
  if (el("run").disabled) return;
  clearError();
  const question = el("question").value.trim();
  if (!privateApiReady) {
    showError("authentication_required");
    navigate("settings");
    return;
  }
  const invalidSetting = RESEARCH_SETTING_IDS
    .map(el)
    .find((input) => input.checkValidity && !input.checkValidity());
  if (invalidSetting) {
    navigate("settings");
    invalidSetting.closest("details")?.setAttribute("open", "");
    showError(new ApiError("研究参数超出允许范围。"));
    validationErrorField = "settings";
    el("errorTitle").textContent = "请调整研究参数";
    el("errorMessage").textContent =
      "按输入框提示填写允许范围内的数值，再开始研究。";
    invalidSetting.focus();
    invalidSetting.reportValidity();
    return;
  }
  if (question.length < 4) {
    showError(new ApiError("请输入至少 4 个字符的研究问题。"));
    validationErrorField = "question";
    el("errorTitle").textContent = "再补充一点背景";
    el("errorMessage").textContent =
      "请输入至少 4 个字符，并说明你希望了解什么。";
    el("question").focus();
    return;
  }
  if (question.length > 2000) {
    showError("request_too_large");
    validationErrorField = "question";
    return;
  }
  if (!selectedDocumentIds?.size) {
    showError(new ApiError("请先导入并勾选至少一份资料。"));
    el("errorTitle").textContent = "选择本次研究的资料";
    el("errorMessage").textContent = "在问题下方勾选资料，或载入示例体验。";
    return;
  }
  stopObservation();
  const version = observationVersion;
  currentTaskId = null;
  taskQuestion = question;
  taskLoading = false;
  el("resultSection").classList.add("hidden");
  updateProgress({ status: "pending", question }, null);
  navigate("task");
  setBusy(true);
  try {
    const submitted = await api("/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(researchPayload(question)),
    });
    if (version !== observationVersion) {
      await loadHistory();
      return;
    }
    currentTaskId = submitted.task_id;
    window.location.hash = `task/${submitted.task_id}`;
    el("cancelResearch").classList.remove("hidden");
    schedulePoll(submitted.task_id);
    await loadHistory();
  } catch (error) {
    if (version !== observationVersion) return;
    setBusy(false);
    el("progressPanel").classList.add("hidden");
    navigate("research");
    showError(error);
  }
}

async function cancelResearch() {
  if (!currentTaskId) return;
  const taskId = currentTaskId;
  const version = observationVersion;
  el("cancelResearch").disabled = true;
  el("cancelResearch").textContent = "正在取消…";
  try {
    const result = await api(`/research/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
    });
    if (version === observationVersion && currentTaskId === taskId)
      renderTask(result, null);
    await loadHistory();
  } catch (error) {
    if (version === observationVersion) showError(error);
  } finally {
    el("cancelResearch").disabled = false;
    el("cancelResearch").textContent = "取消研究";
  }
}

async function loadHistory() {
  try {
    const runs = await api("/research?limit=100");
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
      api("/research?limit=100"),
      desktopMode ? Promise.resolve({ tools: [] }) : api("/mcp/tools").catch(() => ({ tools: [] })),
      desktopMode ? Promise.resolve({ available: false }) : api("/evaluation/latest").catch(() => ({ available: false })),
    ]);
    privateApiReady = true;
    renderConfig(config);
    renderKbStats(documents.stats);
    renderKbDocuments(documents.documents);
    renderHistory(runs);
    renderMcp(tools);
    renderEvaluation(evaluation);
    clearError();
    el("providerNotice").classList.toggle(
      "hidden",
      !desktopMode && providerReady && config.provider !== "mock",
    );
    el("providerNotice").textContent = desktopMode
      ? "研究仅使用本次选定的资料，不包含实时联网搜索。"
      : !providerReady
      ? "开始第一次研究前，请联系管理员完成模型配置。"
      : "当前为离线演示模式：使用内置资料与确定性模型，适合体验研究流程。";
    setBusy(false);
    return true;
  } catch (error) {
    privateApiReady = false;
    setBusy(false);
    if (error.kind === "authentication_required") {
      el("accessTokenGroup").classList.remove("hidden");
      el("providerNotice").textContent =
        "此平台需要访问令牌。打开“设置”，输入管理员提供的令牌后即可开始研究。";
      el("providerNotice").classList.remove("hidden");
    }
    showError(error);
    return false;
  }
}

function resetResearch(prefill = "", parent = null) {
  parentResearch = parent;
  if (parent) {
    exampleMode = Boolean(parent.use_examples);
    selectedDocumentIds = new Set(parent.document_ids || []);
  }
  renderDocumentSelection();
  stopObservation();
  currentTaskId = null;
  renderHistory(historyRuns);
  taskLoading = false;
  currentReportMarkdown = "";
  citationLinks = {};
  clearError();
  el("progressPanel").classList.add("hidden");
  el("resultSection").classList.add("hidden");
  el("question").value =
    typeof prefill === "string" ? prefill.slice(0, 2000) : "";
  el("questionCount").textContent = `${el("question").value.length} / 2000`;
  setBusy(false);
  navigate("research");
  showView("research");
  el("question").focus();
}

async function copyReport() {
  if (!currentReportMarkdown) return;
  try {
    await navigator.clipboard.writeText(currentReportMarkdown);
    toast("报告已复制，可粘贴为 Markdown。");
  } catch {
    toast("浏览器暂不允许复制，请选择报告正文手动复制。");
  }
}

function bindEvents() {
  if (typeof bindProductEvents === "function") bindProductEvents();
  document.querySelectorAll("[data-icon]").forEach((node) => {
    node.innerHTML = icon(node.dataset.icon);
  });
  window.addEventListener("hashchange", handleRoute);
  el("sidebarNew").addEventListener("click", () => resetResearch());
  el("dismissError").addEventListener("click", clearError);
  el("retryResearch").addEventListener("click", () =>
    resetResearch(taskQuestion),
  );
  el("followupResearch").addEventListener("click", () => resetResearch("", currentResult));
  el("documentSelection").addEventListener("change", event => {
    const id = event.target.dataset.document;
    if (!id) return;
    if (event.target.checked) selectedDocumentIds.add(id);
    else selectedDocumentIds.delete(id);
  });
  el("loadExamples").addEventListener("click", () => startExamples().catch(showError));
  el("exitExamples").addEventListener("click", () => {
    exampleMode = false;
    selectedDocumentIds = null;
    resetResearch();
  });
  el("historySearch").addEventListener("input", () =>
    renderHistory(historyRuns),
  );
  el("openKnowledgeDialog").addEventListener("click", () => {
    el("kbDialogFeedback").textContent = "";
    el("knowledgeDialog").showModal();
  });
  ["closeKnowledgeDialog", "cancelKnowledgeDialog"].forEach((id) =>
    el(id).addEventListener("click", () => el("knowledgeDialog").close()),
  );
  el("kbQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") el("kbSearch").click();
  });
  el("kbQuery").addEventListener("input", () => {
    if (!el("kbQuery").value) el("kbResults").classList.add("hidden");
  });
  el("resultSection").addEventListener("click", (event) => {
    const link = event.target.closest("[data-section], [data-citation]");
    if (!link) return;
    event.preventDefault();
    const target = el(link.dataset.section || link.dataset.citation);
    if (link.dataset.section === "executionDetails") target.open = true;
    if (link.dataset.section)
      document
        .querySelectorAll("[data-section]")
        .forEach((item) => item.classList.toggle("active", item === link));
    target?.scrollIntoView({ behavior: "auto", block: "start" });
  });
  el("question").addEventListener("input", () => {
    el("questionCount").textContent = `${el("question").value.length} / 2000`;
    if (validationErrorField === "question" && el("question").value.trim().length >= 4)
      clearError();
  });
  RESEARCH_SETTING_IDS.forEach((id) =>
    el(id).addEventListener("input", () => {
      if (
        validationErrorField === "settings" &&
        RESEARCH_SETTING_IDS.map(el).every((input) => input.checkValidity())
      ) clearError();
    }),
  );
  el("question").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter")
      runResearch();
  });
  el("samples").innerHTML = SAMPLES.map(
    (sample, index) =>
      `<button data-sample="${index}" type="button"><span class="sample-label">${icon(["git-compare-arrows", "shield-check", "lightbulb"][index])}${["方案比较", "风险洞察", "技术决策"][index]}</span><span class="sample-copy">${escapeHtml(sample)}</span></button>`,
  ).join("");
  el("samples").addEventListener("click", (event) => {
    const index = event.target?.closest?.("[data-sample]")?.dataset?.sample;
    if (index === undefined) return;
    el("question").value = SAMPLES[Number(index)];
    el("questionCount").textContent = `${el("question").value.length} / 2000`;
    el("question").focus();
  });
  el("run").addEventListener("click", runResearch);
  el("cancelResearch").addEventListener("click", cancelResearch);
  el("newResearch").addEventListener("click", () => resetResearch());
  el("copyReport").addEventListener("click", copyReport);
  el("refreshHistory").addEventListener("click", loadHistory);
  ["history", "recentHistory"].forEach((id) =>
    el(id).addEventListener("click", (event) => {
      const taskId = event.target?.closest?.("[data-history-task]")?.dataset
        ?.historyTask;
      if (taskId) {
        if (window.location.hash === `#task/${taskId}`) openTask(taskId);
        else window.location.hash = `task/${taskId}`;
      }
    }),
  );
  el("applyAccessToken").addEventListener("click", async () => {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.setItem(
        "researchpilot.accessToken",
        el("accessToken").value.trim(),
      );
    }
    const connected = await loadPrivateData();
    el("applyAccessToken").textContent = connected ? "已连接" : "重新连接";
  });
  el("kbSearch").addEventListener("click", async () => {
    const query = el("kbQuery").value.trim();
    el("kbResults").classList.remove("hidden");
    if (query.length < 2) {
      el("kbResults").innerHTML =
        '<div class="empty-state small">请输入至少两个字符。</div>';
      return;
    }
    el("kbSearch").disabled = true;
    el("kbResults").innerHTML =
      '<div class="skeleton-row" aria-label="正在搜索资料"></div>';
    try {
      const result = await api(
        `/kb/search?q=${encodeURIComponent(query)}&strategy=${el("kbStrategy").value}`,
      );
      renderKbSearch(result);
    } catch (error) {
      el("kbResults").innerHTML =
        `<div class="empty-state small">${escapeHtml(friendlyError(error).message)}</div>`;
    } finally {
      el("kbSearch").disabled = false;
    }
  });
  el("knowledgeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = el("kbTitle").value.trim();
    const content = el("kbContent").value.trim();
    if (title.length < 2 || content.length < 20) {
      el("kbDialogFeedback").textContent = "请填写标题和至少 20 个字符的内容。";
      return;
    }
    el("kbAdd").disabled = true;
    el("kbDialogFeedback").textContent = "正在添加…";
    try {
      await api("/kb/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, content, source: "web-ui://inline" }),
      });
      el("kbTitle").value = "";
      el("kbContent").value = "";
      el("kbDialogFeedback").textContent = "";
      el("knowledgeDialog").close();
      toast("资料已加入我的资料。");
      await loadKnowledgeBase();
    } catch (error) {
      el("kbDialogFeedback").textContent = friendlyError(error).message;
    } finally {
      el("kbAdd").disabled = false;
    }
  });
  el("kbDocuments").addEventListener("click", async (event) => {
    const button = event.target?.closest?.("[data-delete-doc]");
    const docId = button?.dataset?.deleteDoc;
    if (!docId) return;
    if (
      typeof window.confirm === "function" &&
      !window.confirm("确定删除这份个人资料吗？")
    )
      return;
    button.disabled = true;
    try {
      await api(`/kb/documents/${encodeURIComponent(docId)}`, {
        method: "DELETE",
      });
      el("kbFeedback").textContent = "资料已删除。";
      await loadKnowledgeBase();
    } catch (error) {
      el("kbFeedback").textContent = friendlyError(error).message;
      button.disabled = false;
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
  const savedToken =
    typeof sessionStorage === "undefined"
      ? ""
      : sessionStorage.getItem("researchpilot.accessToken") || "";
  el("accessToken").value = savedToken;
  bindEvents();
  showView(
    ["history", "knowledge", "settings"].includes(window.location.hash.slice(1))
      ? window.location.hash.slice(1)
      : "research",
  );
  setBusy(true);
  try {
    const health = await api("/health");
    renderHealth(health);
    await loadPrivateData();
    await handleRoute();
    if (desktopMode) await initializeDesktop();
  } catch (error) {
    privateApiReady = false;
    setBusy(false);
    showError(error);
    el("history").innerHTML =
      '<div class="empty-state small">连接服务后会显示研究记录。</div>';
  }
}

document.addEventListener("DOMContentLoaded", boot);
