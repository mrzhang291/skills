import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import wiki_compiler


def test_compile_auto_wiki(tmp_path):
    os.environ["KNOWLEDGE_RAG_DIR"] = str(tmp_path / "shared" / "knowledge_rag")
    structured = {
        "filename": "sample.md",
        "summary": "summary",
        "client_name": "Client",
        "project_name": "Project",
        "product_capacity": "cap",
        "quality_summary": "q",
        "objective": "o",
        "process": "p",
        "tags": ["tag1"],
        "entities": ["Entity"],
        "key_data": {"k": "v"},
        "_docling_chunks": [{"heading": "Conclusion", "content": "done"}],
    }
    result = wiki_compiler.compile_auto_wiki(str(tmp_path), "general", structured, "doc1")
    assert result["status"] == "ok"
    assert (tmp_path / "shared" / "wiki" / "general" / "summaries" / "sample.md").exists()
    assert (tmp_path / "shared" / "wiki" / "general" / "index.md").exists()
