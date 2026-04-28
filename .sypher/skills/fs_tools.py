#!/usr/bin/env python3
"""fs_tools.py — SYPHER filesystem hands. Read, list, and diff-apply files."""

import json
import os
from pathlib import Path


def read_file(path: str) -> dict:
    """Read a file and return its content with metadata."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"ok": False, "error": f"File not found: {path}"}
    if not p.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}
    try:
        text = p.read_text(errors="replace")
        return {
            "ok": True,
            "path": str(p),
            "size": p.stat().st_size,
            "lines": text.count("\n") + 1,
            "content": text,
        }
    except PermissionError as e:
        return {"ok": False, "error": str(e)}


def list_directory(path: str) -> dict:
    """List directory contents with type and size info."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"ok": False, "error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {path}"}
    try:
        entries = []
        for child in sorted(p.iterdir()):
            stat = child.stat()
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
            })
        return {"ok": True, "path": str(p), "entries": entries, "count": len(entries)}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}


def fast_apply_diff(path: str, search_block: str, replace_block: str) -> dict:
    """
    Find search_block in file and replace it with replace_block.
    Exact match only — no fuzzy matching. Fails loudly if the block
    is missing or appears more than once (ambiguous edit).
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"ok": False, "error": f"File not found: {path}"}

    original = p.read_text(errors="replace")
    count = original.count(search_block)

    if count == 0:
        return {
            "ok": False,
            "error": "search_block not found in file. Check for whitespace or line-ending differences.",
        }
    if count > 1:
        return {
            "ok": False,
            "error": f"search_block appears {count} times — cannot apply diff unambiguously.",
        }

    updated = original.replace(search_block, replace_block, 1)
    p.write_text(updated)

    return {
        "ok": True,
        "path": str(p),
        "lines_before": original.count("\n") + 1,
        "lines_after": updated.count("\n") + 1,
    }


# ── Tool definitions (OpenAI function-calling schema) ─────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text content of a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or workspace-relative file path."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fast_apply_diff",
            "description": (
                "Edit a file by replacing an exact block of text. "
                "search_block must match the file exactly (including indentation). "
                "Use read_file first to get the current content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit."},
                    "search_block": {"type": "string", "description": "Exact text to find."},
                    "replace_block": {"type": "string", "description": "Text to substitute in."},
                },
                "required": ["path", "search_block", "replace_block"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "read_file": lambda args: read_file(args["path"]),
    "list_directory": lambda args: list_directory(args["path"]),
    "fast_apply_diff": lambda args: fast_apply_diff(
        args["path"], args["search_block"], args["replace_block"]
    ),
}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: fs_tools.py read_file <path>"}))
        sys.exit(1)
    fn = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else "."
    result = TOOL_FUNCTIONS.get(fn, lambda _: {"error": f"Unknown function: {fn}"})({
        "path": arg,
        "search_block": sys.argv[3] if len(sys.argv) > 3 else "",
        "replace_block": sys.argv[4] if len(sys.argv) > 4 else "",
    })
    print(json.dumps(result, indent=2))
