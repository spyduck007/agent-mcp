"""Network, database, and deployment failure-path tests."""

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


class FakeResponse:
    def __init__(self, body: bytes = b"ok", status: int = 200, url: str = "https://example.test") -> None:
        self._body = body
        self.status = status
        self.url = url
        self.headers = {"Content-Type": "text/plain"}
        self.length = len(body)

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class NetworkDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.workspace = root / "workspaces" / "local-dev"
        self.workspace.mkdir(parents=True)
        (self.workspace / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        snapshots = root / "snapshots"
        snapshots.mkdir()
        self.secrets = root / "secrets.json"
        self.secrets.write_text(json.dumps({"PG_URL": "postgres://secret-value/db"}), encoding="utf-8")
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
        self.tempdir.cleanup()

    def test_http_request_rejects_non_http_schemes(self) -> None:
        with self.assertRaisesRegex(ValueError, "Only http and https"):
            self.server.http_request("file:///etc/passwd")

    def test_http_success_and_error_responses_are_structured(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"hello")):
            result = json.loads(self.server.http_request("https://example.test"))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], "hello")

        error = urllib.error.HTTPError(
            "https://example.test/missing", 404, "missing", Message(), io.BytesIO(b"not found")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = json.loads(self.server.http_request("https://example.test/missing"))
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["body"], "not found")

    def test_download_limit_does_not_create_partial_file(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"0123456789")):
            with self.assertRaisesRegex(ValueError, "exceeded"):
                self.server.http_download("https://example.test/file", "downloads/file.bin", max_bytes=4)
        self.assertFalse((self.workspace / "downloads/file.bin").exists())

    def test_upload_error_is_reported_without_modifying_source(self) -> None:
        source = self.workspace / "payload.bin"
        source.write_bytes(b"payload")
        error = urllib.error.HTTPError("https://example.test/upload", 500, "failed", Message(), io.BytesIO(b"failure"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = json.loads(self.server.http_upload("https://example.test/upload", "payload.bin"))
        self.assertEqual(result["status"], 500)
        self.assertEqual(source.read_bytes(), b"payload")

    def test_dns_tcp_and_tls_paths(self) -> None:
        with mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 0))]):
            result = json.loads(self.server.dns_lookup("example.test"))
        self.assertEqual(result["addresses"], ["127.0.0.1"])

        with mock.patch("socket.create_connection", side_effect=OSError("refused")):
            result = json.loads(self.server.tcp_check("example.test", 443))
        self.assertFalse(result["reachable"])
        self.assertIn("refused", result["error"])

    def test_database_validation_and_postgres_secret_not_returned(self) -> None:
        with self.assertRaisesRegex(ValueError, "sqlite or postgres"):
            self.server.database_query("mysql", "select 1")
        with self.assertRaisesRegex(ValueError, "workspace database path"):
            self.server.database_query("sqlite", "select 1")
        with self.assertRaisesRegex(ValueError, "secret_ref"):
            self.server.database_query("postgres", "select 1")

        completed = mock.Mock(returncode=1, stdout="", stderr="connection failed for postgres://secret-value/db")
        with mock.patch("subprocess.run", return_value=completed):
            result = json.loads(self.server.database_query("postgres", "select 1", secret_ref="PG_URL"))
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertIn("[REDACTED]", result["stderr"])

    def test_preflight_failure_does_not_snapshot_or_run_status(self) -> None:
        failed = {"exit_code": 1, "stdout": "", "stderr": "missing env", "timed_out": False}
        with (
            mock.patch("app.tools.deployment._run_argv", return_value=failed) as runner,
            mock.patch("app.tools.deployment.create_snapshot") as snapshot,
        ):
            result = json.loads(self.server.deployment_preflight())
        self.assertFalse(result["ready"])
        self.assertIsNone(result["snapshot"])
        self.assertEqual(runner.call_count, 1)
        snapshot.assert_not_called()

    def test_deployment_requires_approval_and_preflight_blocks_apply(self) -> None:
        with mock.patch("app.tools.deployment._run_argv") as runner:
            with self.assertRaisesRegex(PermissionError, "I_APPROVE_DEPLOYMENT"):
                self.server.deployment_apply("no")
        runner.assert_not_called()

        failed = {"exit_code": 1, "stdout": "", "stderr": "bad config", "timed_out": False}
        with mock.patch("app.tools.deployment._run_argv", return_value=failed) as runner:
            result = json.loads(self.server.deployment_apply("I_APPROVE_DEPLOYMENT"))
        self.assertFalse(result["deployed"])
        self.assertEqual(runner.call_count, 1)

    def test_deployment_apply_and_rollback_command_shapes(self) -> None:
        ok = {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}
        with mock.patch("app.tools.deployment._run_argv", side_effect=[ok, ok]) as runner:
            result = json.loads(self.server.deployment_apply("I_APPROVE_DEPLOYMENT", services=["web", "worker"]))
        self.assertTrue(result["deployed"])
        self.assertEqual(runner.call_args_list[1].args[0][-2:], ["web", "worker"])

        with self.assertRaisesRegex(ValueError, "Invalid Compose service"):
            self.server.deployment_apply("I_APPROVE_DEPLOYMENT", services=["bad service"])
        with self.assertRaisesRegex(PermissionError, "I_APPROVE_ROLLBACK"):
            self.server.deployment_rollback("snapshot", "no")

    def test_health_poll_success_and_timeout(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(status=204)):
            result = json.loads(
                self.server.wait_for_http_health("https://health.test", expected_status=204, timeout_seconds=1)
            )
        self.assertTrue(result["healthy"])

        with (
            mock.patch("urllib.request.urlopen", side_effect=OSError("down")),
            mock.patch("app.tools.deployment.time.monotonic", side_effect=[0, 0, 2]),
            mock.patch("app.tools.deployment.time.sleep"),
        ):
            result = json.loads(self.server.wait_for_http_health("https://health.test", timeout_seconds=1))
        self.assertFalse(result["healthy"])
        self.assertEqual(result["last_error"], "down")


if __name__ == "__main__":
    unittest.main()
