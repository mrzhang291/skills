---
name: archon-boss-upload
description: "Upload, encryption, indexing, and Wiki material orchestration for Archon RAG."
---

# Boss Upload Module

Orchestrates the full ingest pipeline: parse -> pending scan -> AI extraction
-> encrypted records -> LanceDB chunks -> docmeta -> metrics -> Wiki materials.

```python
from boss_upload import configure, boss_upload_step1, boss_upload_auto_finalize, boss_upload_get_wiki_data

configure(
    base_dir=os.environ["ARCHON_BASE_DIR"],
    departments={"general": os.environ["ARCHON_DEPT_PASSWORD"]},
)

for file_path in input_files:
    boss_upload_step1(file_path, department="general")

finalize_result = boss_upload_auto_finalize()
wiki_data = boss_upload_get_wiki_data("general")
```

See [upload-workflow.md](references/upload-workflow.md) for the full workflow.

## docmeta Fields

- `client_name`
- `project_name`
- `product_capacity`
- `quality_summary`
- `objective`
- `process`
- `conclusion_chunk_id` (AI-only)
