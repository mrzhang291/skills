---
name: archon-rag
description: "General-purpose local RAG skill for uploading, indexing, searching, and reporting on documents. Use when the user wants to build or maintain a document knowledge base, archive/import files, search or compare documents, drill into conclusions, or generate PDF/Word reports from retrieved content."
---

# Archon RAG

Archon RAG is a self-contained, profile-driven document RAG pipeline. It
parses documents, extracts structured metadata with an AI scan step, encrypts
records, builds a local hybrid retrieval index, compiles a Wiki directory, and
exposes a safe search/report interface.

## Modules

- `docling/` - PDF/DOCX/TXT/MD/CSV parsing, chapter filtering, semantic chunks.
- `scan-worker/` - atomic pending-task claiming and AI field extraction.
- `secure-storage/` - password-protected Fernet encrypted JSONL records.
- `knowledge-rag/` - LanceDB BM25 + vector + RRF retrieval and docmeta index.
- `boss-upload/` - upload pipeline orchestration and metrics.
- `employee-search/` - safe search CLI and three-stage retrieval pipeline.
- `report-generator/` - PDF/Word report rendering.

## Reliability and Automation

- Scan results are validated before they become structured tasks; invalid
  output goes to `.agent_data/review/` or is rejected in strict mode.
- Finalize has a validation gate; rejected tasks go to
  `.agent_data/dead_letter/`.
- Content deduplication uses SHA-256, so renamed duplicates are skipped.
- Image-heavy PDFs automatically use MinerU online OCR when
  `MINERU_API_TOKEN` is set.
- Task status and heartbeat files are kept in `.agent_data/status/`.
- Auto Wiki compilation writes summaries, index, concepts, and entities after
  finalize.
- A hot-folder watcher and a persistent HTTP search service are included.
- The HTTP service requires `ARCHON_API_TOKEN` and writes audit logs.
- Backup/restore, index rebuild, and dead-letter retry commands are included.

## Quick Start

```powershell
python -m pip install -r archon-rag\requirements.txt
```

```powershell
$env:ARCHON_BASE_DIR = ".\archon-data"
$env:ARCHON_DEPARTMENT = "general"
$env:ARCHON_DEPT_PASSWORD = "change-me"
$env:ARCHON_PROFILE = "default"

python archon-rag\scripts\archon.py init --base-dir $env:ARCHON_BASE_DIR --dept $env:ARCHON_DEPARTMENT --dept-password $env:ARCHON_DEPT_PASSWORD
python archon-rag\scripts\archon.py upload .\report.pdf
python archon-rag\scripts\archon.py status
python archon-rag\scripts\archon.py finalize
python archon-rag\scripts\archon.py wiki
python archon-rag\scripts\archon.py search "key metric"
python archon-rag\scripts\archon.py report <doc_id> --answer "Summary text" --format pdf
python archon-rag\scripts\archon.py watch --watch-dir .\inbox
python archon-rag\scripts\archon.py serve --port 8765   # JSON API only, no Web UI
python archon-rag\scripts\archon.py backup --base-dir .\archon-data
python archon-rag\scripts\archon.py rebuild --base-dir .\archon-data --dept general --dept-password change-me
python archon-rag\scripts\archon.py retry --base-dir .\archon-data --dept general --dept-password change-me
```

## Operations

- `doctor`: check Python, dependencies, directories, model, MinerU token, and
  queue state.
- `backup` / `restore`: zip and restore the complete runtime data directory.
- `rebuild`: rebuild LanceDB chunks and docmeta from encrypted records.
- `retry`: move dead-letter or review tasks back into the finalize pipeline.
- CI: `ci/run_ci.py` runs compile, pytest, and optional eval thresholds.

## Pipeline

1. `upload` parses each file and creates a pending scan task.
2. Codex or another worker claims pending tasks with `scan-worker`, runs the
   AI extraction prompt, and writes structured JSON.
3. `finalize` encrypts records, writes LanceDB chunks, stores docmeta,
   appends metrics, and cleans temporary files.
4. `wiki` collects compilation material; Codex writes the four-level Wiki:
   `index.md`, `summaries/`, `concepts/`, `entities/`.
5. `search` runs directory navigation, hybrid retrieval, and self-doubt
   recall; `report` renders the final PDF/Word document.

## Profiles

The active domain profile controls chapter rules and scan field descriptions.

- `default`: generic profile; includes common report sections including
  results and discussion.
- `water-treatment`: legacy Document Brain profile; excludes result/discussion
  chapters and keeps the original wastewater chapter whitelist.

Switch with `ARCHON_PROFILE=water-treatment`, or point `ARCHON_CONFIG` at a
custom JSON file. Profiles live in `config/profiles/`.

## Retrieval Quality

- Embedding model: configured by `ARCHON_EMBEDDING_MODEL` or the active
  profile's `embedding_model`. The default is `BAAI/bge-small-zh-v1.5` for
  lightweight local use; set `ARCHON_EMBEDDING_MODEL=BAAI/bge-m3` for higher
  multilingual/Chinese quality.
- Chinese tokenization: BM25 uses jieba segmentation with a pure-Python BM25
  fallback, so Chinese queries are not limited to raw FTS tokenization.
- Rerank: disabled by default for speed. Set `ARCHON_RERANK=1` to enable,
  and `ARCHON_RERANK_MODEL=BAAI/bge-reranker-base` (or another model) to
  configure the cross-encoder.
- Keep `ARCHON_EMBEDDING_MODEL` stable for an existing LanceDB index. Changing
  the model changes vector dimensions and requires rebuilding the index.
- Evaluation: `eval/run_eval.py` computes `recall@k` and `MRR` against
  `eval/eval_queries.json`. Build a real labeled set before changing models.

## Rules

- Always derive paths from `ARCHON_BASE_DIR` / `DOCBRAIN_BASE_DIR`, never
  hard-code machine paths.
- Always pass `full_text` and `tables` through `complete_scan(...)`.
- Employee-facing output must come from the CLI/public record view, Wiki
  summaries, or AI summaries. Do not expose raw encrypted records or raw
  chunk text.
- Do not bypass the pipeline by writing directly to `secure-storage`.
- Keep runtime data outside the skill directory; point `ARCHON_BASE_DIR` at a
  user-controlled data directory.

## Environment

Copy `.env.example` to a secure location and export the values, or set them in
the process environment. `MINERU_API_TOKEN` must be provided at runtime for
image-heavy PDF OCR and must not be committed to source files.
