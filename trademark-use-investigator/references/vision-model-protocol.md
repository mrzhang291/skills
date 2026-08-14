# 视觉模型调用协议

本文件定义厂商无关的视觉分析输入与输出。按照当前 Agent 可用的视觉模型、MCP 或工具调用，不假设固定厂商和参数名。

## 通用要求

- 尽可能发送原始清晰图片，并另外提供去除大面积空白后的无损裁剪。
- 关闭会改变图像内容的自动美化；允许等比例缩放。
- 要求只输出一个 JSON 对象，不要使用 Markdown 代码块。
- 解析失败时最多重试一次，并在重试中附上解析错误，不要让模型重新发明字段。
- 记录模型或工具名称、调用时间和输入素材 ID。
- 不要求模型给出法律侵权结论。

## 参考商标分析

将参考图和以下指令发送给视觉模型：

```text
你正在分析一枚参考商标，用于后续公开网页中的视觉匹配。

只描述图中实际可见的内容。识别文字、字母、数字、图形、轮廓、构图、相对位置、显著元素、主色以及可能在缩小、裁剪、单色或反色后仍然保留的识别特征。不要根据图形猜测企业、品牌或权利人。

输出严格 JSON，并符合以下结构：
{
  "schema_version": "1.0",
  "trademark_text": ["实际可见文字"],
  "ocr_variants": ["可能的合理识别变体"],
  "design_type": "wordmark|figurative|combined|three_dimensional|unknown",
  "visual_summary": "简洁、客观的整体描述",
  "distinctive_elements": ["具有区分度的元素"],
  "generic_elements": ["不应单独作为吻合依据的通用元素"],
  "layout": "元素的相对位置与构图",
  "dominant_colors": [
    {"name": "颜色名", "hex_approx": "#RRGGBB", "importance": "high|medium|low"}
  ],
  "likely_variations": ["合理变体"],
  "search_terms": {
    "exact": ["精确文字检索词"],
    "expanded": ["结合视觉或语义的扩展词"],
    "visual_descriptions": ["纯图形检索描述"]
  },
  "quality": {
    "resolution": "good|usable|poor",
    "issues": ["模糊、遮挡、背景干扰等"]
  },
  "uncertainties": ["无法可靠确定的内容"]
}
```

## 候选比较

只对已经打开的真实目标页做视觉复核。搜索结果页、搜索缩略图和缓存摘要不能作为候选。将参考商标、候选网页截图、疑似区域裁剪图、候选 ID 以及各输入文件 SHA-256 一起发送：

```text
比较参考商标与候选网页中的疑似标识。先确认候选图中是否真的存在可比较标识，再评价文字、图形轮廓、显著元素、构图和颜色。颜色只能作为辅助依据。不要因为页面正文提到商标名称就认定图样吻合。

同时判断页面呈现的是商品、包装、门店、广告、服务界面等商业使用，还是商标数据库、新闻、百科、案例、评论等普通引用。该判断只针对页面可见证据，不推断法律责任。

坐标使用相对于输入图片宽高的 0 到 1 数值，顺序为 x、y、width、height。无法定位时返回 null。

输出严格 JSON：
{
  "schema_version": "2.0",
  "candidate_id": "输入的候选 ID",
  "input_sha256": ["参考图和候选图哈希"],
  "trademark_visible": "yes|no|unclear",
  "bbox": [0.0, 0.0, 0.0, 0.0],
  "match_type": "exact|near_exact|similar|partial|not_match|unreadable",
  "scores": {
    "text": 0,
    "distinctive_elements": 0,
    "shape_geometry": 0,
    "composition": 0,
    "color_support": 0,
    "overall": 0
  },
  "shared_features": ["实际可见的共同点"],
  "differences": ["实际可见的差异"],
  "alterations": ["缩放、裁剪、变色、旋转、加字等"],
  "use_context": {
    "classification": "commercial_use|non_commercial_reference|uncertain",
    "category": "product|packaging|storefront|advertising|service_interface|resale|registry|editorial|commentary|other|unknown",
    "commercial_signals": ["价格、购买按钮、主体等可见线索"],
    "non_use_signals": ["数据库、新闻、案例等线索"],
    "confidence": 0
  },
  "goods_match": "yes|no|unclear",
  "page_actor": "页面可见销售或发布主体",
  "owner_attribution": "owner|authorized_party|third_party|unrelated|unclear",
  "evidence": ["能够在截图中复核的事实"],
  "uncertainties": ["分辨率、遮挡、主体或时间方面的问题"],
  "recommendation": "high_confidence_candidate|manual_review|reject"
}
```

