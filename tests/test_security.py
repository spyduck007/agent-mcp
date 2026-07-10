"""Regression tests for the web-facing security boundary."""

import importlib
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
