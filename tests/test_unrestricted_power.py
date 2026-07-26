"""Regression coverage for unrestricted runtime capabilities."""

import importlib
import json
import os
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
        os.environ.update(
            {
                "AUTH_MODE": "disabled",
                "WORKSPACE_ROOT": str(self.workspace),
                "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
                "SNAPSHOT_ROOT": str(snapshots),
                "AUDIT_ROOT": str(snapshots / "audit"),
                "MEMORY_ROOT": str(snapshots / "memory"),
                "COMMAND_HOME": str(self.command_home),
                "TOOL_ROOT": str(self.tool_root),
                "ENVIRONMENT_PROFILE_PATH": str(self.command_home / "profiles.json"),
                "UNRESTRICTED_FILESYSTEM": "false",
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
        self.assertEqual(result["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
