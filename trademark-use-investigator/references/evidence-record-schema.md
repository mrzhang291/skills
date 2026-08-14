# 证据记录 Schema 2.0

## 运行配置

`run-config.json` 固定输入、参考文件哈希、执行模式、覆盖门槛和抓取预算。覆盖门槛只能在运行初始化时设定。`execution_profile` 只能为 `quick|forensic`；旧运行缺少该字段时按 `forensic` 处理，避免静默降低既有门槛。

`coverage_requirements.require_each_query_success` 必须与模式一致：Quick 为 `false`，Forensic 为 `true`。Quick 仍在 coverage/report 中列出失败 QUERY_ID，但不让单个被阻断查询阻止完成；Forensic 的失败查询构成覆盖缺口。

`max_results_per_query` 是一个 `QUERY_ID` 跨 provider 的去重 URL 总预算；`max_pages_per_domain` 按保守 `site_key`（常见多级公共后缀的 registrable site）计算不同规范化 URL，兄弟子域共享额度。后者不按重试次数累计。`max_query_count`、`max_probe_attempts`、`max_capture_attempts`、`target_full_pages`、`max_formal_sources` 和视觉预算分别约束查询、廉价探测、完整归档、目标有效页、正式来源和模型调用总量。

Quick 的 `probe-attempts.jsonl` 每个 URL 只保留一个终态记录；验证码、登录、拒绝、超时和内容不符不重试。`discovery/probe-shortlist.json` 只列 `content_valid=true` 的探测页。完整抓取必须携带与 URL/frontier 匹配的 `probe_id`。所有未完整归档的 URL 汇总到 `skipped-links.json`，仅用于透明披露，不是正式网页证据，也不进入 PDF 页面集合。

## 发现记录

`discovery/queries.jsonl` 每行代表一次真实提供方调用：

```json
{
  "schema_version": "2.0",
  "query_id": "Q001",
  "query": "<MARK_NAME> <GOOD>",
  "provider": "bing",
  "provider_key": "bing",
  "source_kind": "browser_search",
  "executed_at": "ISO-8601",
  "state": "normal",
  "result_count": 10,
  "raw_file": "discovery/raw/Q001-bing.json",
  "raw_sha256": "...",
  "diagnostic_dir": "discovery/providers/Q001-bing",
  "errors": []
}
```

`discovery/results.jsonl` 每行必须是非搜索页目标 URL：

```json
{
  "record_type": "discovered_target_url",
  "discovery_id": "Q001-BING-001",
  "query_id": "Q001",
  "provider": "bing",
  "provider_key": "bing",
  "raw_sha256": "...",
  "rank": 1,
  "title": "页面标题",
  "snippet": "搜索摘要，仅用于筛选",
  "result_url": "原始结果URL",
  "normalized_url": "规范化直达URL",
  "target_id": "T...",
  "domain": "shop.example.com",
  "site_key": "example.com"
}
```

## 候选元数据

`candidate-pages/<ID>/metadata.json` 至少包含：

```json
{
  "schema_version": "2.0",
  "record_type": "target_page_capture",
  "candidate_id": "C001",
  "frontier_id": "T...",
  "source_mode": "discovered_frontier",
  "source_provenance": {"source_mode": "discovered_frontier", "frontier_id": "T..."},
  "expected_assertions": ["<MARK_NAME>"],
  "discovered_via": [],
  "requested_url": "https://...",
  "normalized_url": "https://...",
  "requested_normalized_url": "https://...",
  "final_url": "https://...",
  "final_normalized_url": "https://...",
  "redirect_external_domain": false,
  "redirect_chain": [],
  "http_status": 200,
  "title": "...",
  "captured_at": "ISO-8601",
  "page_state": "normal",
  "content_valid": true,
  "candidate_accepted": true,
  "search_result_page": false,
  "text_chars": 1200,
  "main_content_chars": 900,
  "substantive_image_count": 3,
  "commercial_signals": ["price", "purchase_action"],
  "offline_replay": {
    "ok": true,
    "external_request_count": 0,
    "text_retention": 0.98,
    "image_preservation_rate": 1.0
  },
  "artifacts": {
    "singlefile_html": {"path": "page.singlefile.html", "size_bytes": 1, "sha256": "..."}
  }
}
```

`page_state` 允许 `normal` 或 `manual_visual_review` 进入候选。`captcha`、`login_required`、`access_denied`、`empty_shell`、`empty_results`、`unexpected_content` 和 `search_result_page` 只能存在于诊断记录。

`candidate_id` 为 1–64 位安全 ASCII 标识，除路径穿越外还禁止 Win32 保留设备名和尾点/尾空格。`capture-attempts.jsonl` 每个 candidate/规范化 URL 只保留一个汇总行；浏览器调用前先写 `started`，终态原子更新同一 history 项。崩溃遗留的 `started` 占站点预算，严格验证拒绝未终结记录。`storage-state`/浏览器 profile 必须位于 RUN_DIR 外，证据包只记录 basename 与绝对路径字符串的 SHA-256，不复制内容或绝对路径。

