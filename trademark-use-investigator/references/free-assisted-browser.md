# 免费附着浏览器流程

本文件解释 `free_assisted_browser` 的实现与故障恢复。CherryStudio 不得直接拼接本文提到的底层脚本；生产运行只执行 `cherrystudio_orchestrator.py` 返回的命令。

## 计划与运行策略

- 默认销售平台、公开搜索提供方、访问间隔、PDF 页数预算均读取 `scripts/runtime-policy.json`。
- 设商品数为 `n`、销售平台数为 `s`、公开搜索提供方数为 `p`：
  - 销售任务数为 `s × (n + 1)`；每个平台含“仅商标名”一项和“商标名 + 单项商品”各一项。
  - 公开搜索任务数为 `p × (n + 1)`；每个提供方含“权利人 + 商标名 + 注册号”一项和“商标名 + 单项商品”各一项。
- 查询词只使用普通空格，不使用 `+` 或全角加号；URL 查询参数按 UTF-8 编码为空格 `%20`。
- `start` 时把计划、浏览器身份、输出选项和页数预算固化进同一 RUN；恢复时不得改动。

## 阶段 A：浏览器与人工登录

1. 编排器按 `Edge → Chrome` 选择浏览器，使用专用 User Data 和随机 loopback CDP 端口；禁止接管系统日常 Profile。
2. 阶段 A 打开：企查查目标页、公开搜索预检页和策略中配置的销售平台登录入口。销售平台只打开一次，不提前打开任一商品查询结果页。
3. 用户提供精确企查查 `brandDetail` URL 时必须直接打开并全程绑定该 URL；否则打开含注册号、商标名和权利人的商标检索入口，登录后由编排器自动定位并核验最多八个详情候选。不得要求员工手工搜索或选择商标。
4. 标签清理在专用调查 Profile 内为每个受管站点最多保留一个基准页和一个仍未解决的登录/验证码页；关闭重复验证页、双重编码旧搜索页、已被精确详情替代的企查查普通搜索页和旧结果页。工具创建的验证页持久化 target ID，人工解除后在恢复时关闭，未解除时聚焦且不再发请求。
5. 状态文件记录浏览器产品、可执行文件、专用 User Data、Profile、CDP endpoint、企查查目标和标签验收结果。CDP 只能绑定 `127.0.0.1` 或 `localhost`。
6. 真人只处理登录、验证码和页面可用性，并保持浏览器打开。阶段 A 返回 `awaiting_manual_login`；它不是终态。

## 企查查参考守卫

- 阶段 B 前必须取得精确 `brandDetail` 的实时 HTML、原始商标图样和捕获元数据。
- 注册号、商标名、权利人、源 URL、终 URL、浏览器身份、文件大小和 SHA-256 均须一致。
- 不得用用户附件、普通字体渲染、搜索列表图或手填 JSON 代替企查查实时参考；用户附件只能作为辅助核对材料。
- 导入后生成 `reference/qcc-brand-detail.html`、原始图样和 `reference/qcc-reference.json`；`qcc_reference_guard.py` 在恢复和终审时再次验证。

## 阶段 B：公开与销售渠道独立推进

