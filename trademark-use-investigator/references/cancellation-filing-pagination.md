# 撤三申请材料分页取证

本流程用于客户明确要求“至少三个电商平台、每个查询从第一页起连续五页”的撤三初步调查材料。这是覆盖和交付门槛，不把搜索结果页升级为商标实际使用证据。

## 固定参数

- 默认平台：淘宝、京东、1688；需要替换时显式传入至少三个 `--platform`。
- 每个平台对每项核定商品单独执行“商标名 + 单项商品名”。
- 每个查询默认抓取结果须 1–5。第一页明确显示零结果时，可作为不存在后续分页的受控例外，但必须标记 `explicit_zero_results=true`。
- 必须使用运行目录外按 `Edge → Chrome` 选定并写入状态文件的专用 Profile；不导出 Cookie，不自动处理登录或验证码。

## 运行

```text
python "<SKILL_DIR>/scripts/run-visual-sales-after-login.py" --run-dir "<RUN_DIR>" --profile-directory Default --filing-mode --menu-only --platform taobao --platform jd --platform 1688
```

`--filing-mode` 默认将 `--pages-per-query` 设为 5、将平台门槛设为 3，并允许明确零结果页作为不存在后续页的记录。如需详情页核验，去掉 `--menu-only`。

分步执行时：

```text
node "<SKILL_DIR>/scripts/sales-platform-assisted-discover.mjs" --run-dir "<RUN_DIR>" --browser "<STATE_BROWSER>" --browser-executable "<STATE_BROWSER_EXECUTABLE>" --user-data-dir "<STATE_BROWSER_USER_DATA>" --cdp-endpoint "<STATE_CDP_ENDPOINT>" --profile-directory Default --platform taobao --platform jd --platform 1688 --pages-per-query 5 --include-zero-results
python "<SKILL_DIR>/scripts/validate-sales-pagination.py" --run-dir "<RUN_DIR>" --min-platforms 3 --required-pages-per-query 5 --allow-zero-results --require-all-planned-queries
python "<SKILL_DIR>/scripts/build-related-results-pdf.py" --run-dir "<RUN_DIR>" --menu-only --min-platforms 3 --required-pages-per-query 5
```

## 页级记录

每个 `page_runs[]` 必须包含：

- `page_index`、可见页码 `declared_page_number`、查询词和核定商品；
- 原始/最终 URL、抓取时间、商品数和商品集合签名；
- 上一页到当前页的 URL 变化、商品签名变化和可见页码前进结果；
- 渲染 DOM、MHTML、整页 PNG、PDF、商品 JSON 及 SHA-256。

## 失败规则

- URL 变化但商品签名和可见页码均未变化：标记 `repeated_page`，整个查询不达标。
- 验证码、登录、拒绝、搜索未提交、空壳和无法证明翻页：只进 `capture-diagnostics/`。
- 一个长网页打印成五张 A4 不等于五个搜索结果页。
- `sales-pagination-validation.json.ok` 不为 `true` 时，不得生成或交付连续分页菜单 PDF。
