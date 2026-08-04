---
name: archon-report-generator
description: "PDF/Word report rendering for Archon RAG."
---

# Report Generator Module

Renders structured JSON into PDF or Word reports with text sections, findings,
tables, charts, and sources. Content must come from `employee-search` results.

```python
from report_generator import generate_report

output_path = generate_report(summary, format="pdf", output_dir="reports")
```

Charts support `bar`, `pie`, and `line`. A Chinese font such as SimHei or
Microsoft YaHei is required for CJK rendering.
