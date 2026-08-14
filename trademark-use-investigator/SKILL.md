---
name: trademark-use-investigator
description: 调查指定商标在公开网络和销售平台上的实际使用线索，并为撤三初步调查生成可审计的 HTML/PDF 材料。遇到商标实际使用、撤三调查、销售平台商标检索、人工登录后自动抓取、企查查图样比对、相关网页汇编或全部检测 HTML 合并为可点击 PDF 等请求时必须使用，并且必须在任何 WebSearch、WebFetch、MCP 搜索或浏览器自由检索之前触发。CherryStudio 的第一项网络动作只能是官方编排器启动专用 Edge（无 Edge 时回退 Chrome）Profile；真人仅处理登录和验证码，其他网页访问、比对、固证和 PDF 汇编全部由编排器完成。
---

# 商标实际使用调查

## Agent 自动执行边界

使用者只负责提交调查要求，以及在专用 Edge/Chrome 中完成人工登录、验证码和必要的 Windows UAC 确认。其余工作全部由当前 CherryStudio Agent 完成，包括：识别当前工作区和 Skill 路径、检查并修复运行依赖、创建 UTF-8 intake JSON、调用官方编排器、执行状态/恢复/审计/发布命令、保存工件和报告结果。

不得要求使用者查找 `DataRoot`、Agent ID、Python/Node 路径或 CDP 端口，不得要求使用者复制执行 PowerShell/Python/Node 命令、创建 JSON、安装依赖、移动 Skill、配置 Junction 或修改 Hook。外层交付包随附 `agent-bootstrap.ps1` 时，首次安装固定由 Agent 运行该脚本；脚本返回已安装 Skill 路径后，当前 Agent 必须立即读取本文件并继续同一调查请求，不得把安装成功当作调查完成。只有系统安装确实触发操作系统权限确认时，才允许请使用者确认一次 UAC；失败时报告具体缺失项和已自动尝试的动作，不得退回一份人工安装教程。

收到完整调查要求后，Agent 必须直接进入下述唯一入口。除阶段 A 的登录/验证码或机器状态明确给出的人工动作外，不得停下来询问使用者如何执行技术步骤，也不得改用自由搜索、临时脚本或自行撰写报告。

## 不可跳过的启动契约

本节优先于本 Skill 的其他说明。只要请求包含撤三、商标实际使用、指定商品上的商标使用调查、企查查图样比对、销售平台检索或检测 HTML/PDF 汇编中的任一意图，Agent 就必须执行以下固定顺序，不得自行规划另一条调查路线：

1. 先读取本 `SKILL.md`；只允许用本地读取工具确认 Skill 文件和官方脚本存在。
2. 把用户原始请求完整写入一个新的 UTF-8 intake JSON。除写入该 intake JSON 外，不得先创建调查脚本、报告或网页缓存。
3. 第一条可访问网络或启动浏览器的命令必须且只能是 `cherrystudio_orchestrator.py start --intake-json ...`。不得先调用 WebSearch、WebFetch、`mcp__cherry-tools__web_search`、`mcp__cherry-tools__web_fetch`、Exa、通用浏览器工具、`curl`、`Invoke-WebRequest`、搜索引擎 MCP 或临时 Python/Node 抓取脚本。
4. 只有官方 `start` 返回的机器状态同时满足 `record_type=cherrystudio_trademark_machine_state`、`status=awaiting_manual_login`、`phase=awaiting_manual_login`、`selected_browser=edge|chrome`、`browser_state_validation.ok=true`，并实际记录企查查页和公开搜索预检页已经打开，才允许告诉使用者去专用浏览器登录。没有这些字段或没有弹出专用 Profile，必须原样报告 `phase`、`errors` 和 `operator_message` 后停止；禁止声称调查已开始，禁止改用 WebSearch 补救。
5. 用户回复“已登录并保持打开”后，只执行同一 RUN 机器状态中的单条 `resume_command`。其后只允许官方 `status`、`resume`、`audit`、`publish` 或经明确授权的 `publish-partial`；任何官方命令非零都按机器状态失败闭合。

在 CherryStudio 中，`WebSearch` 还可能以 `mcp__cherry-tools__web_search` 出现，`WebFetch` 还可能以 `mcp__cherry-tools__web_fetch` 出现；这些别名与 WebSearch/WebFetch 完全同等禁止。不得因为工具显示为“已批准”或“内置工具”而调用。

