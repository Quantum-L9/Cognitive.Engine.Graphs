"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [agent]
tags: [cwe-939, http, urllib]
owner: platform
status: active
--- /L9_META ---

Socket HTTP/1.0 client that never calls urllib.urlopen (CWE-939).
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from urllib.parse import urlparse


class HttpResponse:
    def __init__(self, status: int, headers: Message, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> HttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


DEFAULT_PORTS = {"http": 80, "https": 443}


def secure_ssl_context() -> ssl.SSLContext:
    """Default context pinned to TLS 1.2+; TLSv1 and TLSv1.1 stay refused."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def format_authority(host: str, port: int | None) -> str:
    """RFC 3986 authority. IPv6 literals keep their brackets."""
    literal = f"[{host}]" if ":" in host else host
    return literal if port is None else f"{literal}:{port}"


def redact_url(url: str) -> str:
    """Scheme and authority only — never echo userinfo, path, or query to a log."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<unparseable URL>"
    scheme = parsed.scheme or "<no scheme>"
    if not host:
        return f"{scheme}://<no host>"
    return f"{scheme}://{format_authority(host, port)}"


def require_http_url(
    url: str,
    *,
    allowed_http_hosts: frozenset[str],
    label: str = "URL",
) -> str:
    """Refuse file://, userinfo, and unsigned remote http."""
    parsed = urlparse(url)
    safe = redact_url(url)
    if parsed.scheme not in {"http", "https"}:
        msg = f"refusing non-http(s) {label} scheme {parsed.scheme!r}: {safe}"
        raise ValueError(msg)
    if parsed.username or parsed.password:
        msg = f"refusing {label} with userinfo: {safe}"
        raise ValueError(msg)
    host = (parsed.hostname or "").lower()
    if not host:
        msg = f"refusing {label} without host: {safe}"
        raise ValueError(msg)
    if parsed.scheme == "http" and host not in allowed_http_hosts:
        msg = f"refusing non-allowlisted http {label}: {safe}"
        raise ValueError(msg)
    return url


def parse_http_response(raw: bytes) -> tuple[int, str, Message, bytes]:
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise urllib.error.URLError("HTTP response missing header terminator")
    head = raw[:sep]
    body = raw[sep + 4 :]
    lines = head.split(b"\r\n")
    if not lines:
        raise urllib.error.URLError("HTTP response missing status line")
    status_line = lines[0].decode("latin-1", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        raise urllib.error.URLError("HTTP status line unparseable")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise urllib.error.URLError("HTTP status is not an integer") from exc
    reason = parts[2] if len(parts) > 2 else ""
    headers = Message()
    for line in lines[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.decode("latin-1", errors="replace")] = value.decode("latin-1", errors="replace").strip()
    length = headers.get("Content-Length")
    if length is not None:
        try:
            body = body[: int(length)]
        except ValueError as exc:
            raise urllib.error.URLError("HTTP Content-Length is invalid") from exc
    return status, reason, headers, body


def http_exchange(
    req: urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext,
    allowed_http_hosts: frozenset[str],
    label: str = "URL",
) -> HttpResponse:
    """HTTP/1.0 exchange over a raw socket. Never calls urllib.urlopen."""
    url = require_http_url(req.full_url, allowed_http_hosts=allowed_http_hosts, label=label)
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        msg = f"refusing {label} without host: {redact_url(url)}"
        raise ValueError(msg)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = req.get_method()
    payload = req.data if isinstance(req.data, (bytes, bytearray)) else b""
    try:
        if parsed.scheme == "https":
            port = parsed.port or 443
            raw_sock = socket.create_connection((host, port), timeout=timeout)
            sock: socket.socket = context.wrap_socket(raw_sock, server_hostname=host)
        else:
            port = parsed.port or 80
            sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise urllib.error.URLError(exc) from exc
    default_port = DEFAULT_PORTS[parsed.scheme]
    header_host = format_authority(host, None if parsed.port in (None, default_port) else port)
    header_lines = [
        f"{method} {path} HTTP/1.0",
        f"Host: {header_host}",
        "Connection: close",
    ]
    for key, value in req.header_items():
        if key.lower() == "host":
            continue
        header_lines.append(f"{key}: {value}")
    if payload:
        header_lines.append(f"Content-Length: {len(payload)}")
    blob = ("\r\n".join(header_lines) + "\r\n\r\n").encode("latin-1") + bytes(payload)
    try:
        sock.sendall(blob)
        chunks: list[bytes] = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    except OSError as exc:
        raise urllib.error.URLError(exc) from exc
    finally:
        sock.close()
    status, reason, headers, body = parse_http_response(b"".join(chunks))
    if status >= 400:
        raise urllib.error.HTTPError(url, status, reason, headers, BytesIO(body))
    return HttpResponse(status, headers, body)
