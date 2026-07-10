"""Regression tests for the web-facing security boundary."""

import importlib
import os
import tempfile
import unittest
import base64
import json
from pathlib import Path


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        self.snapshots = root / "snapshots"
        self.snapshots.mkdir()
        os.environ.update({
            "AUTH_MODE": "disabled",
            "WORKSPACE_ROOT": str(self.workspace),
            "WORKSPACE_MAP_PATH": str(root / "missing-workspaces.json"),
            "SNAPSHOT_ROOT": str(self.snapshots),
            "AUDIT_ROOT": str(self.snapshots / "audit"),
            "MEMORY_ROOT": str(self.snapshots / "memory"),
        })
        import app.server
        self.server = importlib.reload(app.server)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_write_stays_in_assigned_workspace(self) -> None:
        self.server.write_file("safe.txt", "safe")
        self.assertEqual((self.workspace / "safe.txt").read_text(), "safe")

        with self.assertRaises(PermissionError):
            self.server.write_file(str(self.workspace.parent / "outside.txt"), "no")

    def test_snapshot_name_cannot_escape_snapshot_root(self) -> None:
        with self.assertRaises(ValueError):
            self.server.create_snapshot("../../outside")

    def test_sessions_are_scoped_by_identity(self) -> None:
        first = self.server.session_state()
        self.assertEqual(first.subject, "local-dev")
        self.assertEqual(first.current_project, self.workspace)

    def test_binary_atomic_and_diff_primitives(self) -> None:
        payload = b"\x00agent-mcp\xff"
        result = json.loads(self.server.write_binary_file("artifact.bin", base64.b64encode(payload).decode()))
        self.assertEqual(result["size_bytes"], len(payload))
        binary = json.loads(self.server.read_binary_file("artifact.bin"))
        self.assertEqual(base64.b64decode(binary["data_base64"]), payload)
        digest = json.loads(self.server.file_hash("artifact.bin"))
        self.assertEqual(digest["digest"], result["sha256"])

        self.server.atomic_write_file("left.txt", "one\ntwo\n")
        self.server.atomic_write_file("right.txt", "one\nthree\n")
        self.assertIn("-two", self.server.diff_files("left.txt", "right.txt"))

    def test_advanced_command_uses_argv_without_shell(self) -> None:
        result = json.loads(self.server.run_command_advanced(["python", "-c", "print('argv-ok')"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "argv-ok")

    def test_project_context_memory_and_verification(self) -> None:
        self.server.write_file("requirements.txt", "# marker for Python verification\n")
        context = json.loads(self.server.project_context())
        self.assertTrue(context["markers"]["python"])
        self.assertIn("syntax", context["verification_suites"])

        saved = json.loads(self.server.project_memory_set("architecture", "Use isolated workspaces."))
        self.assertIn("architecture", saved["memory_keys"])
        memory = json.loads(self.server.project_memory_get("architecture"))
        self.assertEqual(memory["entry"]["value"], "Use isolated workspaces.")

        verification = json.loads(self.server.project_verify(["syntax"]))
        self.assertTrue(verification["passed"])

    def test_self_improvement_readiness_requires_isolated_workspace(self) -> None:
        readiness = json.loads(self.server.self_improvement_readiness())
        self.assertFalse(readiness["is_isolated_self_workspace"])


if __name__ == "__main__":
    unittest.main()
