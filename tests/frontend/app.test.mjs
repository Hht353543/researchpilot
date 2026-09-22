/**
 * Frontend logic tests (node:test + node:vm, no npm dependencies).
 *
 * app.js is a classic browser script, so it is loaded into a VM context with a
 * minimal DOM stub. This covers the rendering logic that the pytest suite can
 * only check structurally.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.resolve(here, "../../researchpilot/api/static/app.js");

function loadApp(fetchImpl) {
  const nodes = new Map();
  const requests = [];
  const makeNode = (id) => {
    const classes = new Set();
    return {
      id,
      innerHTML: "",
      textContent: "",
      className: "",
      value: "",
      disabled: false,
      open: false,
      dataset: {},
      classList: {
        add(...names) { names.forEach((name) => classes.add(name)); },
        remove(...names) { names.forEach((name) => classes.delete(name)); },
        toggle(name, force) { if (force === undefined ? !classes.has(name) : force) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
      addEventListener() {},
      focus() {},
      scrollIntoView() {},
    };
  };
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, makeNode(id));
      return nodes.get(id);
    },
    addEventListener() {},
  };
  const sandbox = {
    document,
    console,
    window: { scrollTo() {}, confirm: () => true },
    URL,
    Intl,
    setTimeout,
    clearTimeout,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: async (requestPath, options) => {
      requests.push({ path: requestPath, options });
      if (fetchImpl) return fetchImpl(requestPath, options);
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const code = fs.readFileSync(appPath, "utf8");
  vm.runInContext(
    `${code}
     globalThis.__app = { api, renderMarkdown, renderTimeline, renderMetrics, renderKbDocuments,
                           renderSources, renderEvaluation, renderHistory, renderReport,
                           friendlyError, statusMeta, escapeHtml, inline };`,
    sandbox,
  );
  return { app: sandbox.__app, nodes, requests };
}

test("api sends the configured access token as a Bearer credential", async () => {
  const { app, nodes, requests } = loadApp();
  nodes.set("accessToken", { value: "shared-token", addEventListener() {} });

  await app.api("/config");

  assert.equal(requests[0].options.headers.Authorization, "Bearer shared-token");
});

test("api preserves backend error kind for product error mapping", async () => {
  const { app } = loadApp(async () => ({
    ok: false,
    status: 401,
    statusText: "Unauthorized",
    text: async () => JSON.stringify({ kind: "authentication_required", detail: "token rejected" }),
  }));

  await assert.rejects(
    app.api("/config"),
    (error) => error.kind === "authentication_required" && error.detail === "token rejected",
  );
});

test("escapeHtml neutralises tags and quotes", () => {
  const { app } = loadApp();
  const escaped = app.escapeHtml("<script>alert('x')</script>");
  assert.ok(!escaped.includes("<script>"), escaped);
  assert.match(escaped, /&lt;script&gt;/);
});

test("renderMarkdown renders headings, lists, tables and inline code", () => {
  const { app } = loadApp();
  const html = app.renderMarkdown(
    [
      "# 标题",
      "",
      "**加粗** 与 `code`",
      "",
      "- 第一项",
      "- 第二项",
      "",
      "| a | b |",
      "| --- | --- |",
      "| 1 | 2 |",
      "",
      "> 引用",
    ].join("\n"),
  );
  assert.match(html, /<h1>标题<\/h1>/);
  assert.match(html, /<strong>加粗<\/strong>/);
  assert.match(html, /<code>code<\/code>/);
  assert.match(html, /<li>第一项<\/li>/);
  assert.match(html, /<table>/);
  assert.match(html, /<th>a<\/th>/);
  assert.match(html, /<blockquote>引用<\/blockquote>/);
});

test("renderMarkdown escapes hostile report content", () => {
  const { app } = loadApp();
  const html = app.renderMarkdown("正常文本 <img src=x onerror=alert(1)> 结束");
  assert.ok(!html.includes("<img"), html);
  assert.match(html, /&lt;img/);
});

test("renderTimeline builds the agent tree with latency and tokens", () => {
  const { app, nodes } = loadApp();
  app.renderTimeline({
    spans: [
      { span_id: "s1", parent_id: null, name: "PlannerAgent", kind: "agent", latency_ms: 12.5, usage: { total_tokens: 100 }, output: {} },
      { span_id: "s2", parent_id: "s1", name: "PlannerAgent.llm[planner]", kind: "llm", latency_ms: 9, usage: { total_tokens: 80 }, model: "gpt-4o-mini" },
      { span_id: "s3", parent_id: "s1", name: "tool.knowledge_search", kind: "tool", tool: "knowledge_search", latency_ms: 4, error: "boom", usage: {} },
    ],
    metrics: { tool_calls: 1, llm_calls: 1, usage: { total_tokens: 180 } },
  });
  const html = nodes.get("timeline").innerHTML;
  assert.match(html, /PlannerAgent/);
  assert.match(html, /tool\.knowledge_search/);
  assert.match(html, /12 ms|13 ms/, html); // latency rendered
  assert.match(html, /100 tokens/);
  assert.match(html, /boom/); // error surfaced
});

test("renderTimeline handles a missing trace", () => {
  const { app, nodes } = loadApp();
  app.renderTimeline(null);
  assert.match(nodes.get("timeline").innerHTML, /暂无执行详情/);
});

test("renderMetrics renders tiles and tolerates undefined input", () => {
  const { app, nodes } = loadApp();
  app.renderMetrics({ latency_ms: 2500, llm_calls: 7, tool_calls: 3, tool_failures: 1, retrieval_calls: 2, mcp_calls: 1, retries: 0, iterations: 2, usage: { total_tokens: 1234, cost_usd: 0.0012 } });
  const html = nodes.get("metrics").innerHTML;
  assert.match(html, /2\.5 秒/);
  assert.match(html, /1,234/);
  app.renderMetrics(null);
  assert.match(nodes.get("metrics").innerHTML, /暂无运行数据/);
});

test("renderKbDocuments escapes ids and offers delete buttons", () => {
  const { app, nodes } = loadApp();
  app.renderKbDocuments([{ doc_id: "d<x>", title: "标题", chunks: 3, source: "s" }]);
  const html = nodes.get("kbDocuments").innerHTML;
  assert.match(html, /data-delete-doc="d&lt;x&gt;"/);
  assert.match(html, /删除资料/);
});

test("renderSources presents readable links and handles the empty case", () => {
  const { app, nodes } = loadApp();
  app.renderSources([{ id: "web#1", kind: "web", title: "官方资料", score: 0.42, url: "https://example.com/source" }]);
  assert.match(nodes.get("sources").innerHTML, /官方资料/);
  assert.match(nodes.get("sources").innerHTML, /https:\/\/example\.com\/source/);
  assert.equal(nodes.get("sourceCount").textContent, "1");
  app.renderSources([]);
  assert.match(nodes.get("sources").innerHTML, /没有可展示的来源/);
});

test("renderEvaluation keeps benchmark information in developer language", () => {
  const { app, nodes } = loadApp();
  app.renderEvaluation({
    available: true,
    provider: "mock",
    generated_at: "2026-09-13T00:00:00Z",
    metrics: { tasks: 35, task_success_rate: 1.0, retrieval_recall: 0.939, citation_correctness: 1.0, tool_selection_f1: 0.859, avg_latency_s: 0.31 },
    categories: [{ category: "rag", passed: 3, tasks: 3, task_success_rate: 1.0, avg_latency_s: 0.05, avg_tokens: 13266, citation_correctness: 1.0 }],
  });
  const html = nodes.get("evaluation").innerHTML;
  assert.match(html, /35 个任务/);
  assert.match(html, /成功率 100%/);
  app.renderEvaluation({ available: false, message: "no benchmark" });
  assert.match(nodes.get("evaluation").innerHTML, /暂无离线评估/);
});

test("lifecycle status and known failures use product language", () => {
  const { app } = loadApp();
  assert.equal(app.statusMeta("pending").label, "准备中");
  assert.equal(app.statusMeta("running").label, "研究中");
  assert.equal(app.statusMeta("cancelled").label, "已取消");
  assert.equal(app.statusMeta("timed_out").label, "已超时");
  assert.match(app.friendlyError("llm_config").message, /联系管理员/);
  assert.match(app.friendlyError("capacity_exceeded").message, /稍后再试/);
  assert.match(app.friendlyError("persistence_unavailable").message, /联系管理员/);
  assert.match(app.friendlyError("authentication_required").message, /访问令牌/);
  assert.match(app.friendlyError("cancelled").message, /重新开始研究/);
  assert.match(app.friendlyError("timed_out").message, /缩小问题范围/);
});

test("history renders query, natural status and an empty state", () => {
  const { app, nodes } = loadApp();
  app.renderHistory([{ task_id: "task_1", question: "比较两个技术方案", status: "completed", created_at: "2026-09-22T10:00:00Z" }]);
  assert.match(nodes.get("history").innerHTML, /比较两个技术方案/);
  assert.match(nodes.get("history").innerHTML, /已完成/);
  assert.match(nodes.get("history").innerHTML, /data-history-task="task_1"/);
  app.renderHistory([]);
  assert.match(nodes.get("history").innerHTML, /还没有研究记录/);
});

test("report rendering keeps the readable markdown and title", () => {
  const { app, nodes } = loadApp();
  app.renderReport({ title: "行业分析", markdown: "# 行业分析\n\n## 结论\n\n值得继续关注。" });
  assert.equal(nodes.get("resultTitle").textContent, "行业分析");
  assert.match(nodes.get("report").innerHTML, /<h1>行业分析<\/h1>/);
  assert.match(nodes.get("report").innerHTML, /值得继续关注/);
});
