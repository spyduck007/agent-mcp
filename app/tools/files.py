"""MCP tools for the files capability group."""

from app.core import (
    MAX_OUTPUT,
    MAX_READ_BYTES,
    READ_ONLY_ANNOTATIONS,
    Path,
    _format_browser_result,
    _is_allowed_path,
    _workspace_map,
    authorize_tool,
    base64,
    difflib,
    hashlib,
    mcp,
    re,
    require_scope,
    resolve_path,
    session_state,
    shutil,
    uuid,
)


@mcp.tool()
def write_file(file_path: str, contents: str) -> str:
    authorize_tool("write_file")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return f"Wrote {len(contents)} characters to {target}"


@mcp.tool()
def append_file(file_path: str, contents: str) -> str:
    authorize_tool("append_file")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("a", encoding="utf-8") as f:
        f.write(contents)

    return f"Appended {len(contents)} characters to {target}"


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def read_file(file_path: str, start_line: int = 1, max_lines: int = 300) -> str:
    authorize_tool("read_file")
    target = resolve_path(file_path)

    if not target.exists():
        raise FileNotFoundError(file_path)

    data = target.read_bytes()[:MAX_READ_BYTES]
    lines = data.decode("utf-8", errors="replace").splitlines()

    start = max(start_line - 1, 0)
    selected = lines[start : start + max_lines]

    return "\n".join(f"{line_no}: {line}" for line_no, line in enumerate(selected, start=start + 1))


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def read_files(file_paths: list[str]) -> str:
    authorize_tool("read_files")
    return "\n\n".join(f"--- {path} ---\n{read_file(path)}" for path in file_paths)


@mcp.tool()
def replace_in_file(
    file_path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int | None = None,
) -> str:
    authorize_tool("replace_in_file")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)

    if count == 0:
        raise ValueError("old_text was not found")

    if expected_replacements is not None and count != expected_replacements:
        raise ValueError(f"Expected {expected_replacements}, found {count}")

    target.write_text(text.replace(old_text, new_text), encoding="utf-8")
    return f"Replaced {count} occurrence(s) in {target}"


@mcp.tool()
def replace_lines(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    authorize_tool("replace_lines")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    before = lines[: start_line - 1]
    after = lines[end_line:]
    replacement = new_content.splitlines()

    target.write_text("\n".join(before + replacement + after) + "\n", encoding="utf-8")
    return f"Replaced lines {start_line}-{end_line} in {target}"


@mcp.tool()
def insert_at_line(file_path: str, line_number: int, content: str) -> str:
    authorize_tool("insert_at_line")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    lines = target.read_text(encoding="utf-8").splitlines()

    index = max(min(line_number - 1, len(lines)), 0)
    lines[index:index] = content.splitlines()

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"Inserted content at line {line_number} in {target}"


@mcp.tool()
def copy_path(source: str, destination: str) -> str:
    authorize_tool("copy_path")
    require_scope("workspace:write")
    src = resolve_path(source)
    dst = resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)

    return f"Copied {src} to {dst}"


@mcp.tool()
def move_path(source: str, destination: str) -> str:
    authorize_tool("move_path")
    require_scope("workspace:write")
    src = resolve_path(source)
    dst = resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} to {dst}"


@mcp.tool()
def delete_path(path: str) -> str:
    authorize_tool("delete_path")
    require_scope("workspace:write")
    target = resolve_path(path)

    if target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory: {target}"

    target.unlink()
    return f"Deleted file: {target}"


