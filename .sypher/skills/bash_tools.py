#!/usr/bin/env python3
"""bash_tools.py — SYPHER shell hands. Execute Linux commands and return output."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# Commands that are never allowed, even with workspace trust
_BLOCKLIST = frozenset({
    "rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero", "mkfs",
    "shutdown", "reboot", "halt", "poweroff", "init 0", "init 6",
    "chmod -R 777 /", "chown -R", "mv /* ",
})

_MAX_OUTPUT = 32_768  # chars — truncate beyond this


def run_terminal(
    command: str,
    cwd: str | None = None,
    timeout: int = 30,
    env_extra: dict[str, str] | None = None,
) -> dict:
    """
    Execute a shell command and return stdout + stderr.
    Runs in a non-interactive bash shell.  Output is capped at 32 KB.
    """
    # Basic safety check
    for blocked in _BLOCKLIST:
        if blocked in command:
            return {
                "ok": False,
                "error": f"Command blocked by SYPHER safety policy: contains '{blocked}'",
            }

    work_dir = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    if not work_dir.exists():
        return {"ok": False, "error": f"Working directory not found: {cwd}"}

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        stdout = result.stdout
        stderr = result.stderr
        truncated = False

        if len(stdout) + len(stderr) > _MAX_OUTPUT:
            half = _MAX_OUTPUT // 2
            stdout = stdout[:half]
            stderr = stderr[:half]
            truncated = True

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "command": command,
            "cwd": str(work_dir),
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Command timed out after {timeout}s",
            "command": command,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "command": command}


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": (
                "Execute a Linux shell command and return stdout/stderr. "
                "Use for compiling code, running grep/find, git operations, "
                "checking logs, or any other shell task. Timeout: 30 seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to run."},
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional, defaults to workspace root).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 30, max 120).",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
    }
]

TOOL_FUNCTIONS = {
    "run_terminal": lambda args: run_terminal(
        args["command"],
        cwd=args.get("cwd"),
        timeout=min(int(args.get("timeout", 30)), 120),
    ),
}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: bash_tools.py <command> [cwd]"}))
        sys.exit(1)
    print(json.dumps(run_terminal(sys.argv[1], cwd=sys.argv[2] if len(sys.argv) > 2 else None),
                     indent=2))
