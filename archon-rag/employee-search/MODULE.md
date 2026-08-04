---
name: archon-employee-search
description: "Safe document search and report entry point for Archon RAG."
---

# Employee Search Module

CLI entry for searching the shared knowledge base without exposing raw
encrypted records or raw chunk text.

```powershell
python employee_search.py find "throughput"
python employee_search.py search "project conclusion"
python employee_search.py drill "<doc_id>"
python employee_search.py report "<doc_id>" --format pdf --answer "Pure text summary"
```

## Search Pipeline

1. `find` - structured docmeta search.
2. `search` - directory navigation + hybrid retrieval + self-doubt recall.
3. `drill` - deep look at a document; summarize before returning to the user.
4. `report` - render the final PDF/Word report.

## Output Boundary

Output only public metadata, Wiki summaries, and AI summaries. Never output
raw encrypted content, raw chunk full text, or other department data.
