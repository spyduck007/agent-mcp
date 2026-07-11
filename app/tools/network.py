"""MCP tools for the network capability group."""

from app.core import (
    READ_ONLY_ANNOTATIONS,
    _format_browser_result,
    authorize_tool,
    hashlib,
    mcp,
    require_scope,
    resolve_path,
    socket,
    ssl,
    time,
    urllib,
    urlparse,
)


@mcp.tool()
def fetch_url(url: str, timeout_seconds: int = 10) -> str:
    authorize_tool("fetch_url")
    require_scope("network:fetch")
    request = urllib.request.Request(url, headers={"User-Agent": "coding-agent-mcp"})

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(150_000).decode("utf-8", errors="replace")
            return f"status: {response.status}\n\n{body}"
    except urllib.error.HTTPError as exc:
        body = exc.read(150_000).decode("utf-8", errors="replace")
        return f"status: {exc.code}\n\n{body}"


@mcp.tool()
def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout_seconds: int = 20,
) -> str:
    """Make an HTTP request with method, headers, and optional text/JSON body. Response headers, status, timing, and bounded body are returned."""
    authorize_tool("http_request")
    require_scope("network:fetch")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    started = time.monotonic()
    request = urllib.request.Request(
        url, method=method.upper(), headers=headers or {}, data=body.encode("utf-8") if body is not None else None
    )
    try:
        with urllib.request.urlopen(request, timeout=min(max(timeout_seconds, 1), 120)) as response:
            payload = response.read(150_000)
            return _format_browser_result(
                {
                    "url": response.url,
                    "status": response.status,
                    "headers": dict(response.headers.items()),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "body": payload.decode("utf-8", errors="replace"),
                    "truncated": response.length is not None and response.length > len(payload),
                }
            )
    except urllib.error.HTTPError as exc:
        return _format_browser_result(
            {
                "url": url,
                "status": exc.code,
                "headers": dict(exc.headers.items()),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "body": exc.read(150_000).decode("utf-8", errors="replace"),
            }
        )


@mcp.tool()
def http_download(url: str, destination: str, timeout_seconds: int = 60, max_bytes: int = 20_000_000) -> str:
    """Download a bounded HTTP response into the workspace and return its hash."""
    authorize_tool("http_download")
    require_scope("network:fetch")
    require_scope("workspace:write")
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=min(max(timeout_seconds, 1), 120)) as response:
        data = response.read(min(max(max_bytes, 1), 100_000_000) + 1)
        if len(data) > max_bytes:
            raise ValueError("Download exceeded max_bytes")
        target.write_bytes(data)
        return _format_browser_result(
            {
                "url": response.url,
                "status": response.status,
                "path": str(target),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )


@mcp.tool()
def http_upload(
    url: str, source_file: str, method: str = "PUT", headers: dict[str, str] | None = None, timeout_seconds: int = 60
) -> str:
    """Upload a workspace file as a raw HTTP request body."""
    authorize_tool("http_upload")
    require_scope("network:fetch")
    source = resolve_path(source_file)
    request = urllib.request.Request(url, method=method.upper(), headers=headers or {}, data=source.read_bytes())
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=min(max(timeout_seconds, 1), 120)) as response:
            return _format_browser_result(
                {
                    "url": response.url,
                    "status": response.status,
                    "headers": dict(response.headers.items()),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "body": response.read(150_000).decode("utf-8", errors="replace"),
                }
            )
    except urllib.error.HTTPError as exc:
        return _format_browser_result(
            {
                "url": url,
                "status": exc.code,
                "headers": dict(exc.headers.items()),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "body": exc.read(150_000).decode("utf-8", errors="replace"),
            }
        )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def dns_lookup(hostname: str) -> str:
    """Resolve a hostname to IP addresses."""
    authorize_tool("dns_lookup")
    require_scope("network:fetch")
    results = sorted({entry[4][0] for entry in socket.getaddrinfo(hostname, None)})
    return _format_browser_result({"hostname": hostname, "addresses": results})


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def tcp_check(hostname: str, port: int, timeout_seconds: int = 5) -> str:
    """Check TCP connectivity and report latency."""
    authorize_tool("tcp_check")
    require_scope("network:fetch")
    started = time.monotonic()
    try:
        with socket.create_connection((hostname, port), timeout=min(max(timeout_seconds, 1), 30)):
            return _format_browser_result(
                {
                    "hostname": hostname,
                    "port": port,
                    "reachable": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
    except OSError as exc:
        return _format_browser_result(
            {
                "hostname": hostname,
                "port": port,
                "reachable": False,
                "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def tls_certificate(hostname: str, port: int = 443, timeout_seconds: int = 10) -> str:
    """Inspect a TLS certificate and protocol for a host."""
    authorize_tool("tls_certificate")
    require_scope("network:fetch")
    context = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=min(max(timeout_seconds, 1), 30)) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as secure:
            return _format_browser_result(
                {
                    "hostname": hostname,
                    "port": port,
                    "protocol": secure.version(),
                    "cipher": secure.cipher(),
                    "certificate": secure.getpeercert(),
                }
            )


TOOL_EXPORTS = [
    "fetch_url",
    "http_request",
    "http_download",
    "http_upload",
    "dns_lookup",
    "tcp_check",
    "tls_certificate",
]
