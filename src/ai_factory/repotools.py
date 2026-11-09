"""Read-only repository tools for the planners.

The planners never touch the repository themselves: they have no shell and write
only through their replies. They can, however, *read* the project's main clone, so
they can plan against what is already there (an existing code base, a schema, a
reference document) instead of guessing from file names:

- ``list_files(path)``: the files and folders under a folder of the repository;
- ``read_file(path, start_line, max_lines)``: some lines of one text file;
- ``search_repo(text, path)``: lines containing some text, with file and line number.

Everything is confined to the clone: absolute paths, ``..`` and symlinks that lead
outside it are refused, and ``.git`` is never shown. Output is capped so a large
file (say a 1 MB JSON schema) is read in pieces, never all at once.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

NAMES = ("list_files", "read_file", "search_repo")
MAX_LIST = 300  # entries per list_files call
DEFAULT_LINES = 400
MAX_LINES = 1000
MAX_CHARS = 24_000  # per tool result
MAX_LINE_CHARS = 2_000  # a longer line is cut (minified files)
MAX_MATCHES = 100
MAX_SEARCH_BYTES = 5_000_000  # files larger than this are skipped by search_repo
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".mypy_cache"}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files and folders in a folder of the project's "
            "repository (read-only), with file sizes. Use '' for the top level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder, relative to the repo"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read lines of a text file in the project's repository "
            f"(read-only). Returns at most {MAX_LINES} lines and about {MAX_CHARS} "
            "characters per call; read large files in pieces with start_line, or find "
            "the right place first with search_repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File, relative to the repo"},
                    "start_line": {"type": "integer", "description": "First line (from 1)"},
                    "max_lines": {
                        "type": "integer",
                        "description": f"How many lines (default {DEFAULT_LINES})",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo",
            "description": "Find lines containing some text (case-insensitive) in the "
            "project's repository, or in one folder or file of it. Returns "
            f"file:line: text for up to {MAX_MATCHES} matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to look for"},
                    "path": {"type": "string", "description": "Folder or file (default all)"},
                },
                "required": ["text"],
            },
        },
    },
]

# The same tools in the Anthropic Messages API's shape (client tools).
CLAUDE_TOOLS: list[dict[str, Any]] = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS
]


class RepoToolError(ValueError):
    pass


def _resolve(root: Path, rel: str) -> Path:
    rel = (rel or "").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if rel == ".":
        rel = ""
    if rel.startswith("/") or os.path.isabs(rel):
        raise RepoToolError("paths are relative to the repository")
    real_root = root.resolve()
    target = (real_root / rel).resolve()
    if target != real_root and real_root not in target.parents:
        raise RepoToolError("that path is outside the repository")
    parts = target.relative_to(real_root).parts
    if parts and parts[0] == ".git":
        raise RepoToolError(".git is not readable")
    if not target.exists():
        raise RepoToolError(f"{rel or '.'} does not exist")
    return target


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root.resolve()).as_posix()


def _is_text(p: Path) -> bool:
    with p.open("rb") as f:
        return b"\0" not in f.read(8192)


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


def list_files(root: Path, path: str = "") -> str:
    target = _resolve(root, path)
    if not target.is_dir():
        raise RepoToolError(f"{path} is a file; use read_file")
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    lines: list[str] = []
    for p in entries:
        if p.name == ".git":
            continue
        if p.is_dir():
            lines.append(f"{_rel(root, p)}/")
        else:
            lines.append(f"{_rel(root, p)} ({_size(p.stat().st_size)})")
    if not lines:
        return "(empty folder)"
    more = len(lines) - MAX_LIST
    out = "\n".join(lines[:MAX_LIST])
    return out + (f"\n... and {more} more" if more > 0 else "")


def read_file(root: Path, path: str, start_line: int = 1, max_lines: int = DEFAULT_LINES) -> str:
    target = _resolve(root, path)
    if target.is_dir():
        raise RepoToolError(f"{path} is a folder; use list_files")
    if not _is_text(target):
        raise RepoToolError(f"{path} is not a text file")
    start = max(1, int(start_line or 1))
    count = min(max(1, int(max_lines or DEFAULT_LINES)), MAX_LINES)
    out: list[str] = []
    chars = 0
    total = 0
    last = start - 1
    with target.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            total = n
            if n < start or len(out) >= count or chars >= MAX_CHARS:
                continue
            line = line.rstrip("\n")
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " ...(line cut)"
            out.append(f"{n}: {line}")
            chars += len(line) + 8
            last = n
    if start > total:
        return f"{path} has {total} lines"
    header = f"{_rel(root, target)}, lines {start}-{last} of {total}"
    footer = f"\n(more: read_file with start_line={last + 1})" if last < total else ""
    return header + "\n" + "\n".join(out) + footer


def search_repo(root: Path, text: str, path: str = "") -> str:
    needle = (text or "").strip().lower()
    if not needle:
        raise RepoToolError("nothing to search for")
    target = _resolve(root, path)
    files = [target] if target.is_file() else _walk(target)
    matches: list[str] = []
    for p in files:
        try:
            if p.stat().st_size > MAX_SEARCH_BYTES or not _is_text(p):
                continue
            with p.open(encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if needle in line.lower():
                        shown = line.strip()[:300]
                        matches.append(f"{_rel(root, p)}:{n}: {shown}")
                        if len(matches) > MAX_MATCHES:
                            break
        except OSError:
            continue
        if len(matches) > MAX_MATCHES:
            break
    if not matches:
        return "no matches"
    out = "\n".join(matches[:MAX_MATCHES])
    if len(matches) > MAX_MATCHES:
        out += f"\n(more than {MAX_MATCHES} matches; narrow the search or the path)"
    return out[:MAX_CHARS]


def _walk(folder: Path) -> list[Path]:
    real = folder.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(real):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() and real not in p.resolve().parents:
                continue
            found.append(p)
    return found


def run_tool(root: Path, name: str, args: dict[str, Any]) -> str:
    """Run one repository tool; errors come back as text the model can read."""
    try:
        if name == "list_files":
            return list_files(root, str(args.get("path", "")))
        if name == "read_file":
            return read_file(
                root,
                str(args.get("path", "")),
                int(args.get("start_line") or 1),
                int(args.get("max_lines") or DEFAULT_LINES),
            )
        if name == "search_repo":
            return search_repo(root, str(args.get("text", "")), str(args.get("path", "")))
        return f"unknown tool {name}"
    except (RepoToolError, OSError, ValueError, TypeError) as e:
        return f"{name} failed: {e}"


def describe(name: str, args: dict[str, Any]) -> str:
    """One line for the activity feed, like research's "searched: ..."."""
    if name == "list_files":
        return f"listed: {args.get('path') or '.'}"
    if name == "read_file":
        start = int(args.get("start_line") or 1)
        return f"read file: {args.get('path', '')} (from line {start})"
    if name == "search_repo":
        where = f" in {args['path']}" if args.get("path") else ""
        return f"searched repo: {args.get('text', '')}{where}"
    return name
