/* Desktop product flow and browser file import; credentials remain transient in the form. */
let desktopSettings = null;

function renderConnectionGuide(service) {
  const links = {
    deepseek: ["DeepSeek", "https://platform.deepseek.com/api_keys", "https://api-docs.deepseek.com/quick_start/pricing"],
    openai: ["OpenAI", "https://platform.openai.com/api-keys", "https://openai.com/api/pricing/"],
  }[service];
  el("createKeyLink").classList.toggle("hidden", !links);
  el("pricingLink").classList.toggle("hidden", !links);
  if (links) {
    el("createKeyLink").href = links[1];
    el("createKeyLink").textContent = `获取 ${links[0]} API Key`;
    el("pricingLink").href = links[2];
  }
}

function productError(error) {
  return error.kind === "storage_unavailable" || error.kind === "persistence_unavailable"
    ? friendlyError(error).message : error.detail || error.message || friendlyError(error).message;
}

function connectionPayload() {
  const key = el("connectionKey").value.trim();
  return {
    service: el("connectionService").value,
    base_url: el("connectionUrl").value.trim(),
    model: el("connectionModel").value.trim(),
    ...(key ? { api_key: key } : {}),
    temperature: Number(el("temperature").value),
    presence_penalty: Number(el("presence").value),
    frequency_penalty: Number(el("frequency").value),
    max_tokens: Number(el("maxtokens").value),
    top_k: Number(el("topk").value),
    max_iterations: Number(el("iterations").value),
    research_task_timeout_s: Number(el("desktopTimeout").value),
    token_budget_live: Number(el("desktopBudget").value),
  };
}

function renderDesktopSettings(settings) {
  desktopSettings = settings;
  renderConnectionGuide(settings.service);
  el("connectionService").value = settings.service;
  el("connectionUrl").value = settings.base_url;
  el("connectionModel").value = settings.model;
  el("connectionKey").value = "";
  el("connectionKey").placeholder = settings.api_key_configured ? "留空保留已保存的密钥；输入新密钥可替换" : `粘贴你的 ${settings.presets[settings.service].label} API Key`;
  el("keyStatus").textContent = settings.api_key_configured ? "API Key 已加密保存在本机，不会显示在页面中。" : "API Key 只保存在你的电脑。首次验证会进行一次少量用量的模型调用。";
  el("clearConnectionKey").classList.toggle("hidden", !settings.api_key_configured);
  el("setupHeading").textContent = settings.api_key_configured ? "AI 服务连接" : "欢迎使用 ResearchPilot";
  el("setupCopy").textContent = settings.api_key_configured ? "替换密钥或调整偏好后，点击“测试并保存”应用到后续研究。" : "输入自己的 API Key，连接成功后就能开始第一次研究。";
  el("desktopTimeout").value = settings.research_task_timeout_s;
  el("desktopBudget").value = settings.token_budget_live;
  el("budgetSetting").textContent = `${settings.token_budget_live.toLocaleString()} tokens`;
  el("settingsSummary").textContent = settings.presets[settings.service].label;
  if (settings.notice) el("connectionFeedback").textContent = settings.notice;
}

async function initializeDesktop() {
  ["desktopConnection", "desktopLimits", "desktopDiagnostics"].forEach(id => el(id).classList.remove("hidden"));
  ["managedConnection", "platformAccess", "researchPreferences", "developerSettings"].forEach(id => el(id).classList.add("hidden"));
  el("settingsIntro").textContent = "连接 AI 服务；所有设置都会自动保存在本机。";
  el("preferencesHint").textContent = "默认值可以直接使用。修改后点击上方“测试并保存”。";
  el("limitsHint").textContent = "可在高级设置中调整时间与预算。";
  el("kbReindexHint").textContent = "以当前保存的资料重新整理索引，保留导入内容，不恢复已删除的资料。";
  renderDesktopSettings(await api("/desktop/settings"));
  if (!providerReady) {
    navigate("settings");
    showView("settings");
    el("connectionKey").focus();
  }
}

