"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [cwe-939, memory-client, http]
owner: platform
status: active
--- /L9_META ---

CWE-939: memory and Neo4j CLIs never hand env URLs to urllib.urlopen.
"""

from __future__ import annotations

import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_AGENT_DIR = REPO / "agents" / "cursor"
_CLIENT = _AGENT_DIR / "cursor_memory_client.py"
_NEO4J = _AGENT_DIR / "cursor_neo4j_query.py"
_SAFE_HTTP = _AGENT_DIR / "_safe_http.py"
sys.path.insert(0, str(_AGENT_DIR))

import cursor_memory_client as cmc  # noqa: E402
import cursor_neo4j_query as cnq  # noqa: E402


@pytest.mark.unit
def test_source_does_not_call_urllib_urlopen() -> None:
    for path in (_CLIENT, _NEO4J, _SAFE_HTTP):
        src = path.read_text(encoding="utf-8")
        assert "urllib.request.urlopen" not in src
        assert "from urllib.request import urlopen" not in src


@pytest.mark.unit
def test_require_http_url_rejects_file_and_userinfo() -> None:
    with pytest.raises(ValueError, match="non-http"):
        cmc._require_http_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="userinfo"):
        cmc._require_http_url("http://user:pass@127.0.0.1/memory")


@pytest.mark.unit
def test_require_http_url_rejects_unsigned_remote_http() -> None:
    with pytest.raises(ValueError, match="non-allowlisted"):
        cmc._require_http_url("http://example.com/memory")


@pytest.mark.unit
def test_require_http_url_accepts_c1_and_loopback() -> None:
    assert cmc._require_http_url("http://46.62.243.82/memory").startswith("http://")
    assert cmc._require_http_url("http://127.0.0.1:8000/health").startswith("http://")
    assert cmc._require_http_url("https://memory.example/mcp").startswith("https://")


@pytest.mark.unit
def test_mcp_call_tool_refuses_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmc, "L9_EXECUTOR_API_KEY", "test-key")
    monkeypatch.setattr(cmc, "MCP_URL", "file:///etc/passwd")
    result = cmc.mcp_call_tool("get_memory_stats", {"user_id": "l9-shared"})
    assert "error" in result
    assert "non-http" in result["error"]


@pytest.mark.unit
def test_http_exchange_posts_over_loopback() -> None:
    received: dict[str, bytes | str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received["path"] = self.path
            received["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"success","result":{"ok":true}}')

        def log_message(self, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp/call",
            data=b'{"tool_name":"get_memory_stats"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        with cmc._http_exchange(req, timeout=2, context=ctx) as resp:
            body = resp.read()
        assert b'"ok":true' in body
        assert received["path"] == "/mcp/call"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.unit
def test_http_exchange_maps_connect_error() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
    ctx = ssl.create_default_context()
    with pytest.raises(urllib.error.URLError):
        cmc._http_exchange(req, timeout=1, context=ctx)


@pytest.mark.unit
def test_neo4j_query_refuses_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cnq, "NEO4J_URL", "file:///etc/passwd")
    monkeypatch.setattr(cnq, "NEO4J_PASSWORD", "x")
    result = cnq.query_neo4j("RETURN 1")
    assert "error" in result
    assert "non-http" in result["error"]


@pytest.mark.unit
def test_neo4j_query_refuses_remote_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cnq, "NEO4J_URL", "http://example.com:7474")
    monkeypatch.setattr(cnq, "NEO4J_PASSWORD", "x")
    result = cnq.query_neo4j("RETURN 1")
    assert "non-allowlisted" in result["error"]
