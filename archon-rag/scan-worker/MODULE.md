---
name: archon-scan-worker
description: "Parallel AI extraction worker for Archon RAG."
---

# Scan Worker Module

Claims pending tasks atomically with `os.rename`, builds AI extraction prompts
from the active profile, and writes structured JSON results.

## Flow

```python
task = claim_next_pending(str(base_dir))
if task:
    result = complete_scan(
        str(base_dir),
        task["pending_id"],
        tags,
        summary,
        entities,
        key_data,
        filename=task["filename"],
        department=task["department"],
        full_text=task["text"],
        tables=task.get("tables", []),
        client_name=client_name,
        project_name=project_name,
        product_capacity=product_capacity,
        quality_summary=quality_summary,
        objective=objective,
        process=process,
    )
```

Extraction fields are described in
[extraction-schema.md](references/extraction-schema.md). Always pass
`full_text` and `tables`; otherwise downstream encryption and indexing lose
source material.
