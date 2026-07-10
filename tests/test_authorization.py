"""Authorization contract and restricted-scope regression tests."""

import ast
import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AuthorizationContractTests(unittest.TestCase):
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

    def identity_with(self, scopes: set[str]):
        return mock.patch.object(self.server, "_identity", return_value=("local-dev", scopes))

    def test_every_registered_tool_has_contract_and_calls_gate_first(self) -> None:
        module_file = self.server.__file__
        if module_file is None:
            self.fail("app.server has no source file")
        source = Path(module_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        registered: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "mcp"
                    and target.attr == "tool"
                ):
                    registered[node.name] = node
                    break

        self.assertEqual(set(registered), set(self.server.TOOL_SCOPE_REQUIREMENTS))

        for name, node in registered.items():
            body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
            self.assertTrue(body, name)
            first = body[0]
            if not isinstance(first, ast.Expr):
                self.fail(f"{name} does not begin with an authorization expression")
            call = first.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                self.fail(f"{name} does not begin with authorize_tool(...)")
            self.assertEqual(call.func.id, "authorize_tool", name)
            argument = call.args[0]
            if not isinstance(argument, ast.Constant):
                self.fail(f"{name} authorization call has a non-constant tool name")
            self.assertEqual(argument.value, name)

    def test_each_declared_scope_is_independently_required(self) -> None:
        for tool_name, required in self.server.TOOL_SCOPE_REQUIREMENTS.items():
            for missing in required:
                granted = set(required) - {missing}
                with self.subTest(tool=tool_name, missing=missing), self.identity_with(granted):
                    with self.assertRaisesRegex(PermissionError, missing):
                        self.server.authorize_tool(tool_name)

    def test_start_process_requires_command_scope_before_spawning(self) -> None:
        with self.identity_with({"workspace:read"}), mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(PermissionError, "command:run"):
                self.server.start_process("echo denied")
        popen.assert_not_called()

    def test_workspace_write_requires_both_read_and_write_scopes(self) -> None:
        with self.identity_with({"workspace:read"}):
            with self.assertRaisesRegex(PermissionError, "workspace:write"):
                self.server.write_file("denied.txt", "no")
        self.assertFalse((self.workspace / "denied.txt").exists())

        with self.identity_with({"workspace:write"}):
            with self.assertRaisesRegex(PermissionError, "workspace:read"):
                self.server.write_file("denied.txt", "no")
        self.assertFalse((self.workspace / "denied.txt").exists())

    def test_browser_session_identifier_does_not_bypass_browser_scope(self) -> None:
        with self.identity_with(set()):
            with self.assertRaisesRegex(PermissionError, "browser:use"):
                asyncio.run(self.server.browser_session_inspect("guessed-session"))

    def test_network_denial_happens_before_request(self) -> None:
        with self.identity_with(set()), mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(PermissionError, "network:fetch"):
                self.server.http_request("https://example.com")
        urlopen.assert_not_called()

    def test_deployment_denial_happens_before_docker(self) -> None:
        with (
            self.identity_with({"workspace:read", "workspace:write"}),
            mock.patch.object(self.server, "_run_argv") as runner,
        ):
            with self.assertRaisesRegex(PermissionError, "deploy:run"):
                self.server.deployment_preflight()
        runner.assert_not_called()

    def test_github_operation_requires_secret_scope_before_network(self) -> None:
        with self.identity_with({"github:write"}), mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(PermissionError, "secrets:use"):
                self.server.github_create_pull_request("owner/repo", "branch", "title", "body")
        urlopen.assert_not_called()

    def test_secret_backed_command_requires_secrets_scope_before_execution(self) -> None:
        scopes = {"command:run", "workspace:read"}
        with self.identity_with(scopes), mock.patch("subprocess.run") as runner:
            with self.assertRaisesRegex(PermissionError, "secrets:use"):
                self.server.run_command_advanced(["echo", "safe"], secret_refs=["TOKEN"])
        runner.assert_not_called()

    def test_installation_requires_admin_scope_before_shell(self) -> None:
        scopes = {"command:run", "workspace:read", "workspace:write"}
        with self.identity_with(scopes), mock.patch.object(self.server, "shell") as shell:
            with self.assertRaisesRegex(PermissionError, "admin:install"):
                self.server.install_project_dependencies("pip")
        shell.assert_not_called()

    def test_local_development_identity_contains_all_declared_scopes(self) -> None:
        subject, scopes = self.server._identity()
        self.assertEqual(subject, "local-dev")
        declared = {scope for required in self.server.TOOL_SCOPE_REQUIREMENTS.values() for scope in required}
        self.assertTrue(declared.issubset(scopes))

    def test_scope_contract_is_json_serializable_for_documentation(self) -> None:
        serialized = json.dumps(self.server.TOOL_SCOPE_REQUIREMENTS)
        self.assertIn("workspace:read", serialized)


if __name__ == "__main__":
    unittest.main()
