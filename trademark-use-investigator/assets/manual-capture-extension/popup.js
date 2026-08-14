let current = null;
let blocked = null;

function send(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));
}

async function refresh() {
  const response = await send({type: "getStatus"});
  if (!response?.ok) throw new Error(response?.error || "无法连接本地工作站");
  const status = response.value;
  current = status.current_task;
  blocked = status.next_blocked_task;
  document.getElementById("progress").textContent = `完成 ${status.completed_count}/${status.task_count} · 阻断 ${status.blocked_count}`;
  document.getElementById("task").textContent = current ? `${current.task_id} · ${current.platform_label} · ${current.target_good || "仅商标名"}` : (blocked ? "基础任务已轮询完，请重试阻断项" : "全部基础任务已处理");
  document.getElementById("query").textContent = current ? current.query : "-";
  document.getElementById("open").disabled = !current;
  for (const button of document.querySelectorAll("button[data-kind]")) button.disabled = !current;
  document.getElementById("retry").hidden = !!current || !blocked;
}

document.getElementById("open").addEventListener("click", async () => {
  const response = await send({type: "openCurrent"});
  if (!response?.ok) document.getElementById("error").textContent = response?.error || "打开失败";
  else window.close();
});

document.getElementById("retry").addEventListener("click", async () => {
  const response = await send({type: "reopenBlocked"});
  if (!response?.ok) document.getElementById("error").textContent = response?.error || "重开失败";
  else window.close();
});

for (const button of document.querySelectorAll("button[data-kind]")) {
  button.addEventListener("click", () => {
    if (!current) return;
    chrome.runtime.sendMessage({
      type: "capture",
      captureKind: button.dataset.kind,
      note: document.getElementById("note").value,
    });
    window.close();
  });
}

refresh().catch((error) => { document.getElementById("error").textContent = error.message; });
