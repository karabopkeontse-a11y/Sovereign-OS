"""Least-privilege local tools for the SovereignOS agent runtime.

All filesystem access is confined to one workspace's dedicated agent_workspace folder.
No shell, process, network, or arbitrary host-filesystem tools are registered here.
"""
from __future__ import annotations

import re
from pathlib import Path


MAX_TEXT_BYTES = 1_000_000
ALLOWED_READ_EXTENSIONS = {".md", ".txt", ".json", ".csv"}
TOOLS = {
    "workspace.list_files": "read_only",
    "workspace.read_file": "read_only",
    "workspace.create_note": "external_effect",
}


def workspace_root(db_path: str | Path, workspace_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", workspace_id or ""):
        raise ValueError("Invalid workspace identifier.")
    parent = Path(db_path).resolve().parent
    base_candidate = parent / "agent_workspace"
    base_candidate.mkdir(parents=True, exist_ok=True)
    base = base_candidate.resolve()
    if base_candidate.is_symlink() or base.parent != parent:
        raise ValueError("The agent workspace root must be a real folder beside the database.")
    root_candidate = base / workspace_id
    if root_candidate.exists() and root_candidate.is_symlink():
        raise ValueError("Workspace agent folders cannot be symbolic links.")
    root = root_candidate.resolve()
    if root.parent != base:
        raise ValueError("Workspace agent folder escaped its approved root.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_tool_input(tool_name: str, arguments: object) -> dict:
    if tool_name not in TOOLS:
        raise ValueError("This agent tool is not registered.")
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    if tool_name == "workspace.list_files":
        if arguments:
            raise ValueError("workspace.list_files takes no arguments.")
        return {}
    if tool_name == "workspace.read_file":
        if set(arguments) != {"path"} or not isinstance(arguments.get("path"), str):
            raise ValueError("workspace.read_file requires only a relative path.")
        _validate_relative_path(arguments["path"])
        return {"path": arguments["path"]}
    if tool_name == "workspace.create_note":
        if set(arguments) != {"filename", "content"}:
            raise ValueError("workspace.create_note requires filename and content only.")
        filename, content = arguments["filename"], arguments["content"]
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.md", filename):
            raise ValueError("Note filename must be a simple .md filename.")
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 50_000:
            raise ValueError("Note content must be non-empty and no larger than 50 KB.")
        if "\x00" in content:
            raise ValueError("Note content contains an invalid null character.")
        return {"filename": filename, "content": content}
    raise ValueError("This agent tool is not registered.")


def _validate_relative_path(relative_path: str) -> None:
    if not relative_path or "\x00" in relative_path or "\\" in relative_path:
        raise ValueError("Use a non-empty relative path with forward slashes.")
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Absolute paths and parent-directory traversal are not allowed.")
    if candidate.suffix.casefold() not in ALLOWED_READ_EXTENSIONS:
        raise ValueError("Only Markdown, text, JSON and CSV files can be read.")


def _inside_root(root: Path, relative_path: str) -> Path:
    _validate_relative_path(relative_path)
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Requested file resolves outside this workspace.") from exc
    return target


def execute_workspace_tool(tool_name: str, arguments: dict, root: Path) -> dict:
    args = validate_tool_input(tool_name, arguments)
    root = Path(root).resolve()
    if tool_name == "workspace.list_files":
        files = []
        for item in root.rglob("*"):
            if len(files) >= 100:
                break
            if not item.is_file():
                continue
            resolved = item.resolve()
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            if resolved.suffix.casefold() in ALLOWED_READ_EXTENSIONS:
                files.append(relative)
        return {"tool": tool_name, "files": sorted(files), "count": len(files)}
    if tool_name == "workspace.read_file":
        target = _inside_root(root, args["path"])
        if not target.is_file():
            raise FileNotFoundError("The requested file does not exist in this agent workspace.")
        data = target.read_bytes()
        if len(data) > MAX_TEXT_BYTES:
            raise ValueError("The requested file is larger than the 1 MB read limit.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("The requested file is not valid UTF-8 text.") from exc
        return {"tool": tool_name, "path": args["path"], "content": text, "bytes": len(data)}
    if tool_name == "workspace.create_note":
        target = _inside_root(root, args["filename"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(args["content"])
        return {"tool": tool_name, "path": args["filename"],
                "bytes": target.stat().st_size, "created": True}
    raise ValueError("This agent tool is not registered.")


def verify_workspace_tool(tool_name: str, arguments: dict, result: dict, root: Path) -> tuple[bool, str, str]:
    root = Path(root).resolve()
    if tool_name == "workspace.list_files":
        passed = isinstance(result.get("files"), list) and result.get("count") == len(result["files"])
        return passed, "A scoped file listing was returned.", "file-count:" + str(result.get("count", 0))
    if tool_name == "workspace.read_file":
        target = _inside_root(root, arguments["path"])
        passed = target.is_file() and target.stat().st_size == result.get("bytes")
        return passed, "The requested workspace file was read within the size limit.", arguments["path"]
    if tool_name == "workspace.create_note":
        target = _inside_root(root, arguments["filename"])
        passed = target.is_file() and target.read_text(encoding="utf-8") == arguments["content"]
        return passed, "The new note was read back and matched the approved content.", arguments["filename"]
    return False, "No verification rule is registered for this tool.", ""