## 视觉审查

每个 `visual_review` 必须记录 `schema_version=2.0`、`record_type=visual_review`、与目录一致的 `candidate_id`、与文件 stem 一致且候选内唯一的 `review_id`、合法 `recorded_at` 和 `primary|secondary|tie_breaker` 角色。模型身份包括 NFKC+casefold 归一化的 `model_name`、`model_identity.model_provider`、`canonical_model_id`、`model_family`、候选内唯一 `invocation_id`，以及可选的回执 SHA-256；它们是审计声明而非密码学身份证明。

`inputs` 按角色分为：至少一项、路径限于 `RUN_DIR/reference` 的 `reference`；必须精确等于候选 `metadata.artifacts.fullpage` 的 `fullpage`；至少一项、路径限于候选目录且不同于 fullpage 的 `regions`。每项保存路径、大小与 SHA-256，并对整个角色化集合计算 `input_set_sha256`。active primary/secondary/tie 必须使用相同输入集。记录还必须包含 `trademark_visible`、`trademark_match.classification/score`、`commercial_use`、`goods_match`、`page_actor`、`owner_attribution` 和非空可见事实理由。时间不得显著早于 run 创建时间，验证时也不得晚于当前 UTC 超过 5 分钟。

`consensus.json` 状态为：

- `single_model_manual_review`；
- `invalid_role_assignment`；
- `invalid_review_set`；
- `inconclusive`；
- `confirmed`；
- `confirmed_non_positive`；
- `conflict`。

`primary` 与 `secondary` 必须是两个不同的归一化模型家族。所有关键字段一致、输入集一致、分差不超过 15 且没有 `unclear` 才可形成确定结论；一致的非正面结论使用 `confirmed_non_positive` 且 `positive_evidence_eligible=false`。只有商标可见、分类为 `exact|near`、商业使用与指定商品匹配均为 `yes`、`owner_attribution=owner`，且归一化 `page_actor` 精确等于 `run-config.json.trademark.owner` 时才使用正面 `confirmed`；正面 evidence 还须 `confidence=high`，并要求支撑最终分数的每个模型分数均不低于 75。`authorized_party` 不能自动变成正面 evidence。

`tie_breaker` 仅在前两角色有分歧时有效，且必须是第三个不同模型家族。它的 `tie_binding` 必须绑定当时 active primary/secondary 的 review ID 与确定性内容指纹，时间晚于两者且输入集相同。任一基础 review 更新或内容变化后旧 tie 失效，不能参与新共识，状态保持 `conflict`。`resolution` 保存逐字段三方值、结果、支持 review ID、规则与 unresolved 列表，`tie_breaker_used` 明确裁决是否实际参与。

入口按 `run-config.json.budgets.max_visual_candidates` 对具有视觉记录的唯一 candidate ID 计数。同一 candidate 追加角色不重复计数，新 candidate 超预算时不写入记录。

## 正式来源

`source-pages/<ID>/metadata.json` 在候选基础上增加：

```json
{
  "record_type": "target_page",
  "source_id": "E001",
  "origin_candidate_id": "C001",
  "order": 1,
  "label": "...",
  "page_role": "evidence",
  "evidence_accepted": true,
  "accepted_at": "ISO-8601",
  "visual_consensus": {}
}
```

`page_role` 只能是：

- `evidence`：可能支持目标商标实际使用；
- `supporting`：注册、企业、官网等背景页面；
- `exclusion_reference`：同名、错误主体、颜色/系列名或其他商标页面。

## 结果

`results.json` 的语义内容可由 Agent 撰写，但覆盖数字由完成脚本覆盖：

```json
{
  "schema_version": "2.0",
  "conclusion": "限定范围结论",
  "limitations": [],
  "candidates": [],
  "coverage": {},
  "artifact_model": {
    "primary": "same-session MHTML",
    "portable": "SingleFile HTML",
    "derivative": "PDF"
  }
}
```

每个候选结论必须分别保存 `trademark_match`、`commercial_use`、`page_actor` 和 `owner_attribution`，并指向来源 ID、URL、文件哈希和最终 PDF 页码。

## 验证指标

`validation.json.metrics` 至少包括：

- `formal_sources`；
- `formal_serp_count`；
- `accepted_blocked_count`；
- `empty_shell_accepted_count`；
- `wrong_subject_accepted_count`；
- `offline_replay_pass_rate`；
- `artifact_hash_pass_rate`；
- `discovery_providers`；
- `unique_target_urls`；
- `target_domains`；
- `direct_capture_attempts`；
- `normal_direct_pages`；
- `blank_page_count`；
- `nav_only_repeated_page_count`；
- `execution_profile`；
- `discovery_query_count`；
- `failed_discovery_queries`；
- `visual_candidates`；
- `visual_invocations`。
