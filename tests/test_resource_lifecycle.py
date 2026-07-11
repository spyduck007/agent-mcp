"""Lifecycle, limit, cleanup, health, and shutdown tests."""

import asyncio
import importlib
import json
import os
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock


class ResourceLifecycleTests(unittest.TestCase):
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
                "MAX_PROCESSES_PER_USER": "32",
                "MAX_BROWSER_SESSIONS_PER_USER": "12",
                "PROCESS_IDLE_TTL_SECONDS": "14400",
                "BROWSER_IDLE_TTL_SECONDS": "7200",
                "FINISHED_PROCESS_RETENTION_SECONDS": "3600",
                "RESOURCE_CLEANUP_INTERVAL_SECONDS": "60",
            }
        )
        import app.server

        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        state = self.server.session_state()
        for record in list(state.processes.values()):
            if record.process.poll() is None:
                record.process.kill()
                record.process.wait(timeout=5)
        self.tempdir.cleanup()

    def browser_record(self, *, last_activity_at: float | None = None):
        page = mock.Mock()
        page.url = "https://example.test"
        context = mock.Mock()
        context.close = mock.AsyncMock()
        browser = mock.Mock()
        browser.close = mock.AsyncMock()
        playwright = mock.Mock()
        playwright.stop = mock.AsyncMock()
        return self.server._core.BrowserSessionRecord(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            created_at=1.0,
            last_activity_at=time.time() if last_activity_at is None else last_activity_at,
            console_messages=deque(),
            page_errors=deque(),
            failed_requests=deque(),
            responses=deque(),
        )

    def process_record(self, process, *, last_activity_at: float, exited_at: float | None = None):
        return self.server._core.ProcessRecord(
            command="test",
            cwd=self.workspace,
            process=process,
            started_at=1.0,
            last_activity_at=last_activity_at,
            exited_at=exited_at,
        )

    def test_resource_status_reports_limits_without_command_contents(self) -> None:
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        record = self.process_record(process, last_activity_at=time.time() - 5)
        record.output.extend(["one", "two"])
        record.total_lines = 2
        state = self.server.session_state()
        state.processes["proc"] = record
        state.browser_sessions["browser"] = self.browser_record()

        result = json.loads(self.server.resource_status())
        self.assertEqual(result["counts"], {"processes": 1, "browser_sessions": 1})
        self.assertEqual(result["limits"]["max_processes_per_user"], 32)
        self.assertNotIn("command", result["processes"][0])
        self.assertEqual(result["processes"][0]["retained_output_characters"], 6)

    def test_eligible_cleanup_removes_finished_and_idle_resources(self) -> None:
        now = 10000.0
        running = mock.Mock(pid=101)
        running.poll.return_value = None
        running.wait.return_value = 0
        exited = mock.Mock(pid=102)
        exited.poll.return_value = 0
        state = self.server.session_state()
        state.processes["idle-running"] = self.process_record(running, last_activity_at=0)
        state.processes["old-exited"] = self.process_record(exited, last_activity_at=0, exited_at=0)
        state.browser_sessions["idle-browser"] = self.browser_record(last_activity_at=0)

        with (
            mock.patch.object(self.server._core, "PROCESS_IDLE_TTL_SECONDS", 10),
            mock.patch.object(self.server._core, "BROWSER_IDLE_TTL_SECONDS", 10),
            mock.patch.object(self.server._core, "FINISHED_PROCESS_RETENTION_SECONDS", 10),
            mock.patch("app.core.os.killpg") as killpg,
        ):
            result = asyncio.run(self.server._core._cleanup_state_resources(state, now=now))

        self.assertEqual(set(result["removed_processes"]), {"idle-running", "old-exited"})
        self.assertEqual(result["closed_browser_sessions"], ["idle-browser"])
        killpg.assert_called_once_with(101, 15)
        self.assertEqual(state.processes, {})
        self.assertEqual(state.browser_sessions, {})

    def test_recent_resources_are_not_cleaned(self) -> None:
        now = time.time()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        state = self.server.session_state()
        state.processes["active"] = self.process_record(process, last_activity_at=now)
        state.browser_sessions["active-browser"] = self.browser_record(last_activity_at=now)

        result = asyncio.run(self.server._core._cleanup_state_resources(state, now=now + 1))
        self.assertEqual(result["removed_processes"], [])
        self.assertEqual(result["closed_browser_sessions"], [])
        self.assertIn("active", state.processes)
        self.assertIn("active-browser", state.browser_sessions)

    def test_zero_disables_automatic_expiration(self) -> None:
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        state = self.server.session_state()
        state.processes["old"] = self.process_record(process, last_activity_at=0)
        state.browser_sessions["old-browser"] = self.browser_record(last_activity_at=0)
        with (
            mock.patch.object(self.server._core, "PROCESS_IDLE_TTL_SECONDS", 0),
            mock.patch.object(self.server._core, "BROWSER_IDLE_TTL_SECONDS", 0),
            mock.patch.object(self.server._core, "FINISHED_PROCESS_RETENTION_SECONDS", 0),
        ):
            result = asyncio.run(self.server._core._cleanup_state_resources(state, now=999999))
        self.assertEqual(result["removed_processes"], [])
        self.assertEqual(result["closed_browser_sessions"], [])

    def test_force_cleanup_closes_everything_and_continues_after_browser_errors(self) -> None:
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        process.wait.return_value = 0
        state = self.server.session_state()
        state.processes["running"] = self.process_record(process, last_activity_at=time.time())
        browser = self.browser_record()
        browser.context.close.side_effect = RuntimeError("close failed")
        state.browser_sessions["browser"] = browser

        with mock.patch("app.core.os.killpg"):
            result = json.loads(asyncio.run(self.server.resource_cleanup(force=True)))
        self.assertEqual(result["stopped_processes"], ["running"])
        self.assertEqual(result["closed_browser_sessions"], ["browser"])
        self.assertEqual(result["errors"][0]["errors"], ["RuntimeError"])
        browser.browser.close.assert_awaited_once()
        browser.playwright.stop.assert_awaited_once()

    def test_process_limit_is_configurable_and_zero_disables_it(self) -> None:
        state = self.server.session_state()
        existing = mock.Mock()
        existing.process.poll.return_value = None
        state.processes["existing"] = existing
        with mock.patch("app.tools.processes.MAX_PROCESSES_PER_USER", 1):
            with self.assertRaisesRegex(RuntimeError, "Process limit reached"):
                self.server.start_process_advanced(["python", "-c", "pass"], name="blocked")

        with mock.patch("app.tools.processes.MAX_PROCESSES_PER_USER", 0):
            started = json.loads(self.server.start_process_advanced(["python", "-c", "pass"], name="allowed"))
        self.assertEqual(started["process_id"], "allowed")
        self.server.session_state().processes["allowed"].process.wait(timeout=5)

    def test_browser_limit_is_configurable(self) -> None:
        state = self.server.session_state()
        state.browser_sessions["existing"] = self.browser_record()
        with mock.patch("app.tools.browser.MAX_BROWSER_SESSIONS_PER_USER", 1):
            with self.assertRaisesRegex(RuntimeError, "Browser-session limit reached"):
                asyncio.run(self.server.browser_session_open("https://example.test", session_id="blocked"))

    def test_process_access_refreshes_activity(self) -> None:
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        record = self.process_record(process, last_activity_at=1.0, exited_at=1.0)
        self.server.session_state().processes["process"] = record
        self.server.get_process_output("process")
        self.assertGreater(record.last_activity_at, 1.0)

    def test_browser_access_refreshes_activity(self) -> None:
        record = self.browser_record(last_activity_at=1.0)
        record.page.title = mock.AsyncMock(return_value="Example")
        record.page.locator.return_value.evaluate_all = mock.AsyncMock(return_value=[])
        record.page.locator.return_value.inner_text = mock.AsyncMock(return_value="body")
        self.server.session_state().browser_sessions["browser"] = record
        asyncio.run(self.server.browser_session_inspect("browser"))
        self.assertGreater(record.last_activity_at, 1.0)

    def test_health_response_is_bounded_and_contains_no_resource_details(self) -> None:
        response = asyncio.run(self.server._core.health_check(mock.Mock()))
        body = json.loads(response.body)
        self.assertEqual(body["status"], "ok")
        self.assertIn("processes", body)
        self.assertNotIn("command", body)
        self.assertNotIn("url", body)

    def test_shutdown_cleanup_runs_through_lifespan(self) -> None:
        cleanup = mock.AsyncMock(return_value=[])
        loop = mock.AsyncMock(side_effect=asyncio.CancelledError)
        with (
            mock.patch.object(self.server._core, "_cleanup_all_resources", cleanup),
            mock.patch.object(self.server._core, "_resource_cleanup_loop", loop),
        ):

            async def exercise():
                async with self.server._core._mcp_lifespan(None):
                    pass

            asyncio.run(exercise())
        cleanup.assert_awaited_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