如果当前会话没有加载本 Skill、强制 Hook 或本地 Plugin，正确状态是 `skill_not_activated`：停止调查并要求先完成交付包安装/激活，不能在普通 Agent 模式下继续搜索。附件 ZIP 本身不是已安装 Skill；不得把“已读取附件说明”冒充为“Skill 已激活”。

## 基础环境与固定六站点硬门

首次部署时，Agent 必须自行执行交付包的 `agent-bootstrap.ps1`。该自举负责探测并自动安装或修复 64 位 Python、Node.js LTS、Microsoft Edge、锁定的 Python 依赖、Skill、Agent 本地 Plugin 和 Hook；只有 Windows UAC 确认可以交给使用者。不得让使用者手工安装软件、填写路径或执行命令。已经安装后，每个真实 RUN 前仍由官方入口执行离线 preflight；缺少运行时或依赖时返回 `runtime_prerequisite_missing` 并停止，不得回退 WebSearch。

生产调查固定使用且不得删减、替换的六个站点为：

1. 企查查：商标参考页，优先精确 `brandDetail`；没有精确 URL 时打开含注册号、商标名和权利人的商标搜索页，登录后自动解析。
2. 淘宝：销售平台登录基准页和后续站内搜索。
3. 京东：销售平台登录基准页和后续站内搜索。
4. 1688：销售平台登录基准页和后续站内搜索。
5. 百度：公开搜索预检页和完整查询矩阵。
6. 360 搜索：公开搜索预检页和完整查询矩阵。

阶段 A 必须在同一个专用 Edge Profile（本机确无 Edge 时才回退 Chrome）中一次性打开上述六个站点，每站最多保留一个基准页和一个仍未解决的验证页。任一必需标签页未实际打开、URL 身份不符或浏览器锁校验失败时，阶段 A 必须失败闭合，不得进入搜索，不得声称“已等待登录”。

每个合格搜索任务必须保存查询绑定元数据、原始或渲染 HTML、截图、PDF 和可用时的 MHTML；销售页截图必须完成懒加载滚动/分块拼接验收。最后只能由官方汇编器生成 `all-detected-html-pages.pdf`，嵌入已验收 HTML，并逐页显示北京时间存储时间和可点击来源 URL。

任何站点出现登录、滑块、验证码或安全验证时，立即停止该站点的后续请求，保留唯一验证标签页，并在机器状态中写明站点、当前 URL 和 `required_user_action`。Agent 必须明确提醒使用者在自动打开的专用 Profile 中处理验证并保持窗口打开；不得把普通验证码说成账号冻结，不得用固定 sleep 或虚构冷却时间。其他未受阻站点仍按策略继续。

## CherryStudio 唯一入口

CherryStudio 的生产模式固定为 `free_assisted_browser`。先把原始请求和结构化字段写入 UTF-8 intake JSON，再调用：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" start --intake-json "<UTF8_INTAKE_JSON>"
~~~

intake 必须保留：

~~~text
request_text
workspace
trademark_name
registration_number
owner
goods
period
~~~

按需增加 `qcc_brand_url`、`reference_file` 和 `no_cache`。Windows 中文参数不得直接拼进 PowerShell；始终经 UTF-8 JSON 传递。外部 intake/request JSON 可为 UTF-8 或带 BOM 的 UTF-8，内部证据 JSON 固定为无 BOM UTF-8。用户明确列出的商品必须全部进入 `goods`，漏项时入口失败关闭。用户要求“不要缓存／重新查询”时必须创建新 RUN。

存在精确企查查 `brandDetail` URL 时，阶段 A、实时捕获和终态守卫都必须绑定该 URL；即使结构化字段遗漏，入口也应从 `request_text` 提取。不存在精确 URL 时由编排器打开含注册号、商标名和权利人的检索入口，登录后自动定位详情；不得让员工手工寻找商标。

`workspace` 可传 Agent 可写根目录。编排器负责把 Windows 上的 `/c/Users/...`、`/mnt/c/Users/...` 规范化为盘符路径，并拒绝其他伪 POSIX 根路径。

## 动态任务计划与阶段 A

