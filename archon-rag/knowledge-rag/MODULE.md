---
name: archon-knowledge-rag
description: "LanceDB hybrid retrieval and metadata indexing for Archon RAG."
---

# Knowledge RAG Module

Provides local hybrid retrieval over LanceDB: BM25 + vector + RRF fusion,
Self-Doubt full recall, optional rerank, and a 7-field docmeta index.

```python
kr_search("throughput", department="general", mode="hybrid", max_results=10)
kr_search("yield", department="general", mode="bm25_full", min_score=0.1)
```

## APIs

- `kr_add_document(...)` - index one chunk
- `kr_search(...)` - unified search entry
- `kr_hybrid_search(...)` - BM25 + vector + RRF
- `kr_self_doubt_search(...)` - BM25 full recall
- `kr_rerank(...)` - optional precision rerank
- `store_doc_meta(meta, dept)` / `search_doc_meta(query, dept)` - metadata layer
- `drill_conclusion(doc_id, dept)` - AI-only conclusion drill