1. `resume` 优先以前台同步方式附着阶段 A 锁定的同一浏览器，不关闭或重启 Profile。CherryStudio 的 Bash 执行器使用 `resume_command`/`resume_commands.posix_bash`，PowerShell 才使用 `resume_commands.powershell`。`start`/`resume` 把紧凑的 `cherrystudio_progress` JSONL 实时 flush 到 stderr，字段包含真实阶段、当前任务、processed/accepted、计划数和阶段 deadline；最终机器 JSON 独占 stdout。进度状态变化即刷新，静态时最多约 30 秒一条。若命令工具自动返回受管后台任务 ID，只允许保留这一任务。本回合受管 TaskOutput 最多调用一次，单次最长 60 秒；TaskOutput `timeout` 仅表示等待窗口结束，不表示后台任务完成或失败。timeout 后只执行一次本地 `status_command`，向用户报告状态并结束本回合，不得当回合再执行恢复动作。禁止第二次 TaskOutput、第二次 `resume`、600 秒（`600s`）或递增等待，也禁止 shell `sleep`、`Start-Sleep`、`timeout` 和自动 `resume`。
   每条 `cherrystudio_progress` 必须同时包含 `monitoring_contract.managed_task_output_contract` 和顶层 `managed_task_output_contract`，并固定给出 `max_wait_seconds=60`、`max_wait_calls_per_turn=1`、`repeat_forbidden=true`、`on_timeout=status_once_then_return`、`auto_resume=false`，使 Agent 在尚未看到最终 machine state 时也能立即执行等待上限。
   本地 `status` 一旦返回，机器契约必须改为 `managed_task_output_contract_state=exhausted_return_now`，其中 `managed_task_output_wait_allowed=false`、`remaining_wait_calls_this_turn=0`、`retry=false`、`status_result_requires_turn_end=true`、`return_now=true`。运行中、子通道失败收尾中、技术停滞或并发 `resume` 被拒绝都适用；后台执行自行继续，Agent 只报告并结束当前回合。
2. 公开搜索按运行策略的提供方顺序执行完整动态矩阵。每项保存 `serp.html`、保留网页链接的 `serp.pdf`、`serp.png`、`results.json` 和哈希。
3. 只有 `results.json.state=normal|zero_results` 的公开搜索任务可交付。`captcha`、`access_denied`、`login_required`、`no_extractable_results` 和工件不完整只进诊断目录。
4. 企查查守卫通过后，公开和销售矩阵由同一父流程以最多两个 worker 同时启动：一个 worker 只运行公开搜索，另一个只运行销售搜索。两个 worker 必须全部回收后再评估结果；一方失败、超时或验证码不得取消另一方。公开渠道出现验证码、访问限制或技术失败时，销售矩阵继续；销售某个平台受阻时，其他销售平台和公开渠道继续。所有矩阵均完整前不得进入正式汇编；用户明确请求的不完整部分材料只能走本文独立分支。
5. 销售搜索从 `manual-capture-queue.json` 读取任务。实际 URL 路由、查询参数或可见搜索框值必须与规范任务绑定，且 `query_binding.verified=true` 才能认作结果或明确零结果。
6. 查询参数优先按 UTF-8 解码。1688 只有在严格绑定失败时才允许 GB18030/GBK 回退，并把实际编码写入元数据。首页、商品页误跳、错误查询、缺失查询参数或预编码查询一律失败关闭。
7. `delivery_platforms` 只包含已完整通过全部计划任务的平台；至少一项成功的平台另记，不能冒充完整覆盖。
8. 默认只粗检一个候选详情并运行本地图样筛选；只有用户明确扩大详情核验时才提高上限。可选详情页遇到阻断或工件无效时只进诊断目录并记为 `diagnostic_detail_skip`，不覆盖完整搜索矩阵，也不进入证据 PDF。视觉近似只决定保留材料，不自动证明法律上的商标使用。

## 懒加载与视觉捕获

