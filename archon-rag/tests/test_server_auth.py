import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archon_server


def _request(path, token=""):
    req = urllib.request.Request(f"http://127.0.0.1:8899{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=20)


def test_server_auth(tmp_path):
    os.environ["ARCHON_BASE_DIR"] = str(tmp_path)
    os.environ["ARCHON_DEPARTMENT"] = "general"
    os.environ["ARCHON_DEPT_PASSWORD"] = "test"
    os.environ["ARCHON_API_TOKEN"] = "secret"
    server = ThreadingHTTPServer(("127.0.0.1", 8899), archon_server.ArchonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with _request("/health") as resp:
            assert json.loads(resp.read().decode("utf-8"))["status"] == "ok"
        try:
            _request("/find?q=x")
            raise AssertionError("unauthorized should fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        with _request("/status", "secret") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert "pipeline" in data
    finally:
        server.shutdown()
        server.server_close()
