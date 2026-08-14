# 销售平台官方 API 发现协议

## 原则

官方 API 只负责发现商品 ID、标题、图片 URL、店铺/供应商字段和直达商品 URL。API 响应不是网页证据；未返回也不等于平台不存在。不得把 AppKey、Secret、access token、Cookie 或签名参数写入 `RUN_DIR`。

优先使用以下能力：

| 平台 | API | 范围 |
|---|---|---|
| 淘宝 | `taobao.tbk.dg.material.optional.upgrade` | 淘宝客/推广目录 |
| 天猫 | `tmall.selected.items.search` | 精选目录，权限依应用而定 |
| 京东 | `jd.union.open.goods.query` | 京东联盟目录 |
| 1688 | `alibaba.product.search`；详情用 `alibaba.product.get` | 应用获权目录 |
| 拼多多 | `pdd.ddk.goods.search`；详情用 `pdd.ddk.goods.detail` | 多多进宝目录 |
| 唯品会 | `UnionGoodsService.goodsListV2` | 联盟在推目录 |
| 抖店 | `/product/listV2`；详情用 `/product/detail` | 仅授权店铺 |
| 快手小店 | 商品管理接口族 | 仅授权店铺 |
| 小红书店铺 | 商家商品管理接口族 | 仅授权店铺 |

苏宁、当当以及没有匹配应用权限的平台，不能假定存在面向普通开发者的全站关键词接口。

## 输入和落盘

1. 对每项核定商品分别使用“商标名 + 单项商品名”，例如 `<MARK_NAME> <GOOD>`。
2. 在 API 调用进程内读取密钥；只把响应 JSON 导出到临时文件，不保存请求头、密钥或 Cookie。
3. 用 `import-official-api-results.py` 导入响应。导入器保存原始响应及 SHA-256，并生成 `discovery/official-api-results.jsonl`。
4. API 名称必须在导入器允许列表中；第三方聚合、抓取接口不得标记为 `official-api`。
5. API 有合法零结果时使用 `--allow-zero-results`，并在结论中披露接口目录范围。

示例：

~~~text
python "<SKILL_DIR>/scripts/import-official-api-results.py" --run-dir "<RUN_DIR>" --platform 1688 --api-name alibaba.product.search --raw-file "<RAW_JSON>" --query-id Q001 --query "<MARK_NAME> <GOOD>" --target-good "<GOOD>" --allow-zero-results
~~~

## 图像先筛选

运行：

~~~text
python "<SKILL_DIR>/scripts/prefilter-api-images.py" --run-dir "<RUN_DIR>" --threshold 0.46
~~~

脚本下载 API 返回的商品图，保存图片 URL、文件、尺寸和 SHA-256，并用企查查原始图样做多尺度局部匹配。以下候选进入短名单：

- 商品图达到视觉阈值；
- API 标题、店铺或供应商字段明确包含商标名；
- 图像下载失败但文字明确命中，状态记为 `text_match_needs_visual_review`。

图片下载错误只进入 `capture-diagnostics/api-image-downloads.jsonl`。

## 直达页归档

只对 `discovery/api-visual-shortlist.json` 的候选运行：

~~~text
python "<SKILL_DIR>/scripts/capture-api-shortlist.py" --run-dir "<RUN_DIR>" --limit 12
~~~

默认使用隔离的 headless Edge。确需登录内容时才传专用非默认 `--edge-user-data`；不得接管系统默认 Edge。正常商品页保存服务器响应 HTML、渲染 DOM、MHTML、SingleFile HTML、商品图片、整页截图、PDF、元数据和哈希。验证码、登录、拒绝、错误和空壳页移动到 `capture-diagnostics/direct-product-pages/`，不得进入 `visual-match-pages` 或交付 PDF。

平台首页站内搜索只在用户明确要求且官方 API/既有直达链接不能满足时作为后备；不得自动启动登录队列。