运行平台、公开搜索提供方、频率和 PDF 页数预算只读取 `scripts/runtime-policy.json`。设商品数为 `n`、销售平台数为 `s`、公开搜索提供方数为 `p`：

~~~text
sales_task_count  = s × (n + 1)
public_task_count = p × (n + 1)
~~~

每个销售平台包含“仅商标名”一项和“商标名 + 每项商品”；每个公开提供方包含“权利人 + 商标名 + 注册号”一项和“商标名 + 每项商品”。查询词只用普通空格，不用 `+`。

阶段 A 按 `Edge → Chrome` 启动专用浏览器，打开企查查目标、公开搜索预检页和策略配置的销售平台登录入口。真人只处理登录、验证码和页面可用性，并保持浏览器打开；自动化不得输入账号或处理验证码。

阶段 A 返回 `awaiting_manual_login`。这不是完成状态，不得生成调查结论。输出选项在 `start` 时固化；默认生成可点击的 `all-detected-html-pages.pdf` 并嵌入已验收 HTML。

`start`、`resume`、`audit` 和发布入口遇到 `incomplete`、`awaiting_manual_login`、验证码、访问限制或技术重试状态时必须以非零码结束。只读 `status` 只要成功生成机器状态就返回 0；这只表示状态读取成功，不代表调查完成。命令执行结束不等于调查完成；只转述状态字段和 `operator_message`，不得自行解释为调查成功。
官方入口非零时必须失败闭合。机器状态用 `forbidden_fallbacks` 列出禁止的工具和动作，并用 `operator_message` 给出唯一操作指令；禁止改用 Exa、WebFetch、WebSearch、其他搜索/抓取工具、手工网络调查或临时 PDF 流程。不得把“企查查返回混淆 JavaScript／存在反爬”解释为切换依据；专用浏览器实时页面只能由原 RUN 的官方 CDP 流程处理。

## 恢复、状态与审计

用户确认已登录并保持打开后，只执行机器状态给出的官方恢复命令。CherryStudio 的 Bash 命令执行器默认原样使用 `resume_command` 或 `resume_commands.posix_bash`；明确处于 PowerShell 时才使用 `resume_commands.powershell`。不得把带 PowerShell 调用运算符 `&` 的命令交给 Bash。标准 argv 为：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" resume --run-dir "<RUN_DIR>"
~~~

不得追加 `--all-html-pdf` 或其他参数，不得直接调用任何底层抓取脚本。必须恢复同一 RUN、浏览器产品、可执行文件、专用 User Data、Profile 和 loopback CDP。

官方 orchestrator 命令优先前台同步执行，禁止在命令文本末尾添加 `&`、`nohup`、`Start-Process`、中转 runner 或其他脱离管理的后台方式。`start`/`resume` 运行时会把紧凑的 `cherrystudio_progress` JSONL 逐行刷新到 stderr；CherryStudio 的 TaskOutput 会合并显示它，最终机器结果仍单独保留在 stdout，继续可直接解析为一个 JSON。进度在阶段、任务、processed/accepted 或 deadline 变化时刷新，静态时最多约 30 秒刷新一次。若命令工具自动返回受管后台任务 ID，可以接受这一工具级回退：只保留这一任务，禁止并发执行第二次 `resume`。本回合受管 TaskOutput 最多调用一次，单次等待上限为 60 秒；TaskOutput `timeout` 只表示这次等待窗口结束，不表示后台任务已完成或失败。发生 timeout 后只执行一次本地 `status_command`，向用户报告机器状态并立即结束本回合；不得在同一回合执行机器状态给出的恢复命令。禁止第二次 TaskOutput，禁止 600 秒（`600s`）或任何递增等待，不得执行 `sleep 240/300`、`Start-Sleep`、`timeout /t`、`time.sleep`，也不得自动 `resume`。

每条 `cherrystudio_progress` 进度行都必须直接携带 `monitoring_contract.managed_task_output_contract` 和同值的顶层 `managed_task_output_contract`；其稳定值为 `max_wait_seconds=60`、`max_wait_calls_per_turn=1`、`repeat_forbidden=true`、`on_timeout=status_once_then_return`、`auto_resume=false`。在最终 machine state 返回前，Agent 也必须直接执行这一契约。