- 销售平台按平台轮转、公开搜索按提供方轮转；同一域名始终只有一个活动请求，且继续执行原延迟与批间休息。并行只跨互不相同的域名，不通过缩短延迟提速。
- 所有通道共用 RUN 内 `.locks/browser-evidence-capture.lock`。页面导航和单域等待在锁外；截图、懒加载滚动、MHTML 和浏览器 PDF 固证在锁内且全局并发为 1。锁等待不得计入或改写平台访问间隔。
- 搜索页和详情页固证前滚动并提升 `data-src`、`data-lazy-src`、`data-ks-lazyload`、`data-original`、`data-srcset` 等资源，等待实际图片解码和绘制。
- 对会卸载离屏节点的虚拟列表，禁止用滚动结束后的单次 `fullPage` 截图。必须逐视口等待、截图、拼接，并用 `stitch-viewport-tiles.py` 做像素级非空校验。
- 交付元数据必须记录 `visual_capture.strategy=viewport_tile_stitch_v1`、`visual_capture.acceptable=true`、懒加载统计和每块截图方法。
- Playwright 单屏截图技术超时时，在同一页面用 CDP `Page.captureScreenshot` 回退；两者都失败才标记技术重试，不能误报为平台验证。
- 同一轮中首次发生“Playwright 截图失败、CDP 成功”后，后续图片块直接使用 CDP，并记录后端切换、失败数和跳过数；不得让每个图片块重复等待同一种已确认的技术超时。
- `page.evaluate`、CDP 截图、MHTML、PDF 与整页切片都必须有本地操作截止时间。单页整页切片默认最多占用工件锁 90 秒，协议调用 30 秒，PDF 45 秒；超时关闭自动化创建的工作页并释放锁，记为技术捕获失败，不触发平台冷却。完整且校验通过但总报告尚未写出的页面可从 `metadata.json` 恢复，恢复时不得重发对应查询。
- 销售搜索在 `discovery/sales-live-progress.json` 原子记录当前平台、查询编号、阶段、阶段截止时间、最近完成任务和已落盘数量。父进程只轮询这些本地状态文件来生成进度流，不访问网站。Windows 原子替换遇到 `EPERM`、`EBUSY` 或 `EACCES` 时，只对同一临时文件执行有界退避；绝对 2 秒 deadline 到达后不再发起新的 `rename` 重试。已经发起的单个 `rename` 系统调用不可取消，因此不承诺该调用自身在 2 秒内返回。心跳只证明编排器最近写过状态；`status` 必须同时确认记录的 PID 仍存活，并结合子进度判断正常等待与技术停滞。心跳新鲜但 PID 已死时原子持久化 `state=interrupted`，不得继续报告 `resume_running`。心跳过期但 PID 仍活时返回 `technical_execution_stalled`，不运行中断恢复、不提供 `resume_command`；必须先核对并精确终止旧编排器 PID 及其受管子进程树，再执行一次本地 `status`。
- 技术性截图、PDF、HTML 或提取失败不得重新提交已成功的搜索；优先在同一页面或既有有效工件上离线补齐。

## 频率、熔断与状态语义

- 销售与公开搜索的默认延迟、批大小、批间休息和内部保护时间只读取 `scripts/runtime-policy.json`；CLI 显式参数仅在非 CherryStudio 独立流程中覆盖。
- 京东的普通间隔和批间休息从上一项全部固证结束时起计算，固证耗时不得抵扣；首项静置 8–15 秒，普通项真实静置 25–40 秒，每 4 项真实静置 90–150 秒。同一京东工作页只在首项进入首页，后续优先复用结果页搜索框；同域并发恒为 1。
- 出现验证码或登录确认时立即停止该平台，保留并置前唯一真实验证页，同一调用中该平台余项全部 `deferred_manual_verification` 且不发送请求；使用 `status=awaiting_manual_login`、`phase=*_verification_required`。验证码不是账号冻结，平台没有提供等待倒计时，工具不为验证码设置内部冷却；真人处理后可立即原样 `resume`。
- 只有 `access_denied`、`rate_limited`、访问频繁或明确拒绝才设置工具内部安全计时。它不是平台承诺的冷却或冻结时间。销售和公开搜索的风险层级、随机区间及探针规则以 `runtime-policy.json` 为唯一权威；每次到期只允许策略规定的探针任务，成功后才恢复该渠道余项并清除或衰减风险级别。用户可见文案必须读取当前 snapshot 的具体剩余时间。
- 内部安全计时只在状态中记录到期时间，不授权当前回合阻塞等待。状态分别列出可运行、冷却和等待人工验证的平台/任务；单个平台冷却时继续其他销售平台和公开搜索，只有所有剩余任务均不可运行时才使用 `status=incomplete`、`phase=waiting_internal_cooldown` 并立即向用户返回。到期后由用户明确要求继续。不得用 `sleep 240`、`sleep 300` 等命令倒计时，也不得每次增加等待时长。
- 验证码与真实访问限制同时出现时分别列出；处理验证码后允许立即恢复已验证平台，仍处于内部保护的平台继续跳过。任何情况都不自动重试或探测恢复。
- HTML/PDF/截图缺失、像素质量或提取失败属于 `*_capture_retry_required`，不是安全验证。
- 所有用户可见时间转换为 `Asia/Shanghai`；状态文件可继续保存 UTC ISO 8601。

