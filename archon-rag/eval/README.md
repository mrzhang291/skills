# Evaluation

`run_eval.py` measures retrieval quality against a labeled query set.

## Metrics

- `recall@k`: how many expected documents appear in the top-k retrieved results.
- `MRR`: reciprocal rank of the first expected hit.
- `hit_queries`: queries with at least one expected hit.

## Build a Real Evaluation Set

Create a JSON file like `eval_queries.json`:

```json
{
  "name": "my-knowledge-eval",
  "queries": [
    {
      "id": "q1",
      "query": "哪个项目出水 COD 最低",
      "expected_filenames": ["project-a-report.pdf"]
    }
  ]
}
```

Use `expected_filenames` for document-level hits. `expected_doc_ids` is
optional and should contain docmeta `doc_id` values.

## Run

```powershell
python archon-rag\eval\run_eval.py `
  --base-dir .\archon-data `
  --dept general `
  --dept-password change-me `
  --eval-json archon-rag\eval\eval_queries.json `
  --k 5
```

Start with 50-100 real business questions, then compare models by setting
`ARCHON_EMBEDDING_MODEL` and re-running the same eval set.

The first run downloads the configured embedding model and can take a while.
Use a persistent process or server for repeated interactive queries so the
model is loaded once.

The eval script measures the hybrid retrieval path without rerank. To compare
rerank impact, run the full `search` pipeline with `ARCHON_RERANK=1` and
evaluate the ranked output separately.