async function saveOrTestConnection(save) {
  const payload = connectionPayload();
  if (!payload.api_key && !desktopSettings?.api_key_configured) {
    el("connectionFeedback").textContent = "请先输入你的 API Key。";
    el("connectionKey").focus();
    return;
  }
  ["saveConnection", "testConnection", "clearConnectionKey"].forEach(id => el(id).disabled = true);
  el("connectionFeedback").textContent = "正在连接模型服务，请稍候…";
  try {
    const result = await api(save ? "/desktop/settings" : "/desktop/settings/test", {
      method: save ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (save) {
      renderHealth(await api("/health"));
      await loadPrivateData();
      renderDesktopSettings(result);
      el("connectionFeedback").textContent = "连接成功，设置已保存。";
      toast("已准备好，可以开始研究。");
      navigate("research");
      showView("research");
      el("question").focus();
    } else el("connectionFeedback").textContent = "连接成功。点击“测试并保存”后即可使用。";
  } catch (error) {
    el("connectionFeedback").textContent = productError(error);
  } finally {
    ["saveConnection", "testConnection", "clearConnectionKey"].forEach(id => el(id).disabled = false);
  }
}

function decodeDocument(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(buffer);
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(buffer);
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buffer); }
  catch { return new TextDecoder("gb18030", { fatal: true }).decode(buffer); }
}