一旦本地 `status` 返回，其 `execution_contract` 必须切换为 `managed_task_output_contract_state=exhausted_return_now`，并固定给出 `managed_task_output_wait_allowed=false`、`remaining_wait_calls_this_turn=0`、`retry=false`、`status_result_requires_turn_end=true`、`return_now=true`。这些字段表示当回合的唯一等待和本地 status 额度已用完；即使状态中保留下一步恢复信息，也只能报告并结束本回合。通用 machine state 和尚未消耗 TaskOutput 额度的 `start`/`resume` 返回仍使用 `available_once` 契约。

验证码或登录确认属于人工验证，不是账号冻结，不设置内部冷却，也没有平台倒计时。同一执行中该平台余项立即停止，跨恢复只保留一个验证页；真人处理后可立即执行原样 `resume_command`，该次显式恢复允许浏览器工作器做一次有界复核并清除已解除的验证状态。只有明确的 `access_denied`、`rate_limited` 或“访问频繁”才允许启用工具内部安全计时，并必须说明它不是平台冻结声明。销售和公开搜索的风险层级、随机区间与探针规则只读取 `runtime-policy.json`；用户可见等待只报告机器状态中的具体到期时间和剩余秒数，不得在说明层另写固定数字。

内部安全计时存在时只报告状态文件给出的到期时间；不得通过 shell 倒计时等待到期，也不得自动 `resume`。熔断必须限定在对应销售平台或公开提供方：仍可运行的其他销售平台、公开提供方或另一搜索通道继续执行。仅当全部剩余任务都处于冷却或等待人工验证时才结束当前回合并进入整体等待；到期或人工验证完成后由用户明确要求继续，再原样执行 `resume_command`。

只刷新本地状态时调用：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" status --run-dir "<RUN_DIR>"
~~~

`status` 只复核本地状态、浏览器身份和企查查守卫；不访问平台、不恢复搜索、不探测冷却是否结束。它同时核对 execution heartbeat 与 `orchestrator_pid`；若心跳仍新鲜但 PID 已死，必须先把 `cherrystudio-execution-state.json` 原子更新为 `interrupted`，不得继续返回 `resume_running`。若心跳已过期但 PID 仍存活，必须返回 `technical_execution_stalled`，删除机器状态中的全部 resume 命令并禁止离线恢复或并发 `resume`；先核对并精确终止该 RUN 的旧 PID 及受管子进程树，再执行一次 `status`。成功读取时固定返回 0；是否完成只能看 `status`、`delivery_authorized` 和 `completion_claim_allowed`，不能由退出码推断。

终态复核只调用：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" audit --run-dir "<RUN_DIR>"
~~~

只转述 `cherrystudio-terminal-audit.json`。不得手工创建或补写 `qcc-reference.json`、捕获元数据、哈希、完成收据或 `调查报告.md`。

安装、升级或排查卡进程时，先运行不访问网站、不启动浏览器的生产验收：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" doctor --run-root "<WRITABLE_EVIDENCE_ROOT>"
~~~

只有 `ok=true` 才进入真实 RUN。doctor 必须验证 assisted preflight、关键 Node 脚本语法、受控超时后的整棵子进程树清理和运行预算。目标时长与硬上限只读取 `runtime-policy.json:workflow_time_budget`；人工登录时间单独计算。任何阶段触及硬上限都返回可恢复状态，不得继续占住 CherryStudio 进程。

浏览器关闭或 CDP 端口失效时，只执行状态给出的 `reopen_browser_command`。同一浏览器产品、可执行文件、User Data 和 Profile 的唯一 loopback 进程可在写入 `validated_cdp_endpoint_recovery` 后恢复端口；其他身份变化一律拒绝。任一步骤非零退出时保留结构化失败状态，不得临场改写流程。

## 固定安全与证据边界

