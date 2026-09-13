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

function loadApp() {
  const nodes = new Map();
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) {
        nodes.set(id, { id, innerHTML: "", textContent: "", className: "", value: "", addEventListener() {} });
      }
      return nodes.get(id);
    },
    addEventListener() {},
  };
  const sandbox = {
    document,
    console,
    window: {},
    fetch: async () => ({ ok: true, status: 200, text: async () => "{}", json: async () => ({}) }),
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const code = fs.readFileSync(appPath, "utf8");
  vm.runInContext(
    `${code}
     globalThis.__app = { renderMarkdown, renderTimeline, renderMetrics, renderKbDocuments,
                           renderSources, renderEvaluation, escapeHtml, inline };`,
    sandbox,
  );
  return { app: sandbox.__app, nodes };
}

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
  assert.match(html, /180/); // token chip
  assert.match(html, /boom/); // error surfaced
});

test("renderTimeline handles a missing trace", () => {
  const { app, nodes } = loadApp();
  app.renderTimeline(null);
  assert.match(nodes.get("timeline").innerHTML, /暂无 Trace/);
});

test("renderMetrics renders tiles and tolerates undefined input", () => {
  const { app, nodes } = loadApp();
  app.renderMetrics({ latency_ms: 2500, llm_calls: 7, tool_calls: 3, tool_failures: 1, retrieval_calls: 2, mcp_calls: 1, retries: 0, iterations: 2, usage: { total_tokens: 1234, cost_usd: 0.0012 } });
  const html = nodes.get("metrics").innerHTML;
  assert.match(html, /2\.50s/);
  assert.match(html, /1,234/);
  assert.match(html, /\$0\.001200/);
  app.renderMetrics(null);
  assert.match(nodes.get("metrics").innerHTML, /运行一次研究/);
});

test("renderKbDocuments escapes ids and offers delete buttons", () => {
  const { app, nodes } = loadApp();
  app.renderKbDocuments([{ doc_id: "d<x>", title: "标题", chunks: 3, source: "s" }]);
  const html = nodes.get("kbDocuments").innerHTML;
  assert.match(html, /data-delete-doc="d&lt;x&gt;"/);
  assert.match(html, /delete/);
});

test("renderSources lists source metadata and handles the empty case", () => {
  const { app, nodes } = loadApp();
  app.renderSources([{ id: "kb#c0", kind: "knowledge_base", title: "文档", score: 0.42, url: "u" }]);
  assert.match(nodes.get("sources").innerHTML, /kb#c0/);
  app.renderSources([]);
  assert.match(nodes.get("sources").innerHTML, /暂无来源/);
});

test("renderEvaluation shows benchmark metrics and the offline caveat", () => {
  const { app, nodes } = loadApp();
  app.renderEvaluation({
    available: true,
    provider: "mock",
    generated_at: "2026-09-13T00:00:00Z",
    metrics: { tasks: 35, task_success_rate: 1.0, retrieval_recall: 0.939, citation_correctness: 1.0, tool_selection_f1: 0.859, avg_latency_s: 0.31 },
    categories: [{ category: "rag", passed: 3, tasks: 3, task_success_rate: 1.0, avg_latency_s: 0.05, avg_tokens: 13266, citation_correctness: 1.0 }],
  });
  const html = nodes.get("evaluation").innerHTML;
  assert.match(html, /100\.0%/);
  assert.match(html, /93\.9%/);
  assert.match(html, /不代表模型质量/);
  app.renderEvaluation({ available: false, message: "no benchmark" });
  assert.match(nodes.get("evaluation").innerHTML, /no benchmark/);
});
