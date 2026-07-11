"""Behavioral tests for commands, secrets, and process lifecycle."""

import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class CommandAndProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        snapshots = root / "snapshots"
        snapshots.mkdir()
        self.secrets = root / "secrets.json"
        self.secrets.write_text(json.dumps({"TEST_SECRET": "super-secret-value"}), encoding="utf-8")
        os.environ.update(
            {
                "AUTH_MODE": "disabled",
                "WORKSPACE_ROOT": str(self.workspace),
                "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
                "SNAPSHOT_ROOT": str(snapshots),
                "AUDIT_ROOT": str(snapshots / "audit"),
                "MEMORY_ROOT": str(snapshots / "memory"),
                "SECRET_REFS_PATH": str(self.secrets),
            }
        )
        import app.server

        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        state = self.server.session_state()
        for process_id, record in list(state.processes.items()):
            if record.process.poll() is None:
                record.process.kill()
                record.process.wait(timeout=5)
            state.processes.pop(process_id, None)
        self.tempdir.cleanup()

    def test_argv_mode_does_not_interpret_shell_metacharacters(self) -> None:
        result = json.loads(
            self.server.run_command_advanced(
                ["python", "-c", "import sys; print(sys.argv[1])", "; touch should-not-exist"]
            )
        )
        self.assertEqual(result["stdout"].strip(), "; touch should-not-exist")
        self.assertFalse((self.workspace / "should-not-exist").exists())

    def test_environment_stdin_output_file_and_history(self) -> None:
        result = json.loads(
            self.server.run_command_advanced(
                ["python", "-c", "import os,sys; print(os.environ['VALUE']); print(sys.stdin.read())"],
                environment={"VALUE": "configured"},
                stdin="input-data",
                output_file="logs/result.txt",
            )
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("configured", result["stdout"])
        self.assertIn("input-data", result["stdout"])
        self.assertTrue((self.workspace / "logs/result.txt").exists())
        self.assertIn("python", self.server.command_history())

    def test_invalid_environment_and_argv_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "argv"):
            self.server.run_command_advanced([])
        with self.assertRaisesRegex(ValueError, "Invalid environment variable"):
            self.server.run_command_advanced(["python", "-V"], environment={"BAD-NAME": "x"})

    def test_timeout_returns_controlled_result(self) -> None:
        result = json.loads(
            self.server.run_command_advanced(
                ["python", "-c", "import time; print('started', flush=True); time.sleep(5)"], timeout_seconds=1
            )
        )
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])

    def test_secret_value_is_injected_but_redacted_from_result_and_output_file(self) -> None:
        result = json.loads(
            self.server.run_command_advanced(
                ["python", "-c", "import os; print(os.environ['TEST_SECRET'])"],
                secret_refs=["TEST_SECRET"],
                output_file="secret-output.txt",
            )
        )
        self.assertNotIn("super-secret-value", json.dumps(result))
        self.assertNotIn("super-secret-value", (self.workspace / "secret-output.txt").read_text())
        self.assertIn("[REDACTED]", result["stdout"])

    def test_background_process_output_incremental_reads_and_cleanup(self) -> None:
        started = json.loads(
            self.server.start_process_advanced(
                [
                    "python",
                    "-u",
                    "-c",
                    "import time; print('first', flush=True); time.sleep(.2); print('second', flush=True)",
                ],
                name="lifecycle",
            )
        )
        self.assertEqual(started["process_id"], "lifecycle")
        matched = self.server.wait_for_process_output("lifecycle", "second", timeout_seconds=5)
        self.assertIn("matched: true", matched)
        output = self.server.get_process_output("lifecycle", max_lines=1, since_line=2)
        self.assertIn("2: second", output)
        self.assertIn("lifecycle", self.server.list_processes())
        self.assertIn("Forgot", self.server.forget_process("lifecycle"))
        self.assertEqual(self.server.list_processes(), "No tracked processes.")

    def test_interactive_process_accepts_stdin(self) -> None:
        self.server.start_process_advanced(
            ["python", "-u", "-c", "value=input(); print('echo:'+value, flush=True)"],
            name="interactive",
            allow_stdin=True,
        )
        self.server.send_process_input("interactive", "hello")
        matched = self.server.wait_for_process_output("interactive", "echo:hello", timeout_seconds=5)
        self.assertIn("matched: true", matched)
        self.server.forget_process("interactive")

    def test_forget_running_process_fails_then_stop_succeeds(self) -> None:
        self.server.start_process_advanced(
            ["python", "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            name="running",
        )
        self.server.wait_for_process_output("running", "ready", timeout_seconds=5)
        with self.assertRaisesRegex(ValueError, "still running"):
            self.server.forget_process("running")
        stopped = self.server.stop_process("running")
        self.assertIn("Stopped process", stopped)
        self.server.forget_process("running")

    def test_unknown_process_and_duplicate_name_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown process_id"):
            self.server.get_process_output("missing")
        self.server.start_process_advanced(["python", "-c", "pass"], name="duplicate")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.server.start_process_advanced(["python", "-c", "pass"], name="duplicate")
        for _ in range(50):
            if self.server.session_state().processes["duplicate"].process.poll() is not None:
                break
            time.sleep(0.02)
        self.server.forget_process("duplicate")

    def test_process_secret_output_is_redacted(self) -> None:
        self.server.start_process_advanced(
            ["python", "-u", "-c", "import os; print(os.environ['TEST_SECRET'], flush=True)"],
            name="secret-process",
            secret_refs=["TEST_SECRET"],
        )
        self.server.wait_for_process_output("secret-process", "[REDACTED]", timeout_seconds=5)
        output = self.server.get_process_output("secret-process")
        self.assertNotIn("super-secret-value", output)
        self.server.forget_process("secret-process")

    def test_identity_isolates_process_registries(self) -> None:
        first = self.server.session_state()
        first.processes["owned"] = mock.Mock()
        scopes = set(self.server._identity()[1])
        with (
            mock.patch.object(self.server._core, "_identity", return_value=("second-user", scopes)),
            mock.patch.object(self.server._core, "_workspace_map", return_value={"second-user": [self.workspace]}),
        ):
            second = self.server.session_state()
            self.assertIsNot(first, second)
            self.assertNotIn("owned", second.processes)


if __name__ == "__main__":
    unittest.main()