@mcp.tool()
def create_directory(directory: str) -> str:
    authorize_tool("create_directory")
    require_scope("workspace:write")
    target = resolve_path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return f"Created directory: {target}"


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_files(directory: str = ".", recursive: bool = True, max_entries: int = 800) -> str:
    authorize_tool("list_files")
    root = resolve_path(directory)
    iterator = root.rglob("*") if recursive else root.iterdir()

    entries = []
    for path in iterator:
        if len(entries) >= max_entries:
            entries.append("... output truncated ...")
            break

        try:
            rel = path.relative_to(session_state().current_project)
        except ValueError:
            rel = path

        entries.append(f"{rel}{'/' if path.is_dir() else ''}")

    return "\n".join(entries)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def tree(directory: str = ".", depth: int = 3, max_entries: int = 400) -> str:
    authorize_tool("tree")
    root = resolve_path(directory)
    output = [f"{root}/"]
    count = 0

    def walk(path: Path, prefix: str, current_depth: int):
        nonlocal count

        if current_depth > depth:
            return

        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))

        for index, entry in enumerate(entries):
            if count >= max_entries:
                output.append("... output truncated ...")
                return

            connector = "└── " if index == len(entries) - 1 else "├── "
            output.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            count += 1

            if entry.is_dir() and entry.name not in {"node_modules", ".git", ".venv", "__pycache__"}:
                extension = "    " if index == len(entries) - 1 else "│   "
                walk(entry, prefix + extension, current_depth + 1)

    walk(root, "", 1)
    return "\n".join(output)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def find_file(pattern: str, directory: str = ".", max_results: int = 200) -> str:
    authorize_tool("find_file")
    root = resolve_path(directory)
    results = []

    for path in root.rglob(pattern):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break
        results.append(str(path))

    return "\n".join(results)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def search_files(query: str, directory: str = ".", max_results: int = 200) -> str:
    authorize_tool("search_files")
    root = resolve_path(directory)
    results = []

    for path in root.rglob("*"):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break

        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, start=1):
            if query in line:
                results.append(f"{path}:{line_no}: {line}")
                break

    return "\n".join(results)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def regex_search(pattern: str, directory: str = ".", max_results: int = 200) -> str:
    authorize_tool("regex_search")
    root = resolve_path(directory)
    rx = re.compile(pattern)
    results = []

    for path in root.rglob("*"):
        if len(results) >= max_results:
            results.append("... output truncated ...")
            break

        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, start=1):
            if rx.search(line):
                results.append(f"{path}:{line_no}: {line}")
                break

    return "\n".join(results)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def find_symbol(name: str, directory: str = ".", max_results: int = 100) -> str:
    authorize_tool("find_symbol")
    pattern = (
        rf"^\s*(def|class|function|const|let|var|async function|export function|export class)\s+{re.escape(name)}\b"
    )
    return regex_search(pattern, directory, max_results)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def stat_path(path: str) -> str:
    authorize_tool("stat_path")
    target = resolve_path(path)
    stat = target.stat()

    return "\n".join(
        [
            f"path: {target}",
            f"type: {'directory' if target.is_dir() else 'file'}",
            f"size_bytes: {stat.st_size}",
            f"modified_time_unix: {stat.st_mtime}",
        ]
    )


@mcp.tool()
def file_hash(file_path: str, algorithm: str = "sha256") -> str:
    """Return a cryptographic hash of a workspace file without returning its contents."""
    authorize_tool("file_hash")
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")
    target = resolve_path(file_path)
    digest = hashlib.new(algorithm)
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return _format_browser_result(
        {"path": str(target), "algorithm": algorithm, "digest": digest.hexdigest(), "size_bytes": target.stat().st_size}
    )


@mcp.tool()
def read_binary_file(file_path: str, max_bytes: int = 500_000) -> str:
    """Read a binary workspace file as bounded base64 data."""
    authorize_tool("read_binary_file")
    target = resolve_path(file_path)
    limit = min(max(max_bytes, 1), MAX_READ_BYTES)
    data = target.read_bytes()[:limit]
    return _format_browser_result(
        {
            "path": str(target),
            "data_base64": base64.b64encode(data).decode("ascii"),
            "size_bytes_returned": len(data),
            "truncated": target.stat().st_size > len(data),
        }
    )