如果商标没有文字，将 `scores.text` 返回 `null`，不要返回 0。无法读取候选图时使用 `unreadable`，不要猜测。

## 独立复核

第二个模型使用完全相同的参考图、候选整页图、区域裁图和候选 ID，但不要提供第一个模型的输出。两个角色必须记录为 `primary` 和 `secondary`，并使用两个不同的归一化 `model-family`。每次调用必须记录 `model-provider`、`canonical-model-id`、`model-family` 和候选内唯一的 `invocation-id`；可选记录单独保存的调用回执 SHA-256。所有身份字段按 NFKC 与 casefold 归一；这些字段用于审计和阻止明显的同模型复用，不构成远端模型身份的密码学证明。对比时检查：

- `trademark_visible` 是否一致
- `match_type` 是否一致
- `scores.overall` 差值是否超过 15
- `use_context.classification` 是否一致
- `goods_match` 是否一致
- `page_actor` 在压缩空白、忽略大小写后是否一致
- `owner_attribution` 是否一致
- 共同特征是否具体且可以从图片复核

如果当前 Agent 只能调用一个视觉模型，保留该模型的真实结果并标记 `single_model_manual_review`。此时候选只能作为 `supporting`、`exclusion_reference` 或 rejected，不得标为正面 evidence/high，也不得用同一模型家族的第二次调用冒充独立复核。`--allow-manual-review` 不能与 `--page-role evidence` 组合。每个角色的最新结果只按 `recorded_at` 选择；禁止按文件名或 review ID 推断先后。

双方在任何关键字段都没有分歧、分差不超过 15 且 `input_set_sha256` 完全相同，才可形成一致结论。任何关键字段的最终值为 `unclear` 时状态必须是 `inconclusive`，不能是 `confirmed/high`。正面高可信还必须同时满足：商标可见、分类为 `exact/near`、商业使用和指定商品匹配均为 `yes`、`owner_attribution=owner`，且归一化页面主体精确等于 `run-config.json.trademark.owner`。`authorized_party` 只可作为 supporting/人工语义说明；一致的 `different/no/third_party/unrelated/authorized_party` 不得提升为正面 evidence。

## 分歧裁决

只有当前 `primary` 与 `secondary` 确有分歧，且第三个不同视觉模型可用时，才记录 `tie_breaker`。向裁决模型提供原图、候选图和匿名化的两个 JSON 结果，不透露模型名称：

```text
你是视觉证据裁决者。独立查看参考商标和候选图，再检查分析 A 与分析 B。指出哪一项分析更符合图片，或认定现有分辨率不足。不要仅对两个分数求平均。

输出严格 JSON：
{
  "candidate_id": "候选 ID",
  "trademark_visible": "yes|no|unclear",
  "overall_score": 0,
  "match_type": "exact|near_exact|similar|partial|not_match|unreadable",
  "use_classification": "commercial_use|non_commercial_reference|uncertain",
  "goods_match": "yes|no|unclear",
  "page_actor": "页面可见销售或发布主体",
  "owner_attribution": "owner|authorized_party|third_party|unrelated|unclear",
  "reason": "基于可见特征的简短理由",
  "manual_review_required": true
}
```

裁决脚本按字段多数处理。前两者一致的字段保留原值；前两者分歧的字段，第三模型必须明确支持其中一方，否则该字段仍为 unresolved。分数分歧只有在第三模型与至少一方相差不超过 15 时才解决。裁决必须使用相同 `input_set_sha256`，并在 `tie_binding` 保存当时 active primary/secondary 的 review ID 与确定性完整记录指纹；`recorded_at` 必须晚于两条基础记录。任一基础角色后来更新或内容发生变化，旧裁决不得参与，状态继续为 `conflict`。`consensus.json.resolution` 保存每个字段的三方值、最终值、支持 review ID 和采用的规则。

视觉复核候选数受 `run-config.json.budgets.max_visual_candidates` 限制，按已出现视觉复核记录的唯一 candidate ID 计数；同一 candidate 的后续角色复核不额外占用候选预算。所有候选合计的真实复核记录还受 `max_visual_invocations` 限制，Quick 默认为 2 次，不能通过更换 review ID 无限重试。
