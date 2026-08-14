const DEFAULT_SERVER = "http://127.0.0.1:8794";

async function serverUrl() {
  const value = await chrome.storage.local.get("serverUrl");
  return String(value.serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
}

async function getStatus() {
  const response = await fetch(`${await serverUrl()}/api/status`, {cache: "no-store"});
  const value = await response.json();
  if (!response.ok || !value.ok) throw new Error(value.error || `工作站返回 ${response.status}`);
  return value;
}

async function blobToBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunk = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

async function textToBase64(value, type) {
  return blobToBase64(new Blob([String(value || "")], {type: `${type};charset=utf-8`}));
}

async function currentTab() {
  const tabs = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tabs.length || !tabs[0].id) throw new Error("没有可归档的当前标签页");
  return tabs[0];
}

async function pageDom(tabId) {
  const results = await chrome.scripting.executeScript({
    target: {tabId},
    func: () => ({
      html: document.documentElement ? document.documentElement.outerHTML : "",
      text: document.body ? document.body.innerText : "",
      title: document.title || "",
      url: location.href,
    }),
  });
  return results[0]?.result || {html: "", text: "", title: "", url: ""};
}

async function debuggerArtifacts(tabId) {
  const target = {tabId};
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await chrome.debugger.sendCommand(target, "Page.enable");
    const metrics = await chrome.debugger.sendCommand(target, "Page.getLayoutMetrics");
    const content = metrics.cssContentSize || metrics.contentSize;
    const screenshot = await chrome.debugger.sendCommand(target, "Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      clip: {x: 0, y: 0, width: content.width, height: content.height, scale: 1},
    });
    const pdf = await chrome.debugger.sendCommand(target, "Page.printToPDF", {
      printBackground: true,
      preferCSSPageSize: false,
      paperWidth: 8.27,
      paperHeight: 11.69,
      marginTop: 0.35,
      marginBottom: 0.35,
      marginLeft: 0.35,
      marginRight: 0.35,
    });
    return {fullpage: screenshot.data, pdf: pdf.data};
  } finally {
    if (attached) {
      try { await chrome.debugger.detach(target); } catch (_) {}
    }
  }
}

async function saveAsMhtml(tabId) {
  const blob = await chrome.pageCapture.saveAsMHTML({tabId});
  if (!blob) throw new Error("浏览器未返回 MHTML");
  return blobToBase64(blob);
}

async function notify(title, message) {
  const failed = title.includes("失败");
  await chrome.action.setBadgeBackgroundColor({color: failed ? "#b42318" : "#067647"});
  await chrome.action.setBadgeText({text: failed ? "!" : "✓"});
  await chrome.action.setTitle({title: `${title}：${String(message).slice(0, 200)}`});
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: "icon.svg",
      title,
      message: String(message).slice(0, 500),
    });
  } catch (_) {}
}

async function capturePage(captureKind, note) {
  const status = await getStatus();
  const task = status.current_task;
  if (!task) throw new Error("没有待处理的基础任务");
  const tab = await currentTab();
  const warnings = [];
  const dom = await pageDom(tab.id);
  const artifacts = {};

  try {
    artifacts.visible_png = (await chrome.tabs.captureVisibleTab(tab.windowId, {format: "png"})).split(",", 2)[1];
  } catch (error) {
    warnings.push(`visible_png: ${error.message}`);
  }
  try {
    const values = await debuggerArtifacts(tab.id);
    artifacts.fullpage_png = values.fullpage;
    artifacts.pdf = values.pdf;
  } catch (error) {
    warnings.push(`debugger_capture: ${error.message}`);
  }
  try {
    artifacts.mhtml = await saveAsMhtml(tab.id);
  } catch (error) {
    warnings.push(`mhtml: ${error.message}`);
  }
  artifacts.dom_html = await textToBase64(dom.html, "text/html");
  artifacts.body_text = await textToBase64(dom.text, "text/plain");

  const response = await fetch(`${await serverUrl()}/api/capture`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      schema_version: "1.0",
      task_id: task.task_id,
      capture_kind: captureKind,
      captured_at: new Date().toISOString(),
      url: dom.url || tab.url || "",
      title: dom.title || tab.title || "",
      note: note || "",
      warnings,
      artifacts,
    }),
  });
  const value = await response.json();
  if (!response.ok || !value.capture_saved) throw new Error(value.error || `保存失败 ${response.status}`);
  if (!value.accepted && captureKind !== "blocked") {
    throw new Error(`已保存到诊断目录，但归档不完整：${(value.errors || []).join("；")}`);
  }
  await notify("人工固证已保存", `${task.task_id} · ${task.platform_label} · ${captureKind}`);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "getStatus") {
    getStatus().then((value) => sendResponse({ok: true, value})).catch((error) => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message?.type === "openCurrent") {
    (async () => {
      const status = await getStatus();
      if (!status.current_task) throw new Error("没有待处理任务");
      const tab = await currentTab();
      await chrome.tabs.update(tab.id, {url: status.current_task.search_url});
      return status.current_task;
    })().then((value) => sendResponse({ok: true, value})).catch((error) => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message?.type === "reopenBlocked") {
    (async () => {
      const status = await getStatus();
      const task = status.next_blocked_task;
      if (!task) throw new Error("没有待重试的阻断任务");
      const response = await fetch(`${await serverUrl()}/api/reopen`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({task_id: task.task_id}),
      });
      const value = await response.json();
      if (!response.ok || !value.ok) throw new Error(value.error || "重开任务失败");
      const tab = await currentTab();
      await chrome.tabs.update(tab.id, {url: task.search_url});
      return task;
    })().then((value) => sendResponse({ok: true, value})).catch((error) => sendResponse({ok: false, error: error.message}));
    return true;
  }
  if (message?.type === "capture") {
    capturePage(message.captureKind, message.note).catch((error) => notify("人工固证失败", error.message));
    sendResponse({ok: true, started: true});
    return false;
  }
  return false;
});
