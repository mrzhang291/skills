import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "knowledge-rag" / "scripts"))

import knowledge_rag_wrapper as kr


def test_bm25_chinese_search(tmp_path):
    os.environ["KNOWLEDGE_RAG_DIR"] = str(tmp_path / "kr")
    kr.configure(str(tmp_path / "kr"))
    kr.set_dept_password("general", "test")
    kr._embed_text = lambda text: [0.0] * kr._embedding_dim()
    kr._db = None
    kr.kr_add_document(
        content="COD去除率和厌氧工艺对比数据",
        filename="a.md",
        department="general",
        tags=["厌氧"],
        key_data={"去除率": "85%"},
    )
    result = kr._kr_bm25_search("去除率", department="general", max_results=5)
    assert result["status"] == "success"
    assert result["result_count"] >= 1
