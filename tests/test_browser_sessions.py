"""Persistent browser-session lifecycle tests using async mocks."""

import asyncio
import importlib
import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


class BrowserSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        snapshots = root / "snapshots"
        snapshots.mkdir()
        os.environ.update(
            {
                "AUTH_MODE": "disabled",
                "WORKSPACE_ROOT": str(self.workspace),
                "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
                "SNAPSHOT_ROOT": str(snapshots),
                "AUDIT_ROOT": str(snapshots / "audit"),
                "MEMORY_ROOT": str(snapshots / "memory"),
            }
        )
        import app.server

        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_record(self):
        page = mock.Mock()
        page.url = "https://example.test/current"
        page.title = mock.AsyncMock(return_value="Example")
        page.evaluate = mock.AsyncMock(return_value={"ok": True})
        page.set_default_timeout = mock.Mock()
        page.locator.return_value.set_input_files = mock.AsyncMock()
        page.frame_locator.return_value.locator.return_value.evaluate = mock.AsyncMock(return_value="frame-result")
        context = mock.Mock()
        context.close = mock.AsyncMock()
        context.route = mock.AsyncMock()
        context.tracing = mock.Mock()
        context.tracing.stop = mock.AsyncMock()
        context.tracing.start = mock.AsyncMock()
        browser = mock.Mock()
        browser.close = mock.AsyncMock()
        playwright = mock.Mock()
        playwright.stop = mock.AsyncMock()
        return self.server._core.BrowserSessionRecord(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            created_at=123.0,
            console_messages=deque([{"type": "warning", "text": "warn"}]),
            page_errors=deque(["boom"]),
            failed_requests=deque([{"url": "https://bad"}]),
            responses=deque([{"url": "https://bad", "status": 500}]),
        )

    def test_list_logs_evaluate_storage_and_frame_helpers(self) -> None:
        record = self.make_record()
        self.server.session_state().browser_sessions["session"] = record

        listed = json.loads(self.server.browser_session_list())
        self.assertEqual(listed["sessions"][0]["session_id"], "session")
        logs = json.loads(self.server.browser_session_logs("session"))
        self.assertFalse(logs["ok"])
        self.assertEqual(logs["page_errors"], ["boom"])

        evaluated = json.loads(asyncio.run(self.server.browser_session_evaluate("session", "1+1")))
        self.assertEqual(evaluated["result"], {"ok": True})

        imported = json.loads(
            asyncio.run(
                self.server.browser_session_import_storage(
                    "session", local_storage={"token": "value"}, session_storage={"tab": "one"}
                )
            )
        )
        self.assertEqual(imported["local_storage_keys"], ["token"])

        framed = json.loads(asyncio.run(self.server.browser_session_frame_evaluate("session", "iframe", "() => 1")))
        self.assertEqual(framed["result"], "frame-result")

    def test_unknown_session_and_invalid_route_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown browser session"):
            asyncio.run(self.server.browser_session_inspect("missing"))

        self.server.session_state().browser_sessions["session"] = self.make_record()
        with self.assertRaisesRegex(ValueError, "abort or fulfill"):
            asyncio.run(self.server.browser_session_route("session", "**/*", action="rewrite"))

    def test_close_removes_session_and_attempts_all_cleanup_steps(self) -> None:
        record = self.make_record()
        record.context.close.side_effect = RuntimeError("context close failed")
        self.server.session_state().browser_sessions["session"] = record

        with self.assertRaisesRegex(RuntimeError, "context close failed"):
            asyncio.run(self.server.browser_session_close("session"))

        self.assertNotIn("session", self.server.session_state().browser_sessions)
        record.context.close.assert_awaited_once()
        record.browser.close.assert_awaited_once()
        record.playwright.stop.assert_awaited_once()

    def test_open_navigation_failure_cleans_every_resource(self) -> None:
        page = mock.Mock()
        page.goto = mock.AsyncMock(side_effect=RuntimeError("navigation failed"))
        page.set_default_timeout = mock.Mock()
        context = mock.Mock()
        context.close = mock.AsyncMock()
        context.tracing = mock.Mock()
        context.tracing.start = mock.AsyncMock()
        browser = mock.Mock()
        browser.close = mock.AsyncMock()
        playwright = mock.Mock()
        playwright.stop = mock.AsyncMock()

        starter = mock.Mock()
        starter.start = mock.AsyncMock(return_value=playwright)
        async_playwright = mock.Mock(return_value=starter)

        with (
            mock.patch("app.tools.browser._load_playwright", return_value=async_playwright),
            mock.patch("app.tools.browser._new_browser_page", mock.AsyncMock(return_value=(browser, context, page))),
            mock.patch("app.tools.browser._attach_session_events"),
        ):
            with self.assertRaisesRegex(RuntimeError, "navigation failed"):
                asyncio.run(self.server.browser_session_open("https://example.test", session_id="failed"))

        self.assertNotIn("failed", self.server.session_state().browser_sessions)
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    def test_user_sessions_do_not_share_browser_registries(self) -> None:
        first = self.server.session_state()
        first.browser_sessions["owned"] = self.make_record()
        scopes = set(self.server._identity()[1])
        with (
            mock.patch.object(self.server._core, "_identity", return_value=("other", scopes)),
            mock.patch.object(self.server._core, "_workspace_map", return_value={"other": [self.workspace]}),
        ):
            second = self.server.session_state()
        self.assertNotIn("owned", second.browser_sessions)


if __name__ == "__main__":
    unittest.main()
