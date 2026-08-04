---
name: archon-secure-storage
description: "Password-protected encrypted record storage for Archon RAG."
---

# Secure Storage Module

Stores department-level encrypted JSONL records using Fernet AES-256 with
PBKDF2 key derivation. Use it through `boss-upload`; only import directly for
debugging, migration, or verification.

```python
record = {
    "id": "uuid",
    "source_filename": "report.pdf",
    "department": "general",
    "tags": ["confidential", "report"],
    "summary": "Structured summary",
    "full_text": "fragment---fragment",
    "key_data": {"throughput": "120 t/h"},
}

pw_add_record(record, password=os.environ["ARCHON_DEPT_PASSWORD"], department="general")
records = pw_decrypt_store(password=os.environ["ARCHON_DEPT_PASSWORD"], department="general")
```

The encrypted store lives at
`{base_dir}/shared/encrypted/{department}/store_{department}.enc`.
