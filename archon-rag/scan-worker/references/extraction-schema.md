# Extraction Schema

Use this schema when scanning pending documents after chapter filtering.
The active profile controls which chapters are allowed and how fields are
described in the prompt.

## Field Contract

Return and pass these values into `complete_scan(...)`:

| Field | How to extract |
|---|---|
| `tags` | Explicit technical terms, process names, entities, or abbreviations. |
| `summary` | Summarize only visible allowed sections. |
| `entities` | Preserve company names, project names, product names, abbreviations, and codes exactly. |
| `key_data` | Preserve original metric names, values, and units. Prefer table values. |
| `client_name` | Extract the client/customer name when present. |
| `project_name` | Extract the project name when present. |
| `product_capacity` | Extract product type and capacity/scale when explicitly stated. |
| `quality_summary` | Extract key quality/spec metrics; keep units. |
| `objective` | Extract from the objectives section. |
| `process` | Extract the method/process route. |

If a value is missing, write `原文未提及`; do not infer.

## Evidence Rule

For each important fact, keep a short `source_quote` from the allowed source
chapter. Quote only enough to verify the fact.

## Completion Rule

Always pass `full_text=task["text"]` and `tables=task.get("tables", [])` to
`complete_scan(...)`; otherwise downstream encryption and indexing lose source
material.