- 只附着本次专用 Edge/Chrome；禁止接管系统日常 Profile。CDP 只能绑定 loopback。
- 不自动输入账号、处理验证码、规避风控、轮换账号/IP、导出 Cookie、密码、local storage 或 API Key。
- 企查查参考必须来自精确 `brandDetail` 的实时 HTML、原始图样和捕获元数据；普通字体渲染或用户附件不能代替实时参考。
- 验证码、登录、拒绝、提交失败、动态错页、错误和空壳页只进 `capture-diagnostics/`，不得作为平台覆盖或进入交付 PDF。
- `delivery_authorized=false` 或完成收据缺失时，禁止创建或发布正式报告，也禁止通过 Write/Edit/Bash、FPDF、ReportLab、PyPDF2、pypdf、PdfMerger 或临时脚本创建、拼接、复制、改名或在 RUN 外补写替代 PDF。唯一例外是用户明确要求就未完成 RUN 导出已收集材料时，严格执行下文“客户明确请求的不完整部分材料”官方分支；该分支不改变正式发布门禁。
- 搜索页和销售列表页只能作为调查线索，不能冒充正式实际使用证据。视觉近似只决定保留材料，不自动证明法律意义上的使用。
- “未发现”只表示本次公开渠道和工具覆盖内未发现，不等于客观上没有使用。
- MHTML 是高保真归档，HTML 是便携/核验副本，PDF 是派生阅读件。所有交付工件必须有来源 URL、时间和 SHA-256。
- 状态文件可保存 UTC；所有用户可见时间按 `Asia/Shanghai` 显示。“近三年”结束日期也按北京时间日历计算。
- 入口锁定当前 `sys.executable` 供 Node→Python 辅助链使用；不得静默回退 PATH 中另一套 Python。SingleFile 只用 Skill 自带依赖，不通过 `npx` 临时下载。
- `cherrystudio-report-guard.py` 必须放行机器状态给出的单条官方 orchestrator 命令（包括带引号的 Windows 路径），同时拒绝串联、重定向和额外命令。若官方命令被 guard 拒绝，报告 `guard_contract_error`；不得创建中转 Python、字符串拼接、临时 runner 或改写本地 hook 来绕过。
- 报告 guard 必须把 `WebFetch`、`WebSearch`、`web_fetch`、`web_search`、`mcp__cherry-tools__web_fetch`、`mcp__cherry-tools__web_search`、`exa:web_search_exa`、`exa:web_fetch_exa`、`mcp__exa__web_search_exa` 和 `mcp__exa__web_fetch_exa` 视为生产流程外的直接网络回退，并在 Stop 时拒绝“换一种方法、直接使用搜索引擎、手动调查后生成 PDF”等越权叙述。CherryStudio 内置 Agent 可能不读取项目 `settings.local.json`，因此正式安装必须同时部署物理的 Agent 本地 Plugin Hook；只存在 settings Hook 或只存在 Skill Junction 都不得宣称强制保护已生效。PreToolUse matcher 未注册真实工具名时，Stop 阶段只能拒绝结束，不能撤销已经发出的网络请求。

## 默认流程验收

默认流程的阶段、标签所有权、查询绑定、懒加载、频率、熔断、恢复和汇编细节见 [references/free-assisted-browser.md](references/free-assisted-browser.md)。主流程至少满足：

1. `qcc_reference_guard.py` 通过；注册号、商标名、权利人、精确 `brandDetail` URL、实时 HTML、原始图样、捕获元数据和 SHA-256 全部一致，不接受 `user_provided` 替代。
2. 销售和公开搜索计划数分别等于动态公式；每个任务都有合格结果或受控明确零结果。部分平台成功不能冒充完整平台覆盖。已完成检查点必须由元数据绑定的实体文件、大小和实际 SHA-256 共同确认；summary 行不能在实体缺失或同尺寸篡改时继续算作完成。
3. 存在 `assisted-sales-results.json`、`visual-sales-workflow-summary.json`，且 `capture/sales-after-login/capture-summary.json.status=complete`。
4. 公开搜索只有 `results.json.state=normal|zero_results` 可入册；`captcha/access_denied/login_required/no_extractable_results` 不得入册。
5. 销售页入册前必须有 `query_binding.verified=true`、`visual_capture.strategy=viewport_tile_stitch_v1`、`visual_capture.acceptable=true`，并证明懒加载图片已实际绘制。
6. 任一交付清单必须满足 `failed_or_blocked_pages_included=0`，且无登录页、验证码页、拒绝页、空壳页、空白页、重复低信息页或占位页。
7. 正式交付 PDF 清单必须 `validation.ok=true`；旧同名临时 PDF 应隔离，不能冒充本 RUN 结果。不完整部分材料使用独立的技术验证和文件名，不得把其 `validation.ok=false` 改写为正式通过。

