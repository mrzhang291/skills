# 真人检索与本地一键固证

本流程只是后备：仅当真人登录后的自动搜索仍被风控阻断，或用户明确要求真人逐项检索时使用。若用户只愿负责登录，必须改用 `build-sales-login-queue.py` → `open-sales-login-profile.py` → `run-visual-sales-after-login.py`，由自动化完成搜索、抓取和图样比对。本流程不让 Playwright、Selenium 或 WebDriver 导航平台；真人负责登录、验证、确认查询已提交和判断页面状态，本地扩展只在人工点击后归档当前页。

## 组件和边界

- 默认使用运行目录外的商标调查专用 Microsoft Edge Profile，不接管系统日常 Edge；仅在本机没有 Edge 时自动回退 Google Chrome。
- 默认任务为淘宝、京东、1688 ×（仅商标名 + 每项核定商品）；不要求连续五页。
- 工作站仅绑定 `127.0.0.1`，不监听局域网；扩展只把页面工件发送到本机。
- 扩展保存可见页截图、整页截图、PDF、MHTML、渲染 DOM 和正文；工作站补抓包含地址栏的活动 Edge/Chrome 窗口并计算 SHA-256。
- 扩展使用 `chrome.debugger` 仅执行当前页截图和打印，不导航、不输入、不点击页面元素、不处理验证码。
- 可选的 Instant Data Scraper 只在浏览器本地把真人已打开的列表页导出成 CSV/XLSX，不需要淘宝 API、账号套餐或免费额度；导出只是筛查线索。
- Cookie、密码、local storage、浏览器 Profile 和 API Key 不写进运行目录。

## 启动

先生成队列：

```text
python "<SKILL_DIR>/scripts/build-manual-capture-queue.py" --run-dir "<RUN_DIR>" --platform taobao --platform jd --platform 1688
```

首次使用先在专用 Edge 中安装 Skill 自带扩展；没有 Edge 时脚本才选择 Chrome。脚本只打开所选浏览器的扩展管理页和扩展目录，不替员工点击安装或确认权限：

```text
python "<SKILL_DIR>/scripts/open-manual-extension-setup.py"
```

员工在专用 Edge（回退时为 Chrome）中打开“开发者模式”，点击“加载已解压的扩展程序”，选择脚本返回的 `extension_dir`，固定扩展后关闭专用浏览器。该步骤只需执行一次；Skill 更新扩展文件后，在扩展管理页点击“重新加载”。

如需自动整理当前列表的标题、链接、价格和店铺，员工另行从 [Chrome Web Store 的 Instant Data Scraper 官方条目](https://chromewebstore.google.com/detail/instant-data-scraper/ofaokhiedipichpaobibbnahnkdoiiah) 安装扩展。员工必须亲自查看并确认第三方扩展权限；本 Skill 不自动安装。Instant Data Scraper 与 Skill 自带固证扩展职责不同：前者生成可筛选的数据线索，后者生成带页面工件和哈希的固证材料。

之后启动本机工作站和已安装扩展的专用 Edge；没有 Edge 时自动启动 Chrome：

```text
python "<SKILL_DIR>/scripts/manual-capture-workstation.py" --run-dir "<RUN_DIR>" --launch-browser
```

保持命令窗口运行。工作站地址固定使用 `http://127.0.0.1:8794/`；如果该端口被占用，先结束占用进程，不要把工作站暴露到局域网。

## 每项任务

1. 在扩展中点击“打开当前任务”；如果平台没有正确提交查询，真人在可见搜索框中粘贴扩展显示的查询词并提交。
2. 真人处理登录或验证，等待页面稳定，并确认地址栏域名、查询词和页面状态。
3. 页面是正常结果列表时，可点击 Instant Data Scraper，确认识别出的列和翻页范围后导出 CSV 或 XLSX；不要让它代替真人处理登录、验证码或页面状态判断。
4. 回到工作站，选择刚导出的文件，点击“导入当前任务 CSV/XLSX”。工作站归档原始文件、统一常见中英文字段、过滤跨平台链接、去重并生成全局候选清单；重复文件不会重复写入。
5. 仍须对正常结果列表点击“保存正常结果页”；只有页面明确表示零结果时才点“保存明确零结果”。导入 CSV/XLSX 不会完成任务。
6. 验证码、滑块、登录、拒绝和空壳页点击“标记验证/阻断页”；它们只进入 `capture-diagnostics/manual-capture/`，任务需要稍后重新打开完成。
7. 命中相关商品时可先打开详情并点击“保存商品详情页”。详情抓取不替代该查询的结果页抓取，也不自动证明权利人使用。
8. 保存后等待浏览器通知，再切换标签页或进入下一任务。

工作台不可用时，也可以命令行导入并显式绑定任务：

```text
python "<SKILL_DIR>/scripts/instant_data_import.py" --run-dir "<RUN_DIR>" --task-id MC001 --input "<EXPORT.csv|EXPORT.xlsx>"
```

导入器接受 UTF-8/GB18030 CSV 和 XLSX，保留原始文件，输出规范化 JSON/CSV、拒绝行明细、导入清单和 SHA-256。文字未命中商标或核定商品的行仍保留为待复核线索，不能据此宣称零命中。

## 校验和汇编

全部基础任务处理后执行：

```text
python "<SKILL_DIR>/scripts/validate-manual-capture.py" --run-dir "<RUN_DIR>" --min-platforms 3
python "<SKILL_DIR>/scripts/build-manual-sales-pdf.py" --run-dir "<RUN_DIR>" --min-platforms 3
```

`manual-capture-validation.json.ok=true` 才表示任务矩阵完整。输出 `manual-sales-results.pdf` 和 `manual-sales-results.manifest.json`，两者均声明为相关调查线索，不冒充正式商标使用证据。需要中途预览时，PDF 构建脚本可加 `--allow-partial`，但清单会标记 `partial_preview=true`。

校验器会同时检查任务引用的每批结构化导出：原始 CSV/XLSX、规范化 JSON/CSV、拒绝行文件的大小与 SHA-256 必须匹配。`structured_export_batch_count` 和 `structured_export_item_count` 进入校验报告与 PDF 清单，但不会增加 `accepted_task_count`。

## 目录

```text
discovery/manual-capture-queue.json
discovery/manual-capture-state.json
discovery/manual-exports/<TASK_ID>/<IMPORT_ID>/
discovery/manual-export-imports.jsonl
discovery/manual-export-candidates.json
discovery/manual-export-candidates.csv
manual-capture/pages/<TASK_ID>/<CAPTURE_ID>/
manual-capture/details/<TASK_ID>/<CAPTURE_ID>/
capture-diagnostics/manual-capture/<TASK_ID>/<CAPTURE_ID>/
manual-capture-validation.json
manual-sales-results.pdf
manual-sales-results.manifest.json
```

正常结果/明确零结果每次归档至少含 `browser-window.png`、`visible.png`、`fullpage.png`、`page.pdf`、`page.mhtml`、`rendered-dom.html`、`body-text.txt`、`metadata.json` 和 `hashes.json`。
