---
name: archon-docling
description: "Document parsing and semantic chunking module for Archon RAG."
---

# Docling Module

Parses PDF, DOCX, TXT, MD, and CSV files into filtered markdown, semantic
chunks, tables, conclusions, and metadata.

Chapter rules are profile-driven. Read
[chapter-rules.md](references/chapter-rules.md) and the active profile before
parsing or debugging.

## APIs

- `dl_parse_document(filepath)` -> markdown, chunks, tables, conclusion, metadata
- `dl_extract_conclusion(filepath)` -> conclusion sections
- `dl_get_document_summary(filepath)` -> section summaries
- `dl_prepare_for_wiki(markdown_content, filename, department)` -> Wiki materials

## Chunking

- Tables stay in `chunk["tables"]`.
- `###` and deeper sub-headings inherit the parent heading.
- Conclusion/recommendation chapters are protected from internal splitting.
- Overlong chapters split at paragraph boundaries.
