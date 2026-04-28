#!/usr/bin/env python3
"""
ui_controller.py — SYPHER UI mutation skill.
Writes structured JSON events to ui_events.jsonl; the TypeScript bus
reads and applies them to the live Electron DOM in real-time.
"""

import json
import sys
import time
from pathlib import Path

# Resolved at runtime relative to workspace root (two levels up from skills/)
_DEFAULT_EVENTS_FILE = Path(__file__).parents[2] / ".sypher" / "ui_events.jsonl"


def _emit(event: dict, events_file: Path | None = None) -> dict:
    """Append a mutation event to the JSONL events file."""
    target = Path(events_file) if events_file else _DEFAULT_EVENTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {"ts": int(time.time() * 1000), **event}
    with target.open("a") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")

    return {"ok": True, "emitted": payload}


# ── Public mutation functions ─────────────────────────────────────────────────

def color_shift(color: str, events_file: str | None = None) -> dict:
    """Change the primary neon accent color of the SYPHER HUD."""
    _VALID_COLORS = {
        "neon_green": "#00ff88",
        "neon_red": "#ff2255",
        "neon_cyan": "#00eeff",
        "neon_amber": "#ffaa00",
        "neon_violet": "#cc44ff",
        "neon_white": "#e8ffe8",
    }
    hex_color = _VALID_COLORS.get(color, color if color.startswith("#") else "#00ff88")
    return _emit({"action": "color_shift", "color": hex_color}, events_file)


def maximize_terminal(events_file: str | None = None) -> dict:
    """Expand the terminal panel to fill most of the screen."""
    return _emit({"action": "maximize_terminal"}, events_file)


def restore_layout(events_file: str | None = None) -> dict:
    """Restore the default editor/terminal split layout."""
    return _emit({"action": "restore_layout"}, events_file)


def hide_explorer(events_file: str | None = None) -> dict:
    """Hide the file explorer sidebar to maximize code real estate."""
    return _emit({"action": "hide_explorer"}, events_file)


def show_explorer(events_file: str | None = None) -> dict:
    """Show the file explorer sidebar."""
    return _emit({"action": "show_explorer"}, events_file)


def focus_terminal(events_file: str | None = None) -> dict:
    """Move keyboard focus to the integrated terminal."""
    return _emit({"action": "focus_terminal"}, events_file)


def set_debug_layout(events_file: str | None = None) -> dict:
    """Switch to the high-intensity debug layout: red borders, large terminal."""
    r1 = color_shift("neon_red", events_file)
    r2 = maximize_terminal(events_file)
    r3 = hide_explorer(events_file)
    return {"ok": True, "steps": [r1, r2, r3], "layout": "debug"}


def set_code_layout(events_file: str | None = None) -> dict:
    """Switch to the standard coding layout: green borders, split view."""
    r1 = color_shift("neon_green", events_file)
    r2 = restore_layout(events_file)
    r3 = show_explorer(events_file)
    return {"ok": True, "steps": [r1, r2, r3], "layout": "code"}


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "mutate_ui",
            "description": (
                "Physically mutate the IDE layout and visual theme to match the current task. "
                "Use 'debug' layout when debugging; 'code' layout for normal coding; "
                "or set individual properties."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "color_shift", "maximize_terminal", "restore_layout",
                            "hide_explorer", "show_explorer", "focus_terminal",
                            "set_debug_layout", "set_code_layout",
                        ],
                        "description": "The UI mutation to perform.",
                    },
                    "color": {
                        "type": "string",
                        "description": (
                            "For color_shift: a hex color '#rrggbb' or a preset name "
                            "(neon_green, neon_red, neon_cyan, neon_amber, neon_violet)."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    }
]


def _dispatch(args: dict) -> dict:
    action = args.get("action", "")
    ef = args.get("events_file")
    dispatch = {
        "color_shift": lambda: color_shift(args.get("color", "neon_green"), ef),
        "maximize_terminal": lambda: maximize_terminal(ef),
        "restore_layout": lambda: restore_layout(ef),
        "hide_explorer": lambda: hide_explorer(ef),
        "show_explorer": lambda: show_explorer(ef),
        "focus_terminal": lambda: focus_terminal(ef),
        "set_debug_layout": lambda: set_debug_layout(ef),
        "set_code_layout": lambda: set_code_layout(ef),
    }
    fn = dispatch.get(action)
    if fn is None:
        return {"ok": False, "error": f"Unknown action: {action}"}
    return fn()


TOOL_FUNCTIONS = {"mutate_ui": _dispatch}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: ui_controller.py <action> [color]"}))
        sys.exit(1)
    args = {"action": sys.argv[1]}
    if len(sys.argv) > 2:
        args["color"] = sys.argv[2]
    print(json.dumps(_dispatch(args), indent=2))
