"""MCP tools for the browser capability group."""

from app.core import (
    BROWSER_LOG_LIMIT,
    Any,
    BrowserSessionRecord,
    _append_browser_events,
    _attach_session_events,
    _bounded_timeout_ms,
    _collect_page_summary,
    _evaluate_accessibility_snapshot,
    _evaluate_dom_snapshot,
    _evaluate_form_snapshot,
    _format_browser_result,
    _get_browser_session,
    _load_playwright,
    _new_browser_page,
    _normalize_url,
    _run_browser_action,
    _run_browser_actions,
    _safe_int,
    authorize_tool,
    hashlib,
    mcp,
    require_scope,
    resolve_path,
    session_state,
    time,
    uuid,
)


@mcp.tool()
async def browser_inspect(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    text_limit: int = 8000,
) -> str:
    """Inspect a page: title, final URL, headings, links, buttons, inputs, and visible body text."""
    authorize_tool("browser_inspect")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(await _collect_page_summary(page, include_text=True, text_limit=text_limit))
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_check_errors(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
) -> str:
    """Open a page and collect console errors, page exceptions, failed requests, and non-2xx/3xx responses."""
    authorize_tool("browser_check_errors")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000
    console_messages: list[dict[str, str]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, str]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.on(
            "console",
            lambda msg: (
                console_messages.append({"type": msg.type, "text": msg.text})
                if msg.type in {"error", "warning"}
                else None
            ),
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "requestfailed",
            lambda req: failed_requests.append({"url": req.url, "failure": str(req.failure) if req.failure else ""}),
        )
        page.on(
            "response",
            lambda resp: bad_responses.append({"url": resp.url, "status": resp.status}) if resp.status >= 400 else None,
        )

        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            await page.wait_for_timeout(1000)
            result = await _collect_page_summary(page, include_text=False)
            result.update(
                {
                    "console_messages": console_messages[:100],
                    "page_errors": page_errors[:100],
                    "failed_requests": failed_requests[:100],
                    "bad_responses": bad_responses[:100],
                    "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
                }
            )
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_interact(
    url: str,
    actions: list[dict[str, Any]],
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Run browser actions from a fresh page and return a text DOM summary. Locators can use selector, role/name, text, label, placeholder, test_id, alt_text, or title. Supported actions: click, dblclick, fill, type, press, hover, check, uncheck, select, focus, blur, wait_for_selector, wait_for_text, wait_for_url, wait, goto, reload, assert_text, assert_selector, assert_count, assert_url, assert_title, evaluate."""
    authorize_tool("browser_interact")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        _append_browser_events(page, console_messages, page_errors, failed_requests, bad_responses)

        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions, timeout_ms)
            result = await _collect_page_summary(page, include_text=True, text_limit=text_limit)
            result.update(
                {
                    "actions": action_results,
                    "console_messages": console_messages[:100],
                    "page_errors": page_errors[:100],
                    "failed_requests": failed_requests[:100],
                    "bad_responses": bad_responses[:100],
                    "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
                }
            )
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_evaluate(
    url: str,
    script: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
) -> str:
    """Evaluate JavaScript on a page and return the JSON-serializable result."""
    authorize_tool("browser_evaluate")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = min(max(timeout_seconds, 1), 120) * 1000

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            value = await page.evaluate(script)
            return _format_browser_result(
                {
                    "url": page.url,
                    "title": await page.title(),
                    "result": value,
                }
            )
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_dom_snapshot(
    url: str,
    selector: str = "body",
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 250,
    text_limit: int = 500,
    include_attributes: bool = True,
) -> str:
    """Return a structured, text-only DOM snapshot focused on useful/interactive elements. No screenshots or images."""
    authorize_tool("browser_dom_snapshot")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            data = await _evaluate_dom_snapshot(
                page,
                selector,
                _safe_int(element_limit, 250, 1, 2000),
                _safe_int(text_limit, 500, 1, 5000),
                include_attributes,
            )
            return _format_browser_result(data)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_accessibility_snapshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 250,
    text_limit: int = 500,
) -> str:
    """Return a text-only accessibility-oriented snapshot: roles, names, fields, headings, links, landmarks, and controls."""
    authorize_tool("browser_accessibility_snapshot")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(
                await _evaluate_accessibility_snapshot(
                    page, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000)
                )
            )
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_form_snapshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    element_limit: int = 200,
    text_limit: int = 500,
) -> str:
    """Return form/action/field metadata for a page, including labels, names, placeholders, values, required and disabled flags."""
    authorize_tool("browser_form_snapshot")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            return _format_browser_result(
                await _evaluate_form_snapshot(
                    page, _safe_int(element_limit, 200, 1, 2000), _safe_int(text_limit, 500, 1, 5000)
                )
            )
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_assert(
    url: str,
    assertions: list[dict[str, Any]],
    actions: list[dict[str, Any]] | None = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
) -> str:
    """Run optional actions and assertion actions against a fresh page. Assertion actions include assert_text, assert_selector, assert_count, assert_url, and assert_title."""
    authorize_tool("browser_assert")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    bad_responses: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        _append_browser_events(page, console_messages, page_errors, failed_requests, bad_responses)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions or [], timeout_ms)
            assertion_results = await _run_browser_actions(page, assertions, timeout_ms)
            return _format_browser_result(
                {
                    "url": page.url,
                    "title": await page.title(),
                    "actions": action_results,
                    "assertions": assertion_results,
                    "console_messages": console_messages[:100],
                    "page_errors": page_errors[:100],
                    "failed_requests": failed_requests[:100],
                    "bad_responses": bad_responses[:100],
                    "ok": not console_messages and not page_errors and not failed_requests and not bad_responses,
                }
            )
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_network_trace(
    url: str,
    actions: list[dict[str, Any]] | None = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    max_events: int = 200,
) -> str:
    """Open a page, optionally run actions, and return request/response/failure/console traces as text JSON."""
    authorize_tool("browser_network_trace")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    event_limit = _safe_int(max_events, 200, 1, 2000)
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    console_messages: list[dict[str, Any]] = []
    page_errors: list[str] = []

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        page.set_default_timeout(timeout_ms)
        page.on(
            "request",
            lambda req: (
                requests.append({"url": req.url, "method": req.method, "resource_type": req.resource_type})
                if len(requests) < event_limit
                else None
            ),
        )
        page.on(
            "response",
            lambda resp: (
                responses.append({"url": resp.url, "status": resp.status, "status_text": resp.status_text})
                if len(responses) < event_limit
                else None
            ),
        )
        page.on(
            "requestfailed",
            lambda req: (
                failed_requests.append(
                    {"url": req.url, "method": req.method, "failure": str(req.failure) if req.failure else ""}
                )
                if len(failed_requests) < event_limit
                else None
            ),
        )
        page.on(
            "console",
            lambda msg: (
                console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location})
                if len(console_messages) < event_limit
                else None
            ),
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)) if len(page_errors) < event_limit else None)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            action_results = await _run_browser_actions(page, actions or [], timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
            except Exception:
                pass
            return _format_browser_result(
                {
                    "url": page.url,
                    "title": await page.title(),
                    "actions": action_results,
                    "requests": requests[:event_limit],
                    "responses": responses[:event_limit],
                    "failed_requests": failed_requests[:event_limit],
                    "console_messages": console_messages[:event_limit],
                    "page_errors": page_errors[:event_limit],
                    "bad_responses": [r for r in responses if r.get("status", 0) >= 400][:event_limit],
                    "ok": not failed_requests
                    and not page_errors
                    and not [r for r in responses if r.get("status", 0) >= 400]
                    and not [m for m in console_messages if m.get("type") in {"error", "warning"}],
                }
            )
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_storage_state(
    url: str,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 15,
    include_local_storage: bool = True,
    include_session_storage: bool = True,
    include_cookies: bool = True,
) -> str:
    """Return cookies, localStorage, and sessionStorage for a page after navigation."""
    authorize_tool("browser_storage_state")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)

    async with async_playwright() as p:
        browser, context, page = await _new_browser_page(p, width, height)
        try:
            await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            result: dict[str, Any] = {"url": page.url, "title": await page.title()}
            if include_cookies:
                result["cookies"] = await context.cookies()
            if include_local_storage:
                result["local_storage"] = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
            if include_session_storage:
                result["session_storage"] = await page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
            return _format_browser_result(result)
        finally:
            await context.close()
            await browser.close()


@mcp.tool()
async def browser_session_open(
    url: str,
    session_id: str | None = None,
    width: int = 1280,
    height: int = 720,
    wait_until: str = "load",
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Open a persistent browser session for multi-step testing across MCP calls. Close it with browser_session_close."""
    authorize_tool("browser_session_open")
    async_playwright = _load_playwright()
    target_url = _normalize_url(url)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    session_id = (session_id or str(uuid.uuid4())[:8]).strip()
    if not session_id:
        session_id = str(uuid.uuid4())[:8]
    state = session_state()
    if session_id in state.browser_sessions:
        raise ValueError(f"Browser session already exists: {session_id}")

    playwright = await async_playwright().start()
    browser, context, page = await _new_browser_page(playwright, width, height)
    await context.tracing.start(screenshots=False, snapshots=True, sources=True)
    page.set_default_timeout(timeout_ms)
    record = BrowserSessionRecord(
        playwright=playwright, browser=browser, context=context, page=page, created_at=time.time()
    )
    _attach_session_events(session_id, page, record)
    state.browser_sessions[session_id] = record
    try:
        await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
        summary = await _collect_page_summary(page, include_text=True, text_limit=text_limit)
        summary.update({"session_id": session_id, "created_at_unix": record.created_at})
        return _format_browser_result(summary)
    except Exception:
        state.browser_sessions.pop(session_id, None)
        cleanup_error: Exception | None = None
        for cleanup in (context.close, browser.close, playwright.stop):
            try:
                await cleanup()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise cleanup_error from None
        raise


@mcp.tool()
def browser_session_list() -> str:
    """List open persistent browser sessions."""
    authorize_tool("browser_session_list")
    require_scope("browser:use")
    sessions = []
    for session_id, record in session_state().browser_sessions.items():
        sessions.append(
            {
                "session_id": session_id,
                "url": record.page.url,
                "created_at_unix": record.created_at,
                "console_messages": len(record.console_messages),
                "page_errors": len(record.page_errors),
                "failed_requests": len(record.failed_requests),
                "bad_responses": len(record.responses),
            }
        )
    return _format_browser_result({"sessions": sessions})


@mcp.tool()
async def browser_session_close(session_id: str) -> str:
    """Close a persistent browser session and free its browser process."""
    authorize_tool("browser_session_close")
    record = _get_browser_session(session_id)
    session_state().browser_sessions.pop(session_id, None)
    cleanup_error: Exception | None = None
    for cleanup in (record.context.close, record.browser.close, record.playwright.stop):
        try:
            await cleanup()
        except Exception as exc:
            cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error
    return f"Closed browser session: {session_id}"


@mcp.tool()
async def browser_session_inspect(session_id: str, text_limit: int = 8000) -> str:
    """Inspect the current page in a persistent browser session."""
    authorize_tool("browser_session_inspect")
    record = _get_browser_session(session_id)
    result = await _collect_page_summary(record.page, include_text=True, text_limit=text_limit)
    result.update({"session_id": session_id})
    return _format_browser_result(result)


@mcp.tool()
async def browser_session_interact(
    session_id: str,
    actions: list[dict[str, Any]],
    timeout_seconds: int = 20,
    text_limit: int = 8000,
) -> str:
    """Run actions in an existing persistent browser session and return the updated text DOM summary."""
    authorize_tool("browser_session_interact")
    record = _get_browser_session(session_id)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    record.page.set_default_timeout(timeout_ms)
    action_results = await _run_browser_actions(record.page, actions, timeout_ms)
    result = await _collect_page_summary(record.page, include_text=True, text_limit=text_limit)
    result.update(
        {
            "session_id": session_id,
            "actions": action_results,
            "console_messages": list(record.console_messages)[-100:],
            "page_errors": list(record.page_errors)[-100:],
            "failed_requests": list(record.failed_requests)[-100:],
            "bad_responses": list(record.responses)[-100:],
        }
    )
    return _format_browser_result(result)


@mcp.tool()
async def browser_session_evaluate(session_id: str, script: str) -> str:
    """Evaluate JavaScript in an existing persistent browser session."""
    authorize_tool("browser_session_evaluate")
    record = _get_browser_session(session_id)
    value = await record.page.evaluate(script)
    return _format_browser_result(
        {"session_id": session_id, "url": record.page.url, "title": await record.page.title(), "result": value}
    )


@mcp.tool()
async def browser_session_dom_snapshot(
    session_id: str,
    selector: str = "body",
    element_limit: int = 250,
    text_limit: int = 500,
    include_attributes: bool = True,
) -> str:
    """Return a structured DOM snapshot for the current page in a persistent browser session."""
    authorize_tool("browser_session_dom_snapshot")
    record = _get_browser_session(session_id)
    data = await _evaluate_dom_snapshot(
        record.page,
        selector,
        _safe_int(element_limit, 250, 1, 2000),
        _safe_int(text_limit, 500, 1, 5000),
        include_attributes,
    )
    data["session_id"] = session_id
    return _format_browser_result(data)


@mcp.tool()
async def browser_session_accessibility_snapshot(
    session_id: str,
    element_limit: int = 250,
    text_limit: int = 500,
) -> str:
    """Return an accessibility-oriented snapshot for the current page in a persistent browser session."""
    authorize_tool("browser_session_accessibility_snapshot")
    record = _get_browser_session(session_id)
    data = await _evaluate_accessibility_snapshot(
        record.page, _safe_int(element_limit, 250, 1, 2000), _safe_int(text_limit, 500, 1, 5000)
    )
    data["session_id"] = session_id
    return _format_browser_result(data)


@mcp.tool()
def browser_session_logs(session_id: str, max_entries: int = 100) -> str:
    """Return recent console errors/warnings, page errors, failed requests, and bad responses for a persistent browser session."""
    authorize_tool("browser_session_logs")
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    limit = _safe_int(max_entries, 100, 1, BROWSER_LOG_LIMIT)
    return _format_browser_result(
        {
            "session_id": session_id,
            "url": record.page.url,
            "console_messages": list(record.console_messages)[-limit:],
            "page_errors": list(record.page_errors)[-limit:],
            "failed_requests": list(record.failed_requests)[-limit:],
            "bad_responses": list(record.responses)[-limit:],
            "ok": not record.console_messages
            and not record.page_errors
            and not record.failed_requests
            and not record.responses,
        }
    )


@mcp.tool()
async def browser_session_upload(session_id: str, selector: str, files: list[str]) -> str:
    """Upload one or more workspace files through a file input in a persistent browser session."""
    authorize_tool("browser_session_upload")
    require_scope("workspace:read")
    record = _get_browser_session(session_id)
    paths = [str(resolve_path(path)) for path in files]
    await record.page.locator(selector).set_input_files(paths)
    return _format_browser_result(
        {"session_id": session_id, "selector": selector, "files": paths, "url": record.page.url}
    )


@mcp.tool()
async def browser_session_frame_evaluate(session_id: str, frame_selector: str, script: str) -> str:
    """Evaluate JavaScript inside a selected iframe in a persistent browser session."""
    authorize_tool("browser_session_frame_evaluate")
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    frame = record.page.frame_locator(frame_selector)
    result = await frame.locator("html").evaluate(script)
    return _format_browser_result({"session_id": session_id, "frame_selector": frame_selector, "result": result})


@mcp.tool()
async def browser_session_route(
    session_id: str, url_pattern: str, action: str = "abort", fulfill_body: str | None = None, status: int = 200
) -> str:
    """Persistently intercept browser-session requests: abort them or fulfill them with a controlled text response."""
    authorize_tool("browser_session_route")
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    if action not in {"abort", "fulfill"}:
        raise ValueError("action must be abort or fulfill")

    async def handler(route):
        if action == "abort":
            await route.abort()
        else:
            await route.fulfill(status=status, body=fulfill_body or "", content_type="text/plain")

    await record.context.route(url_pattern, handler)
    return _format_browser_result({"session_id": session_id, "url_pattern": url_pattern, "action": action})


@mcp.tool()
async def browser_session_trace(session_id: str, destination: str) -> str:
    """Export a Playwright trace ZIP for the persistent session into the workspace, then start a new trace segment."""
    authorize_tool("browser_session_trace")
    require_scope("workspace:write")
    record = _get_browser_session(session_id)
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    await record.context.tracing.stop(path=str(target))
    await record.context.tracing.start(screenshots=False, snapshots=True, sources=True)
    return _format_browser_result({"session_id": session_id, "path": str(target), "size_bytes": target.stat().st_size})


@mcp.tool()
async def browser_session_import_storage(
    session_id: str, local_storage: dict[str, str] | None = None, session_storage: dict[str, str] | None = None
) -> str:
    """Set localStorage and sessionStorage values in the current persistent browser-session page."""
    authorize_tool("browser_session_import_storage")
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    await record.page.evaluate(
        """({localStorageValues, sessionStorageValues}) => {
        for (const [key, value] of Object.entries(localStorageValues || {})) localStorage.setItem(key, value);
        for (const [key, value] of Object.entries(sessionStorageValues || {})) sessionStorage.setItem(key, value);
    }""",
        {"localStorageValues": local_storage or {}, "sessionStorageValues": session_storage or {}},
    )
    return _format_browser_result(
        {
            "session_id": session_id,
            "local_storage_keys": sorted((local_storage or {}).keys()),
            "session_storage_keys": sorted((session_storage or {}).keys()),
        }
    )


@mcp.tool()
async def browser_session_download(session_id: str, action: dict[str, Any], destination: str) -> str:
    """Perform a browser action that triggers a download and save the exact downloaded file in the workspace."""
    authorize_tool("browser_session_download")
    require_scope("workspace:write")
    record = _get_browser_session(session_id)
    target = resolve_path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with record.page.expect_download() as download_info:
        await _run_browser_action(record.page, action, 1, _bounded_timeout_ms(20))
    download = await download_info.value
    await download.save_as(str(target))
    data = target.read_bytes()
    return _format_browser_result(
        {
            "session_id": session_id,
            "path": str(target),
            "suggested_filename": download.suggested_filename,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )


@mcp.tool()
async def browser_session_popup(session_id: str, action: dict[str, Any], timeout_seconds: int = 20) -> str:
    """Perform an action expected to open a popup and make that popup the active page of the persistent session."""
    authorize_tool("browser_session_popup")
    require_scope("browser:use")
    record = _get_browser_session(session_id)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    async with record.page.expect_popup(timeout=timeout_ms) as popup_info:
        await _run_browser_action(record.page, action, 1, timeout_ms)
    popup = await popup_info.value
    await popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    record.page = popup
    _attach_session_events(session_id, popup, record)
    summary = await _collect_page_summary(popup, include_text=True)
    summary.update({"session_id": session_id, "popup": True})
    return _format_browser_result(summary)


TOOL_EXPORTS = [
    "browser_inspect",
    "browser_check_errors",
    "browser_interact",
    "browser_evaluate",
    "browser_dom_snapshot",
    "browser_accessibility_snapshot",
    "browser_form_snapshot",
    "browser_assert",
    "browser_network_trace",
    "browser_storage_state",
    "browser_session_open",
    "browser_session_list",
    "browser_session_close",
    "browser_session_inspect",
    "browser_session_interact",
    "browser_session_evaluate",
    "browser_session_dom_snapshot",
    "browser_session_accessibility_snapshot",
    "browser_session_logs",
    "browser_session_upload",
    "browser_session_frame_evaluate",
    "browser_session_route",
    "browser_session_trace",
    "browser_session_import_storage",
    "browser_session_download",
    "browser_session_popup",
]
