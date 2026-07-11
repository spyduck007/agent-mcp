"""Git and GitHub command-shape and failure-path tests."""

import importlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


class GitAndGitHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        snapshots = root / "snapshots"
        snapshots.mkdir()
        secrets = root / "secrets.json"
        secrets.write_text(json.dumps({"GITHUB_TOKEN": "fake-token-value"}), encoding="utf-8")
        os.environ.update(
            {
                "AUTH_MODE": "disabled",
                "WORKSPACE_ROOT": str(self.workspace),
                "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
                "SNAPSHOT_ROOT": str(snapshots),
                "AUDIT_ROOT": str(snapshots / "audit"),
                "MEMORY_ROOT": str(snapshots / "memory"),
                "SECRET_REFS_PATH": str(secrets),
            }
        )
        import app.server

        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_git_arguments_are_quoted_and_validation_is_enforced(self) -> None:
        with mock.patch("app.tools.git.shell", return_value="ok") as shell:
            self.server.git_checkout("feature/name; echo nope", create=True)
            self.server.git_commit("message with 'quotes'")
            self.server.git_show("HEAD~1")
            self.server.git_blame("path with spaces.py", 2, 4)
        commands = [call.args[0] for call in shell.call_args_list]
        self.assertIn("'feature/name; echo nope'", commands[0])
        self.assertIn("'message with '\"'\"'quotes'\"'\"''", commands[1])
        self.assertIn("-L 2,4", commands[3])

        with self.assertRaisesRegex(ValueError, "action must be"):
            self.server.git_stash("explode")
        with self.assertRaisesRegex(ValueError, "add requires"):
            self.server.git_worktree("add")
        with self.assertRaisesRegex(ValueError, "remove requires"):
            self.server.git_worktree("remove")

    def test_apply_patch_returns_failure_details(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="patch failed")
        with mock.patch("app.tools.git.subprocess.run", return_value=completed) as runner:
            result = self.server.apply_patch("bad patch")
        self.assertIn("exit_code: 1", result)
        self.assertIn("patch failed", result)
        self.assertEqual(runner.call_args.kwargs["input"], "bad patch")

    def test_github_push_hides_token_and_reports_safe_argv(self) -> None:
        command_result = {
            "argv": ["git", "-c", "secret-bearing-header", "push"],
            "exit_code": 1,
            "stdout": "",
            "stderr": "failed",
            "timed_out": False,
        }
        with mock.patch("app.tools.github._run_argv", return_value=command_result) as runner:
            result = json.loads(self.server.github_push_branch("agent/test"))
        self.assertNotIn("fake-token-value", json.dumps(result))
        self.assertEqual(result["result"]["argv"], ["git", "push", "--set-upstream", "origin", "agent/test"])
        self.assertIn("AUTHORIZATION: Basic", runner.call_args.args[0][2])
        with self.assertRaisesRegex(ValueError, "Invalid branch"):
            self.server.github_push_branch("bad branch")

    def test_pull_request_validation_and_http_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/name"):
            self.server.github_create_pull_request("invalid", "head", "title", "body")

        error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/pulls",
            422,
            "unprocessable",
            Message(),
            io.BytesIO(b'{"message":"validation failed"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = json.loads(self.server.github_create_pull_request("owner/repo", "head", "title", "body"))
        self.assertEqual(result["status"], 422)
        self.assertIn("validation failed", result["error"])
        self.assertNotIn("fake-token-value", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
