"""Regression coverage for unrestricted runtime capabilities."""

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class UnrestrictedPowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        snapshots = root / "snapshots"
        snapshots.mkdir()
        self.command_home = root / "root-home"
        self.tool_root = root / "tools"
        self.host_workspaces_root = root / "host-workspaces"
        os.environ.update(
            {
                "AUTH_MODE": "disabled",
                "WORKSPACE_ROOT": str(self.workspace),
                "HOST_WORKSPACES_ROOT": str(self.host_workspaces_root),
                "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
                "SNAPSHOT_ROOT": str(snapshots),
                "AUDIT_ROOT": str(snapshots / "audit"),
                "MEMORY_ROOT": str(snapshots / "memory"),
                "COMMAND_HOME": str(self.command_home),
                "TOOL_ROOT": str(self.tool_root),
                "ENVIRONMENT_PROFILE_PATH": str(self.command_home / "profiles.json"),
                "UNRESTRICTED_FILESYSTEM": "false",
                "MCP_TOOL_PROFILE": "chatgpt",
                "MCP_MAX_EXPOSED_TOOLS": "100",
                "MCP_TOOL_INCLUDE": "",
                "MCP_TOOL_EXCLUDE": "",
            }
        )
        import app.server

        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        state = self.server.session_state()
        for terminal_id in list(state.terminals):
            self.server.terminal_close(terminal_id, kill=True)
        self.tempdir.cleanup()

    def test_command_environment_uses_persistent_root_home(self) -> None:
        env = self.server._core._command_environment()
        self.assertEqual(env["HOME"], str(self.command_home))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(self.command_home / ".config"))
        self.assertTrue(env["PATH"].startswith(str(self.tool_root / "bin")))

    def test_environment_profiles_round_trip(self) -> None:
        saved = json.loads(self.server.environment_profile_set("ctf", {"FLAG_MODE": "fast"}))
        self.assertIn("ctf", saved["profiles"])
        env = self.server._core._command_environment(profile="ctf")
        self.assertEqual(env["FLAG_MODE"], "fast")
        deleted = json.loads(self.server.environment_profile_delete("ctf"))
        self.assertEqual(deleted["profiles"], [])

    def test_terminal_session_round_trip(self) -> None:
        opened = json.loads(
            self.server.terminal_open(
                ["python", "-u", "-c", "import sys; print('ready'); print(sys.stdin.readline().strip())"],
                session_id="test-terminal",
            )
        )
        self.assertEqual(opened["session_id"], "test-terminal")
        deadline = time.time() + 5
        output = ""
        while "ready" not in output and time.time() < deadline:
            output = json.loads(self.server.terminal_read("test-terminal"))["output"]
            time.sleep(0.05)
        self.assertIn("ready", output)
        self.server.terminal_write("test-terminal", "hello", append_newline=True)
        deadline = time.time() + 5
        while "hello" not in output and time.time() < deadline:
            output = json.loads(self.server.terminal_read("test-terminal"))["output"]
            time.sleep(0.05)
        self.assertIn("hello", output)

    def test_worker_run_constructs_privileged_docker_command(self) -> None:
        fake = {"argv": [], "exit_code": 0, "timed_out": False, "stdout": "ok", "stderr": ""}
        with mock.patch("app.tools.workers._run_argv_with_env", return_value=fake) as runner:
            result = json.loads(
                self.server.worker_run(
                    "python:3.12",
                    ["python", "--version"],
                    privileged=True,
                    network="host",
                    gpus="all",
                )
            )
        argv = runner.call_args.args[0]
        self.assertIn("--privileged", argv)
        self.assertIn("host", argv)
        self.assertIn("all", argv)
        self.assertIn(f"{self.host_workspaces_root}:/workspace", argv)
        self.assertEqual(result["exit_code"], 0)

    def test_worker_mount_auto_detection_uses_longest_matching_mount(self) -> None:
        with mock.patch.dict(os.environ, {"HOST_WORKSPACES_ROOT": ""}, clear=False), mock.patch(
            "app.tools.workers._current_container_mounts",
            return_value=[
                {"Destination": "/workspaces", "Source": "/srv/workspaces"},
                {"Destination": "/workspaces/local-dev", "Source": "/srv/specific"},
            ],
        ):
            translated = self.server.worker_run.__module__
            from app.tools.workers import _host_path_for_container_path

            self.assertEqual(_host_path_for_container_path(self.workspace), Path("/srv/specific"))
            self.assertEqual(translated, "app.tools.workers")

    def test_python_verification_uses_mcp_interpreter(self) -> None:
        from app.tools.project import _verification_argv

        self.assertEqual(_verification_argv(["python", "-m", "pytest"])[0], sys.executable)
        self.assertEqual(_verification_argv(["npm", "run", "test"]), ["npm", "run", "test"])

    def test_chatgpt_profile_stays_below_client_limit_and_keeps_escape_hatches(self) -> None:
        state = self.server.TOOL_PROFILE_STATE
        self.assertEqual(state.profile, "chatgpt")
        self.assertLessEqual(len(state.exposed_tools), 100)
        self.assertLess(len(state.exposed_tools), len(state.all_tools))
        for name in {
            "run_command_advanced",
            "github_cli",
            "package_install",
            "terminal_open",
            "worker_run",
            "project_context",
        }:
            self.assertIn(name, state.exposed_tools)

    def test_hidden_tools_remain_python_callable(self) -> None:
        self.assertIn("git_blame", self.server.TOOL_PROFILE_STATE.hidden_tools)
        self.assertTrue(callable(self.server.git_blame))
        self.assertIn("worker_build", self.server.TOOL_PROFILE_STATE.hidden_tools)
        self.assertTrue(callable(self.server.worker_build))

    def test_full_profile_can_publish_every_tool_when_limit_disabled(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MCP_TOOL_PROFILE": "full", "MCP_MAX_EXPOSED_TOOLS": "0"},
            clear=False,
        ):
            import app.tool_profiles

            state = app.tool_profiles.resolve_tool_profile(self.server.TOOL_PROFILE_STATE.all_tools)
        self.assertEqual(state.profile, "full")
        self.assertEqual(state.exposed_tools, state.all_tools)

    def test_profile_limit_fails_fast(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MCP_TOOL_PROFILE": "chatgpt", "MCP_MAX_EXPOSED_TOOLS": "10"},
            clear=False,
        ):
            import app.tool_profiles

            with self.assertRaisesRegex(RuntimeError, "exceeding MCP_MAX_EXPOSED_TOOLS"):
                app.tool_profiles.resolve_tool_profile(self.server.TOOL_PROFILE_STATE.all_tools)


if __name__ == "__main__":
    unittest.main()