## 标签所有权与恢复

- 自动化只导航自己创建的工作页。阶段 A 的标签验收可在专用调查 Profile 内关闭同一受管站点的重复基准/验证/旧结果页，但必须保留一个当前基准页和一个仍阻断的验证页；不得触碰非受管站点页面。
- `status` 只访问本机 CDP 并核对浏览器身份、本地工件和企查查守卫，不访问平台。成功生成机器状态时返回 0；是否完成只看状态字段，不从退出码推断。
- 报告 guard 必须以完整 token 解析识别单条官方 orchestrator 命令，并兼容带引号的 Windows/正斜杠路径；不得只用要求 `.py` 后立即空格的正则。guard 拒绝官方命令时标记 `guard_contract_error` 并停止，不得用中转脚本、字符串拼接或临时 runner 绕过。
- guard 必须在商标流程上下文中拒绝 agent 自行发出的 shell 等待与后台包装，包括 `sleep`、`Start-Sleep`、`timeout /t`、`time.sleep`、`nohup`、`Start-Process` 和尾随 `&`。脚本内部由运行策略控制的访问间隔不属于 shell 盲等，不受影响。
- 任一官方入口非零退出时，机器状态必须保留非空 `operator_message`，并写入结构化 `forbidden_fallbacks`。推荐结构为 `{"active": true, "tools": [...], "actions": [...]}`；`tools` 至少列出 `WebFetch`、`WebSearch`、`web_fetch`、`web_search`、`mcp__cherry-tools__web_fetch`、`mcp__cherry-tools__web_search`、`exa:web_search_exa`、`exa:web_fetch_exa`、`mcp__exa__web_search_exa`、`mcp__exa__web_fetch_exa`，`actions` 至少列出 `alternate_search`、`manual_web_investigation` 和 `manual_pdf_generation`。恢复或终止只能遵循 `operator_message`，不得把反爬 JavaScript、搜索服务超时或标签验收失败改写成另一条调查流程。
- 要在网络请求发出前拦截这些工具，CherryStudio 的 PreToolUse matcher 必须包含：`Bash|Write|Edit|WebFetch|WebSearch|web_fetch|web_search|mcp__cherry-tools__web_fetch|mcp__cherry-tools__web_search|exa:web_search_exa|exa:web_fetch_exa|mcp__exa__web_search_exa|mcp__exa__web_fetch_exa`。其中 `mcp__cherry-tools__web_search` 与 `mcp__cherry-tools__web_fetch` 是 CherryStudio 内置且通常自动批准的真实运行时工具名，不能只拦截界面显示名。`cherrystudio-report-guard.py` 会识别这些别名；若本地 hook 或 Agent 本地 Plugin matcher 未注册这些工具，网络请求将先行发生，Stop 阶段只能拒绝结束而不能撤回请求。
- 保存的 CDP 端口失效时，仅在同一浏览器产品、可执行文件、User Data 和 Profile 有且仅有一个 loopback 主进程时恢复端口，并写入 `validated_cdp_endpoint_recovery`。
- 浏览器产品、可执行文件、User Data、Profile 或 handoff 模式变化一律拒绝恢复。浏览器确已关闭时只使用机器状态给出的 `reopen_browser_command`。
- 中断恢复先按本地工件事实持久化有界状态；已完成公开搜索时只能进入销售恢复阶段，不得重复公开矩阵。
- 只有任务身份、查询绑定、视觉拼接、工件哈希和 PDF 校验达到终审等级的既有页面可离线复用；不得联网“补证”旧页面。

## 汇编

