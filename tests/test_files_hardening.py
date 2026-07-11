"""Behavioral coverage for workspace-confined file operations."""

import base64
import hashlib
import importlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


class FileToolHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        self.outside = root / "outside"
        self.outside.mkdir()
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

    def test_traversal_absolute_and_symlink_escape_are_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            self.server.write_file("../escape.txt", "no")
        with self.assertRaises(PermissionError):
            self.server.read_file(str(self.outside / "escape.txt"))

        (self.workspace / "outside-link").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(PermissionError):
            self.server.write_file("outside-link/escape.txt", "no")
        self.assertFalse((self.outside / "escape.txt").exists())

    def test_nested_edit_copy_move_list_search_and_delete_workflow(self) -> None:
        self.server.create_directory("src/nested")
        self.server.write_file("src/nested/app.py", "def alpha():\n    return 'one'\n")
        self.server.append_file("src/nested/app.py", "\ndef beta():\n    return 'two'\n")
        self.server.insert_at_line("src/nested/app.py", 2, "    # inserted")
        self.server.replace_lines("src/nested/app.py", 3, 3, "    return 'updated'")
        self.server.replace_in_file("src/nested/app.py", "beta", "gamma", expected_replacements=1)

        contents = self.server.read_file("src/nested/app.py")
        self.assertIn("inserted", contents)
        self.assertIn("updated", contents)
        self.assertIn("gamma", contents)
        self.assertIn("app.py", self.server.list_files("src", recursive=True))
        self.assertIn("app.py", self.server.tree("src", depth=3))
        self.assertIn("app.py", self.server.find_file("*.py", "src"))
        self.assertIn("updated", self.server.search_files("updated", "src"))
        self.assertIn("gamma", self.server.regex_search(r"def\s+gamma", "src"))
        self.assertIn("gamma", self.server.find_symbol("gamma", "src"))
        self.assertIn("type: file", self.server.stat_path("src/nested/app.py"))

        self.server.copy_path("src", "copy")
        self.server.move_path("copy/nested/app.py", "moved/app.py")
        self.assertTrue((self.workspace / "moved/app.py").exists())
        self.server.delete_path("copy")
        self.server.delete_path("moved/app.py")
        self.assertFalse((self.workspace / "copy").exists())
        self.assertFalse((self.workspace / "moved/app.py").exists())

    def test_replace_validation_and_missing_file_errors(self) -> None:
        self.server.write_file("sample.txt", "same same")
        with self.assertRaisesRegex(ValueError, "old_text was not found"):
            self.server.replace_in_file("sample.txt", "missing", "x")
        with self.assertRaisesRegex(ValueError, "Expected 1, found 2"):
            self.server.replace_in_file("sample.txt", "same", "x", expected_replacements=1)
        with self.assertRaises(FileNotFoundError):
            self.server.read_file("missing.txt")

    def test_atomic_compare_and_swap_preserves_original_on_mismatch(self) -> None:
        self.server.write_file("atomic.txt", "original")
        wrong = hashlib.sha256(b"wrong").hexdigest()
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.server.atomic_write_file("atomic.txt", "replacement", expected_sha256=wrong)
        self.assertEqual((self.workspace / "atomic.txt").read_text(), "original")

        current = hashlib.sha256(b"original").hexdigest()
        result = json.loads(self.server.atomic_write_file("atomic.txt", "replacement", expected_sha256=current))
        self.assertEqual(result["characters"], len("replacement"))
        self.assertEqual((self.workspace / "atomic.txt").read_text(), "replacement")

    def test_binary_validation_bounds_hash_and_compare_and_swap(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid base64"):
            self.server.write_binary_file("bad.bin", "***")

        payload = b"abcdef"
        encoded = base64.b64encode(payload).decode()
        created = json.loads(self.server.write_binary_file("data.bin", encoded))
        self.assertEqual(created["size_bytes"], 6)
        bounded = json.loads(self.server.read_binary_file("data.bin", max_bytes=3))
        self.assertEqual(base64.b64decode(bounded["data_base64"]), b"abc")
        self.assertTrue(bounded["truncated"])

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.server.write_binary_file("data.bin", base64.b64encode(b"new").decode(), expected_sha256="0" * 64)
        self.assertEqual((self.workspace / "data.bin").read_bytes(), payload)

    def test_chmod_and_symlink_workflow(self) -> None:
        self.server.write_file("target.txt", "target")
        with self.assertRaisesRegex(ValueError, "octal"):
            self.server.chmod_path("target.txt", "99")
        self.server.chmod_path("target.txt", "640")
        self.assertEqual(stat.S_IMODE((self.workspace / "target.txt").stat().st_mode), 0o640)

        self.server.create_symlink("target.txt", "link.txt")
        details = json.loads(self.server.read_symlink("link.txt"))
        self.assertEqual(Path(details["resolved_target"]), self.workspace / "target.txt")
        with self.assertRaises(FileExistsError):
            self.server.create_symlink("target.txt", "link.txt")
        with self.assertRaisesRegex(ValueError, "not a symbolic link"):
            self.server.read_symlink("target.txt")


if __name__ == "__main__":
    unittest.main()
