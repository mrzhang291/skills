# URL 发现协议

## 目标

把关键词查询转换为可直接访问的目标网页 URL。搜索结果页只证明“如何发现 URL”，不证明目标网页内容，也不进入证据 PDF。

## 查询族

本协议的完整查询族用于 `forensic`。`quick` 必须改用
`scripts/build-query-plan.py` 生成的最多 7 个合并查询，具体边界见
[execution-profiles.md](execution-profiles.md)。Forensic 至少组合以下查询：

1. 精确注册号；
2. 完整权利人；
3. 商标精确词与 OCR/拼写变体；
4. 商标加核定商品、同义词和商业场景词；
5. 商标加权利人；
6. `site:` 加重点平台或行业站点；
7. 常见同名误报和排除词。

## 提供方

- Firecrawl：可选，配置 `FIRECRAWL_API_KEY` 或 `FIRECRAWL_API_URL` 后使用。
- Bing、360、Yahoo、百度：通过 Playwright 访问，提取结果卡片中的直达 URL；百度跳转链接必须解析为最终站外 URL，无法解析时不得把 `baidu.com/link` 当作目标页。
- CherryStudio 搜索工具：把实际结构化 JSON 原样保存后，以受信 provider `cherrystudio-web` 和 `--source-kind cherrystudio_export` 校验导入。未知 provider 默认拒绝。
- 提供方被验证码或拒绝时记录状态，不伪造成功，不把阻断页保存为正式来源。

Quick 禁止可见浏览器和人工解锁；被阻断时记录真实状态并继续。只有用户明确启用 Forensic 且授权交互登录时，才可在 `--headed` 下同时设置 `--wait-for-unblock-ms <N>`；脚本在上限内重新判定页面状态，超时后仍保留真实阻断状态。

Quick 至少需要一个正常的受信 base provider，单个查询失败不触发反复重试；Forensic 至少需要两个，并要求每个 QUERY_ID 至少有一个成功提供方。`provider_instance_id` 仅用于审计，不能把同一 base provider 刷成多个覆盖来源；同一 raw SHA-256 被不同 provider 声称时拒绝完成。provider 身份是本地来源声明与原始响应校验，不是远端身份的密码学证明。

## 查询结果预算

`run-config.json.budgets.max_results_per_query` 限制同一个 `QUERY_ID` 跨全部提供方最终写入 `results.jsonl` 的规范化 URL 总数。先按规范化 URL 去重，再按提供方轮转、提供方内原始排名稳定裁剪，防止第一个提供方耗尽全部预算。搜索后端的完整原始响应和页面诊断不裁剪。

`--limit-per-provider` 只能进一步降低单个后端的请求上限。无论浏览器发现、Firecrawl 还是结构化结果导入，都不能把同一 `QUERY_ID` 的保留目标数扩大到运行预算以上；同一查询重跑或新增导入提供方后必须对该查询重新统一裁剪。

## URL 规范化

执行以下处理：

- 解包常见 Bing/Yahoo 重定向；
- 移除 fragment、`utm_*`、`spm` 等跟踪参数；
- 统一 scheme、host、默认端口和查询参数顺序；
- 基于规范 URL 去重；
- 记录每个 URL 的 query、provider、rank 和原始 URL；
- 用稳定 SHA-256 前缀生成 `target_id`。

以下 URL 只能作为发现页：

- Bing、360、Yahoo、百度、搜狗、Google、DuckDuckGo、Yandex、Brave 的搜索页；
- `search.jd.com`、`s.taobao.com`、`s.1688.com`、`search.suning.com` 等平台内部搜索页；
- 带明显 `/search` 路径和 `q`/`keyword` 参数的结果页。

## Frontier 选择

从 `discovery/url-frontier.json` 选择直达抓取 URL 时：

- Quick 先运行 `scripts/rank-frontier.py`，只保留命中商标、注册号或完整权利人的最多 8 个本地候选，并集中在最多 3 个站点；
- 优先标题/摘要含商标、商品、权利人或购买场景的页面；
- 每域名默认不超过运行配置的 `max_pages_per_domain`；
- 保留跨域多样性，避免镜像站和同一产品分页重复；
- 详情页、文章页、官网页、企业页和注册详情页可进入候选；
- 站内搜索页、频道列表页和聚合页仍不得进入正式证据。

发现记录必须保存原始响应。无法从搜索页提取直达 URL 时标为 `no_extractable_results`，不得把搜索页自身改成结果 URL。

抓取时 `frontier_id` 与规范化 URL 必须成对命中同一个 frontier 项；validator 会把每个 frontier trace 与 `results.jsonl` 的 URL、target ID、query/provider/discovery ID 和 raw SHA-256 逐项交叉校验。`max_pages_per_domain` 按 `site_key` 下已经预约/尝试的不同规范化 URL 计数；同一 URL 的失败重试复用原 candidate ID，新的 URL 才占用新额度。
