#!/usr/bin/env python3
"""planner.py — SYPHER Task Planner Pre-Processor.

Uses GPT-5.5 (via hf_matrix) to decompose a complex task into a JSON checklist.
Writes the plan to task_manifest.json and pipes each step to the UI event bus
so the HUD task list overlay updates in real time.

Usage:
  planner.py --task "Refactor the auth layer to use JWT" --workspace /path/to/ws
  planner.py --update-step 2 --status running --workspace /path/to/ws
  planner.py --update-step 2 --status done --output "Patched 3 files" --workspace /path/to/ws

Outputs (stdout): plan JSON on initial call, updated step JSON on update calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))


def _log(msg: str) -> None:
    print(f"[PLANNER] {msg}", file=sys.stderr, flush=True)


# ── Prompt ────────────────────────────────────────────────────────────────────

_PLAN_SYSTEM = """\
You are SYPHER Planner, an expert software project decomposer.
Break the given task into a precise, ordered execution checklist.
Respond with ONLY a valid JSON object — no markdown, no explanation.

Schema:
{
  "task": "<original task>",
  "complexity": "low" | "medium" | "high",
  "preset_recommendation": "<PRESET_NAME>",
  "estimated_iterations": <int 1-12>,
  "steps": [
    {"step": 1, "task": "<concise actionable description>", "status": "pending"},
    ...
  ]
}

Rules:
- Maximum 8 steps. Minimum 2.
- Steps must be concrete and testable (not "think about X").
- preset_recommendation must be one of: GPT_5_5_ULTRA, CODE_GOD, LOGIC_BEAST,
  THE_SURGEON, LIGHTSPEED, HARDWARE_EYES, CREATIVE_FORGE, MINT_SPECIALIST,
  WEB_STREAKER, KIMI_BRIDGE, THE_DRAFTSMAN.
- Output ONLY the JSON object. No surrounding text.
"""


def _plan_messages(task: str) -> list[dict]:
    return [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": f"Task: {task}"},
    ]


# ── UI event bus ──────────────────────────────────────────────────────────────

def _emit(workspace: Path, event: dict) -> None:
    events_path = workspace / ".sypher" / "ui_events.jsonl"
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        _log(f"Failed to write UI event: {e}")


def _save_manifest(workspace: Path, manifest: dict) -> None:
    out = workspace / ".sypher" / "task_manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(manifest, f, indent=2)


def _load_manifest(workspace: Path) -> dict:
    src = workspace / ".sypher" / "task_manifest.json"
    if not src.exists():
        return {}
    with src.open() as f:
        return json.load(f)


# ── Generate plan ─────────────────────────────────────────────────────────────

def generate_plan(task: str, workspace: Path, max_steps: int = 8) -> dict:
    """Call GPT-5.5 to decompose `task` into a JSON checklist."""
    from hf_matrix import complete, load_config  # type: ignore[import]

    config = load_config()
    messages = _plan_messages(task)

    _log(f"Calling GPT_5_5_ULTRA to plan: {task[:80]}...")
    _emit(workspace, {"action": "planner_active", "active": True})

    try:
        response = complete(
            messages=messages,
            config=config,
            force_preset_name="GPT_5_5_ULTRA",
            auto_route=False,
        )
    except Exception as e:
        _log(f"LLM call failed: {e}")
        _emit(workspace, {"action": "planner_active", "active": False})
        raise

    content = response.get("content") or ""
    _log(f"Raw plan response ({len(content)} chars)")

    # Extract JSON from response (strip any surrounding text)
    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON block in the response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            _emit(workspace, {"action": "planner_active", "active": False})
            raise ValueError(f"No JSON found in planner response: {content[:200]}")
        plan = json.loads(content[start:end])

    # Enforce schema defaults
    plan.setdefault("task", task)
    plan.setdefault("complexity", "medium")
    plan.setdefault("preset_recommendation", "CODE_GOD")
    plan.setdefault("estimated_iterations", 6)
    plan.setdefault("steps", [])

    # Cap steps
    plan["steps"] = plan["steps"][:max_steps]
    for i, step in enumerate(plan["steps"], 1):
        step["step"] = i
        step.setdefault("status", "pending")

    _save_manifest(workspace, plan)

    # Emit full manifest to HUD
    _emit(workspace, {
        "action": "task_manifest_update",
        "manifest": plan["steps"],
    })

    _log(f"Plan generated: {len(plan['steps'])} steps, "
         f"complexity={plan['complexity']}, preset={plan['preset_recommendation']}")
    return plan


# ── Update a step ─────────────────────────────────────────────────────────────

def update_step(
    workspace: Path,
    step: int,
    status: str,
    output: str | None = None,
) -> dict:
    """Update the status of a single step in the manifest and emit a UI event."""
    manifest = _load_manifest(workspace)
    steps = manifest.get("steps", [])

    updated: dict = {}
    for s in steps:
        if s.get("step") == step:
            s["status"] = status
            if output:
                s["output"] = output
            updated = s
            break

    if updated:
        _save_manifest(workspace, manifest)
        _emit(workspace, {
            "action": "task_update",
            "step": step,
            "status": status,
            "output": output or "",
        })
        # When last step completes, deactivate planner glow
        if status in ("done", "failed"):
            all_done = all(
                s.get("status") in ("done", "failed")
                for s in steps
            )
            if all_done:
                _emit(workspace, {"action": "planner_active", "active": False})

    return updated


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="SYPHER Task Planner")
    ap.add_argument("--task", default=None, help="Task description to decompose")
    ap.add_argument("--workspace", default=str(Path.cwd()), help="Workspace root")
    ap.add_argument("--max-steps", type=int, default=8)

    # Update-mode args
    ap.add_argument("--update-step", type=int, default=None,
                    help="Step number to update (update mode)")
    ap.add_argument("--status", default=None,
                    choices=["pending", "running", "done", "failed"],
                    help="New status for --update-step")
    ap.add_argument("--output", default=None, help="Optional output text for the step")

    args = ap.parse_args()
    workspace = Path(args.workspace)

    if args.update_step is not None:
        if not args.status:
            ap.error("--status is required with --update-step")
        result = update_step(workspace, args.update_step, args.status, args.output)
        print(json.dumps(result, separators=(",", ":")))
        return

    if not args.task:
        ap.error("--task is required when not in update mode")

    try:
        plan = generate_plan(args.task, workspace, args.max_steps)
        print(json.dumps(plan, separators=(",", ":"), ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