- 正常搜索列表可进入内部 `related-web-results.pdf`，但该文件永不属于正式 deliverables，不得复制到桌面或向用户称为报告。
- 全部检测 HTML 模式在同一 RUN 本地生成 `all-detected-html-pages.pdf` 和 manifest；不为汇编重新访问平台。
- PDF 优先使用浏览器原生打印页以保留网页内链接，截图只作回退；每页必须有来源 URL、存储时间和可点击跳转。
- 原始 HTML 和截图必须先于打印布局清理保存。打印时只临时隐藏零尺寸或明显负坐标、且不包含搜索结果的固定元素；打印后恢复。源 PDF 与正式汇编分别按 `runtime-policy.json:pdf.tail_quality` 做两层连续低信息尾页检测，只能裁尾页、不能删除内部页，至少保留第一页，并用最大栅格面积保护纯图片页。裁剪页码、原因和物理指标必须进入 manifest，终态审计重新打开源 PDF 复核。
- 原始 HTML 作为 PDF 附件嵌入并逐项校验 SHA-256。阻断、登录、验证码、失败和空壳页不得进入 PDF。
- 必需的搜索矩阵抓取非零退出或摘要不是 `complete` 时，必须停止正式汇编并隔离同名旧文件。默认粗检详情属于可选增强：失败页只能作为带原因的诊断跳过，不能进入 PDF、不能算作捕获成功，也不能被描述成已核验详情。用户明确请求不完整部分材料时，只能走下文官方独立分支。
- 正式唯一交付 PDF 为 `all-detected-html-pages.pdf`。完成收据必须绑定其 PDF/manifest 哈希、页数和HTML附件数；编排器 `publish` 再次物理复核附件、逐页北京时间、逐页来源网址和链接后才发布到桌面。
- 未获正式发布授权时禁止用 FPDF、ReportLab、PyPDF2、pypdf、PdfMerger 或临时脚本创建、拼接或复制替代PDF。用户明确请求导出未完成 RUN 的当前材料时，仅允许下述 `publish-partial` 分支。

## 客户明确请求的不完整部分材料

该分支是对未完成 RUN 的受控材料导出，不是正式报告降级。只有当用户在当前请求中明确要求导出部分材料 PDF 时，才能创建 UTF-8 请求 JSON：

~~~json
{
  "request_text": "<用户的明确请求原文>",
  "allow_partial_materials": true,
  "requested_at": "<ISO 8601 时间>"
}
~~~

三个字段均必填：`request_text` 非空，`allow_partial_materials=true`，`requested_at` 为本次明确请求的时间。通过 guard 执行且只执行：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" publish-partial --run-dir "<RUN_DIR>" --request-json "<ABSOLUTE_REQUEST_JSON>" [--output-dir "<OUTPUT_DIR>"]
~~~

编排器必须完成以下约束：

1. 保持 RUN 为 `incomplete`、`delivery_authorized=false`；不创建正式完成收据，不改写 `audit` 或 `publish` 门禁。
2. 只纳入已验收的 `normal` 和明确零结果（`zero|zero_results`）页。验证码、登录、访问拒绝、限频、空白、空壳、失败和工件不完整页必须保持在诊断目录且不进 PDF。
3. 使用 `partial-investigation-materials.pdf`、`partial-investigation-materials.manifest.json`、`partial-investigation-materials.request.json`、`partial-investigation-materials.receipt.json` 和 `cherrystudio-partial-published-materials.json`；不覆盖、复用或冒充正式文件。
4. PDF 封面、任务覆盖页、正文和页脚均不得增加“调查未完成”“部分调查材料”“不得作为完整覆盖或最终结论”等横幅、水印或等价醒目免责声明；任务覆盖页仅客观列出计划、入册、缺失、受阻和未入册项目。逐页显示 `Asia/Shanghai` 存储时间和来源 URL，使来源 URL 和目录跳转实际可点击。未完成属性只写入独立 `partial-*` 文件名以及 manifest、receipt、published record 和 machine state 的机器字段。
5. 把每份已验收原始 HTML 嵌入 PDF 附件并核对 SHA-256；在 manifest 列明已完成、缺失和受阻任务，并保证 `failed_or_blocked_pages_included=0`。
6. 发布记录只能授权“部分调查材料已发布”这一事实。对用户必须同时说明“调查未完成”，不得声称“调查完成”、“全面检索完成”、“未发现实际使用”或任何完整性、法律使用结论。

部分材料的技术校验成功不等于正式 `validation.ok=true`。后续完成所有矩阵后，仍必须独立执行正式 `audit`、生成完成收据、取得 `delivery_authorized=true`，再通过 `publish` 发布正式 PDF。
