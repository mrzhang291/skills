# Upload Workflow

Use this reference when running or debugging the Archon RAG upload pipeline.

## Required Pipeline

1. Configure `base_dir`, departments, and department passwords.
2. Run `boss_upload_step1(file_path, department=...)` for each file.
3. Confirm parsing produced filtered chunks, tables, conclusion/advice text,
   and metadata according to the active profile.
4. Use scan-worker to claim each pending task and extract the required fields.
5. Call `complete_scan(...)` with metadata fields plus `full_text` and `tables`.
6. Run `boss_upload_auto_finalize()` to archive originals, encrypt records,
   write retrieval chunks, write docmeta, and update metrics.
7. Run `boss_upload_get_wiki_data(department)` and update the four-level Wiki.

## Final Checks

- Indexed chunks respect the active profile's include/exclude rules.
- `docmeta` contains the visible fields plus `conclusion_chunk_id`.
- `shared/wiki/{department}/index.md` is updated.
- Report uploaded count, skipped duplicates, index status, docmeta status,
  and the Wiki path.
