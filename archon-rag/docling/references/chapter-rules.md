# Chapter Rules

Archon RAG uses profile-driven chapter rules. The active profile is selected
with `ARCHON_PROFILE` (default: `default`) or `ARCHON_CONFIG` for a custom
JSON file. Profiles live in `config/profiles/`.

## Default Profile

- Includes common report sections: Introduction/Background, Objectives,
  Scope/Materials, Methods/Process, Results, Discussion, Conclusion,
  Recommendations.
- Does not exclude result or discussion sections by default.
- Heading matching is alias-based and supports both English and Chinese
  variants.

## Water-Treatment Profile (Legacy)

The `water-treatment` profile preserves the original Document Brain behavior:

- Include: 项目背景, 试验目标, 试验对象, 试验水质, 试验工艺, 结论, 建议.
- Treat `试验总结` as conclusion/advice.
- Exclude result/discussion sections such as `结果与讨论`, `试验结果`,
  `测试结果`, `检测结果`, and `分析结果`.

## Chunking Behavior

- Exclude rules take priority over include rules.
- `###` and deeper sub-headings inherit their parent allowed chapter.
- Conclusion and recommendation chapters are kept as one chunk when possible.
- Tables are stored in `chunk["tables"]` and are not mixed into body text.
- Overlong chapters are split at paragraph boundaries; the allowed chapter
  boundary is preserved.
