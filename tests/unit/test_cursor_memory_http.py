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

import _safe_http  # noqa: E402
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
        ctx = _safe_http.secure_ssl_context()
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
    ctx = _safe_http.secure_ssl_context()
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


@pytest.mark.unit
def test_secure_ssl_context_refuses_tls_below_1_2() -> None:
    for context in (
        _safe_http.secure_ssl_context(),
        cmc.ssl_context,
        cnq._SSL_CONTEXT,
    ):
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.unit
def test_url_errors_never_echo_userinfo() -> None:
    secret = "http://alice:hunter2@example.com/memory"
    with pytest.raises(ValueError) as excinfo:
        cmc._require_http_url(secret)
    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "alice" not in message
    # Exact, not a substring probe: the whole message is the redacted form.
    assert message == "refusing memory URL with userinfo: http://example.com"


@pytest.mark.unit
def test_redact_url_drops_path_query_and_userinfo() -> None:
    assert _safe_http.redact_url("https://u:p@host.example:8443/x?token=abc") == ("https://host.example:8443")
    assert _safe_http.redact_url("file:///etc/passwd") == "file://<no host>"
    assert _safe_http.redact_url("http://[::1]:8000/health") == "http://[::1]:8000"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", None, "127.0.0.1"),
        ("127.0.0.1", 8000, "127.0.0.1:8000"),
        ("::1", None, "[::1]"),
        ("::1", 8000, "[::1]:8000"),
    ],
)
def test_format_authority_brackets_ipv6(host: str, port: int | None, expected: str) -> None:
    assert _safe_http.format_authority(host, port) == expected


@pytest.mark.unit
def test_host_header_keeps_explicit_non_default_port() -> None:
    """A loopback server on an ephemeral port must be addressed host:port."""
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["host"] = self.headers.get("Host", "")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
        with cmc._http_exchange(req, timeout=2, context=_safe_http.secure_ssl_context()):
            pass
    finally:
        server.shutdown()
        server.server_close()

    assert seen["host"] == f"127.0.0.1:{port}"