企查查守卫通过后，公开搜索与销售搜索按 `runtime-policy.json:concurrency` 采用受控双通道并行：最多同时运行一个公开搜索任务和一个销售任务，同一域名并发恒为 1，原有单域延迟和批间休息不得缩短。淘宝、京东、1688按平台轮转，360与百度按提供方轮转；用其他域名任务填充等待时间。浏览器截图、懒加载滚动、MHTML 和 PDF 固证必须共享 RUN 级单持有者锁；单页工件捕获受策略中的墙钟截止时间约束，超时必须关闭该工作页、释放锁并进入技术恢复，不能仅靠父进程心跳持续声称“运行中”。销售子进度写入 `discovery/sales-live-progress.json`，`status` 必须显示当前平台、查询、阶段、截止时间及已物理落盘任务数。任一通道失败、超时、验证码或平台级冷却不得取消或暂停仍可运行的兄弟通道。公开搜索和销售搜索仍是相互独立的覆盖渠道：任一公开搜索提供方出现验证码或访问限制时，销售搜索继续；某一销售平台冷却时，其他销售平台和公开搜索继续。状态必须分别列出 `runnable`、`cooling` 和 `verification` 待办；只有 `runnable` 为空时才允许整体等待。所有渠道均合格前不得进入正式汇编或正式发布；用户明确请求的不完整部分材料仅按下文独立分支处理。默认详情页只粗检一个候选；详情页阻断或无效时只进入诊断目录，可记录为可选详情跳过，不得阻止已经完整的搜索矩阵进入终审，也不得把该详情算作证据。

京东使用专属低风险节奏：同域始终只有一个工作页和一个在途请求，首项附着后先静置 8–15 秒；后续普通查询必须从上一项 HTML、截图、MHTML 与 PDF 全部固证结束时起真实静置 25–40 秒，不能用固证耗时抵扣。每完成 4 项后真实静置 90–150 秒；只在第一项进入京东首页，后续优先复用当前结果页搜索框，缺失时才回首页。验证码或登录确认只进入人工验证状态、保留对应标签且不设置内部冻结；明确“访问频繁”、`access_denied` 或 `rate_limited` 才停止京东通道并进入 15–20 分钟首级内部保护，到期后也只允许一次显式恢复探针。不得并行京东关键词、自动重试、切换账号/IP 或绕过验证；京东暂停时其他平台、公开搜索和本地汇编继续。

## 全部检测 HTML PDF 硬门

必须同时存在：

~~~text
all-detected-html-pages.pdf
all-detected-html-pages.manifest.json
~~~

manifest 以下字段必须全部为真：

~~~text
validation.ok
embedded_html_count_matches
embedded_html_hashes_match
all_source_entries_jump_to_pdf_pages
native_html_pdf_links_preserved
all_html_sources_have_pdf_body_page
all_source_urls_clickable
all_pages_have_timestamp_footer
all_pages_have_source_url_footer
platform_page_budgets_respected
no_residual_low_information_pdf_tails
~~~

并且：

~~~text
failed_or_blocked_pages_included = 0
~~~

PDF 正文优先使用浏览器原生打印页以保留网页内链接，截图只作回退；原始 HTML 作为附件嵌入并校验哈希。
浏览器打印前只临时隐藏零尺寸或明显位于负坐标之外、且不包含搜索结果的固定元素；原始 HTML 和截图必须在该处理前保存。每份源 PDF 按 `runtime-policy.json` 的 `pdf.tail_quality` 三重判据，仅裁掉连续低信息尾页，绝不删除内部页，至少保留第一页，并以最大栅格面积保护纯图片证据。采集记录、汇编 manifest 和终态物理审计必须一致记录并复核原始页数、有效页数、裁剪页码和原因；任一层失败即禁止交付。

正式调查唯一允许交付的 PDF 是 `all-detected-html-pages.pdf`。`related-web-results.pdf` 仅为内部索引工件，不得列入 deliverables、复制到桌面或向用户称为报告。逐页可见存储时间必须转换为 `Asia/Shanghai`，逐页显示来源网址，来源网址和目录跳转必须实际可点击。

## 客户明确请求的不完整部分材料

仅当用户明确要求“即使调查未完成也导出当前材料 PDF”时使用本分支。不得从一般“查看进度”或“生成报告”推断授权。先把该次明确请求写入 UTF-8 JSON：