function downloadText(text, filename, type = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function importFiles(files) {
  el("kbFiles").disabled = true;
  let imported = 0;
  const importedNames = [];
  try {
    for (const file of files) {
      if (!/\.(txt|md|markdown|csv|json|jsonl|pdf|docx)$/i.test(file.name)) throw new Error("请选择支持的资料文件。");
      if (file.size > maxImportBytes) throw new Error(`${file.name} 过大，请选择较小的文件。`);
      el("kbFeedback").textContent = `正在导入 ${file.name}…`;
      const buffer = await file.arrayBuffer();
      const binary = /\.(pdf|docx)$/i.test(file.name);
      const payload = binary
        ? { filename: file.name, content_base64: encodeBinary(buffer) }
        : { filename: file.name, content: decodeDocument(buffer) };
      const body = JSON.stringify(payload);
      if ((payload.content?.length || 0) > 500000 || new TextEncoder().encode(body).length > maxImportBytes) throw new Error(`${file.name} 内容过长，请拆分后导入。`);
      await api("/kb/import", { method: "POST", headers: { "Content-Type": "application/json" }, body });
      imported++;
      importedNames.push(file.name);
    }
    el("kbFeedback").textContent = `已导入 ${imported} 个文件：${importedNames.join("、")}。请在研究页勾选本次要使用的资料。`;
  } catch (error) {
    el("kbFeedback").textContent = `${imported ? `已导入 ${imported} 个文件。` : ""}${error.detail || error.message}`;
  } finally {
    el("kbFiles").value = "";
    el("kbFiles").disabled = false;
    await loadKnowledgeBase();
  }
}

function encodeBinary(buffer) {
  const bytes = new Uint8Array(buffer);
  let value = "";
  for (let offset = 0; offset < bytes.length; offset += 8192)
    value += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return btoa(value);
}

function reportHtml() {
  const scope = currentResult?.use_examples ? "示例资料（体验报告）" : "本次选定的个人资料";
  const title = escapeHtml(currentResult?.report?.title || "研究报告");
  const sources = el("sources").innerHTML.replace(/<details\b/g, "<details open")
    .replace(/<svg\b[^>]*>[\s\S]*?<\/svg>/gi, "");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>${title}</title><style>body{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 24px;line-height:1.8;color:#243c36}a{color:#21685c}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px}blockquote{border-left:3px solid #21685c;padding-left:16px;margin-left:0}pre{white-space:pre-wrap}svg{display:none}.source-item{break-inside:avoid;page-break-inside:avoid;margin:24px 0;border-top:1px solid #ddd;padding-top:12px}.source-meta,.source-location{color:#63736c}@media print{body{margin:0}details{display:block}summary{display:none}}</style></head><body><p>资料范围：${scope}；不包含实时联网搜索。</p>${renderMarkdown((currentResult?.report?.markdown || currentReportMarkdown).split(/\n## (?:证据附录|引用原文)/)[0])}<h2>参考来源与引用原文</h2>${sources}</body></html>`;
}

function bindProductEvents() {
  el("kbFiles").addEventListener("change", event => importFiles(Array.from(event.target.files)));
  el("downloadReport").addEventListener("click", () => {
    if (currentReportMarkdown) downloadText(reportHtml(), "ResearchPilot-研究报告.html", "text/html;charset=utf-8");
  });
  el("downloadMarkdown").addEventListener("click", () => {
    if (currentReportMarkdown) downloadText(currentReportMarkdown, "ResearchPilot-研究报告.md", "text/markdown;charset=utf-8");
  });
  el("printReport").addEventListener("click", () => window.print());
  let printDetails = [];
  window.addEventListener("beforeprint", () => {
    printDetails = Array.from(document.querySelectorAll(".source-excerpt"), node => [node, node.open]);
    printDetails.forEach(([node]) => { node.open = true; });
  });
  window.addEventListener("afterprint", () => {
    printDetails.forEach(([node, wasOpen]) => { node.open = wasOpen; });
    printDetails = [];
  });
  el("connectionService").addEventListener("change", () => {
    const preset = desktopSettings.presets[el("connectionService").value];
    renderConnectionGuide(el("connectionService").value);
    el("connectionUrl").value = preset.base_url;
    el("model").value = preset.model;
    el("connectionModel").value = preset.model;
    el("connectionKey").value = "";
    el("connectionKey").placeholder = `请输入 ${preset.label} 的 API Key`;
    el("connectionFeedback").textContent = "切换服务商后，请输入对应服务的 API Key。";
    if (!preset.model) el("connectionModel").focus();
  });
  el("connectionKey").addEventListener("keydown", event => {
    if (event.key === "Enter") saveOrTestConnection(true);
  });
  el("saveConnection").addEventListener("click", () => saveOrTestConnection(true));
  el("testConnection").addEventListener("click", () => saveOrTestConnection(false));
  el("clearConnectionKey").addEventListener("click", async () => {
    if (!window.confirm("清除密钥后需要重新输入才能开始新研究。确定清除吗？")) return;
    try {
      renderDesktopSettings(await api("/desktop/settings/api-key", { method: "DELETE" }));
      renderHealth(await api("/health"));
      setBusy(false);
      el("connectionFeedback").textContent = "密钥已清除。";
    } catch (error) { el("connectionFeedback").textContent = productError(error); }
  });
  el("desktopExit").addEventListener("click", async () => {
    try {
      const active = (await api("/desktop/diagnostics")).active_tasks;
      if (active && !window.confirm("有研究正在进行。退出将取消这些任务，确定退出吗？")) return;
      await api("/desktop/exit", { method: "POST" });
      stopObservation();
      privateApiReady = false;
      setBusy(false);
      el("providerNotice").textContent = "ResearchPilot 已退出。下次双击程序即可重新打开。";
      el("providerNotice").classList.remove("hidden");
    } catch (error) { showError(error); }
  });
  ["loadDiagnostics", "downloadDiagnostics"].forEach(id => el(id).addEventListener("click", async () => {
    try {
      const text = JSON.stringify(await api("/desktop/diagnostics"), null, 2);
      el("diagnosticsOutput").textContent = text;
      if (id === "downloadDiagnostics") downloadText(text, "ResearchPilot-诊断.json", "application/json");
    } catch (error) { showError(error); }
  }));
}