@mcp.tool()
def write_binary_file(file_path: str, data_base64: str, expected_sha256: str | None = None) -> str:
    """Write base64 binary data atomically within the assigned workspace."""
    authorize_tool("write_binary_file")
    require_scope("workspace:write")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise ValueError("data_base64 is not valid base64") from exc
    target = resolve_path(file_path)
    if expected_sha256 and target.exists() and file_hash(file_path).find(expected_sha256) < 0:
        raise ValueError("Existing file hash does not match expected_sha256")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return _format_browser_result(
        {"path": str(target), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    )


@mcp.tool()
def atomic_write_file(file_path: str, contents: str, expected_sha256: str | None = None) -> str:
    """Atomically replace a text file, optionally only when its current SHA-256 matches."""
    authorize_tool("atomic_write_file")
    require_scope("workspace:write")
    target = resolve_path(file_path)
    if expected_sha256 and target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current != expected_sha256:
            raise ValueError("Existing file hash does not match expected_sha256")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(target)
    return _format_browser_result(
        {"path": str(target), "sha256": hashlib.sha256(contents.encode()).hexdigest(), "characters": len(contents)}
    )


@mcp.tool()
def diff_files(left_path: str, right_path: str, context_lines: int = 3) -> str:
    """Return a unified diff between two workspace text files."""
    authorize_tool("diff_files")
    left = resolve_path(left_path)
    right = resolve_path(right_path)
    context = min(max(context_lines, 0), 100)
    diff = difflib.unified_diff(
        left.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
        right.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
        fromfile=str(left),
        tofile=str(right),
        n=context,
    )
    return "".join(diff)[:MAX_OUTPUT]


@mcp.tool()
def search_all_matches(query: str, directory: str = ".", max_results: int = 500, case_sensitive: bool = True) -> str:
    """Search every matching line across workspace text files, rather than only the first match per file."""
    authorize_tool("search_all_matches")
    root = resolve_path(directory)
    needle = query if case_sensitive else query.lower()
    rows: list[str] = []
    for path in root.rglob("*"):
        if len(rows) >= max_results:
            rows.append("... output truncated ...")
            break
        if not path.is_file() or any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        try:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    rows.append(f"{path}:{line_number}: {line}")
                    if len(rows) >= max_results:
                        break
        except OSError:
            continue
    return "\n".join(rows)


@mcp.tool()
def chmod_path(path: str, mode: str) -> str:
    """Set workspace file permissions using an octal mode such as '755'."""
    authorize_tool("chmod_path")
    require_scope("workspace:write")
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        raise ValueError("mode must be a 3- or 4-digit octal string")
    target = resolve_path(path)
    target.chmod(int(mode, 8))
    return f"Set {target} mode to {mode}"


@mcp.tool()
def create_symlink(target: str, link_path: str) -> str:
    """Create a workspace-contained symbolic link. Both target and link must remain in the assigned workspace."""
    authorize_tool("create_symlink")
    require_scope("workspace:write")
    source = resolve_path(target)
    link = resolve_path(link_path)
    if link.exists() or link.is_symlink():
        raise FileExistsError(link_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(source)
    return f"Created symlink {link} -> {source}"


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def read_symlink(path: str) -> str:
    """Return a symbolic link target after validating that it remains in the workspace."""
    authorize_tool("read_symlink")
    require_scope("workspace:read")
    state = session_state()
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else state.current_project / raw

    # Resolve the parent to enforce workspace confinement without following the
    # final path component, which must remain a symlink for inspection.
    link = candidate.parent.resolve() / candidate.name
    roots = _workspace_map().get(state.subject, [])
    if not _is_allowed_path(link, roots):
        raise PermissionError("Path is outside the assigned workspace")
    if not link.is_symlink():
        raise ValueError("Path is not a symbolic link")

    resolved = link.resolve()
    if not _is_allowed_path(resolved, roots):
        raise PermissionError("Symbolic link target is outside the assigned workspace")
    return _format_browser_result(
        {"path": str(link), "link_target": str(link.readlink()), "resolved_target": str(resolved)}
    )


TOOL_EXPORTS = [
    "write_file",
    "append_file",
    "read_file",
    "read_files",
    "replace_in_file",
    "replace_lines",
    "insert_at_line",
    "copy_path",
    "move_path",
    "delete_path",
    "create_directory",
    "list_files",
    "tree",
    "find_file",
    "search_files",
    "regex_search",
    "find_symbol",
    "stat_path",
    "file_hash",
    "read_binary_file",
    "write_binary_file",
    "atomic_write_file",
    "diff_files",
    "search_all_matches",
    "chmod_path",
    "create_symlink",
    "read_symlink",
]