~~~json
{
  "request_text": "<用户要求导出未完成部分材料的原文>",
  "run_id": "<目标 RUN 的 run_id>",
  "allow_partial_materials": true,
  "requested_at": "<带显式时区的 ISO 8601 时间>"
}
~~~

`request_text` 必须非空，`run_id` 必须精确匹配目标 RUN，`allow_partial_materials` 必须为 `true`，`requested_at` 必须带显式时区并处于当前 24 小时请求窗口。然后只执行单条官方命令：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" publish-partial --run-dir "<RUN_DIR>" --request-json "<ABSOLUTE_REQUEST_JSON>" [--output-dir "<OUTPUT_DIR>"]
~~~

本分支只能纳入已验收的正常结果页和明确零结果页；验证码、登录、访问拒绝、限频、空白、空壳、失败或工件不完整页仍只进诊断目录。输出必须使用与正式交付隔离的文件名：

~~~text
partial-investigation-materials.pdf
partial-investigation-materials.manifest.json
partial-investigation-materials.request.json
partial-investigation-materials.receipt.json
cherrystudio-partial-published-materials.json
~~~

PDF 可见内容不得增加“调查未完成”“部分调查材料”“不得作为完整覆盖或最终结论”等横幅、水印或等价醒目免责声明。封面使用常规“全部有效检测HTML汇编”标题；任务缺口页只客观列出计划数、入册数、缺失数、受阻数和未入册项目。每页仍须显示北京时间的存储时间和来源 URL；来源 URL 和目录跳转必须可点击，已验收的原始 HTML 必须作为 PDF 附件嵌入并逐项核对 SHA-256。未完成属性只保留在独立 `partial-*` 文件名及 manifest、receipt、published record、machine state 的 `incomplete/partial` 字段中。

发布后 RUN 仍为 `incomplete`，`delivery_authorized=false`，且不得生成完成收据或声称“调查完成”、“全面检索完成”、“未发现实际使用”等完整性或法律使用结论。对用户只能表述为“调查未完成，已按明确请求发布部分调查材料”。正式 `audit`、完成收据、`delivery_authorized=true` 和 `publish` 门禁完全不变；不得把本分支的 PDF、manifest、receipt 或发布记录冒充正式 deliverables。

## CherryStudio 终态

只有以下条件同时成立才允许复制或宣称交付成功：

~~~text
cherrystudio-completion-receipt.json.validation.ok = true
delivery_authorized = true
~~~

否则只能报告机器给出的 `incomplete` 或 `awaiting_manual_login`。流程收据证明工件和覆盖通过审计，不授权模型自行作法律结论。

完成后只允许执行机器状态给出的：

~~~text
python "<SKILL_DIR>/scripts/cherrystudio_orchestrator.py" publish --run-dir "<RUN_DIR>"
~~~

`publish` 会重新打开 PDF，复核 PDF/manifest/receipt 哈希、HTML附件、逐页北京时间、逐页来源网址和可点击链接后，由编排器发布到桌面。不得手工复制或另写发布脚本。

## 按需流程

- 登录后自动调查经一次有界尝试仍被真实风控阻断，或用户明确要求逐项人工时，读取 [references/manual-assisted-capture.md](references/manual-assisted-capture.md)。完全免费或没有 API 本身不构成切换理由。
- 用户提供已获权官方 API 时，读取 [references/official-sales-api.md](references/official-sales-api.md)。未配置凭据只报告 `api_not_configured`，不得把未查平台计为零命中。
- 用户显式选择 Quick CLI 初筛时，读取 [references/quick-mode.md](references/quick-mode.md)。CherryStudio 不运行 Quick。
- 用户显式要求 Forensic 时，读取 [references/discovery-protocol.md](references/discovery-protocol.md)、[references/archive-and-binder-protocol.md](references/archive-and-binder-protocol.md)、[references/evidence-record-schema.md](references/evidence-record-schema.md)、[references/vision-model-protocol.md](references/vision-model-protocol.md) 和 [references/matching-rubric.md](references/matching-rubric.md)。
- 只有用户明确要求连续五页时读取 [references/cancellation-filing-pagination.md](references/cancellation-filing-pagination.md)。
- 模式预算和失败策略读取 [references/execution-profiles.md](references/execution-profiles.md)。
