# Archon RAG Architecture

## Data Flow

```text
Source files
    |
    v
MinerU OCR for image-heavy PDFs -> docling: parse -> chapter filter -> chunks
    |
    v
pending/*.json
    |
    v
scan-worker: claim -> AI extraction -> validation -> structured/*.json
    |
    v
boss-upload finalize:
    - validation gate / dead-letter
    - secure-storage encrypted JSONL
    - knowledge-rag LanceDB chunks + docmeta
    - metrics CSV + conflict detection
    - SHA-256 content dedup
    - auto Wiki compile
    - temp cleanup
    |
    v
wiki: auto summaries/index/concepts/entities + AI refinement
    |
    v
employee-search: directory -> hybrid -> self-doubt -> public results
    |
    v
report-generator: PDF/Word
```

## Runtime Layout

```text
{ARCHON_BASE_DIR}/
├── private/originals/{department}/
├── shared/encrypted/{department}/
├── shared/knowledge_rag/data/lancedb/
├── shared/wiki/{department}/
│   ├── index.md
│   ├── summaries/
│   ├── concepts/
│   └── entities/
└── .agent_data/
    ├── pending/
    ├── processing/
    ├── structured/
    ├── status/
    ├── review/
    ├── dead_letter/
    ├── hashes/
    ├── uploads/
    └── metrics/
```

## Search Stages

1. Directory navigation: match Wiki `index.md`, summaries, concepts, entities.
2. Hybrid retrieval: jieba-aware BM25 + Chinese vector embeddings + RRF over
   LanceDB chunks.
3. Self-doubt recall: BM25 full recall for keyword mentions missed above.

## Evaluation

`eval/run_eval.py` runs a labeled query set and reports `recall@k` and `MRR`.
The embedding model is configurable through `ARCHON_EMBEDDING_MODEL` or the
active profile, defaulting to `BAAI/bge-small-zh-v1.5`.

## Security Model

- Full text is encrypted in `shared/encrypted/{department}/store_*.enc`.
- LanceDB holds searchable chunk text for retrieval.
- Employee output is limited to public metadata, Wiki summaries, and AI
  summaries.
- Departments are isolated by password and by LanceDB table suffix.
