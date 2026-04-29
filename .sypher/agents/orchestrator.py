#!/usr/bin/env python3
"""orchestrator.py — SYPHER ReAct loop with autonomous web-backed self-heal.

Receives a user prompt, runs a Reasoning + Acting loop, and autonomously
calls filesystem, shell, web, and UI tools until the task is done.

Self-healing: when run_terminal fails, the orchestrator escalates to
GPT_5_5_ULTRA and automatically runs web research on the error signal so the
repair turn is grounded in fresh documentation without user input.

Usage:
  .sypher_env/bin/python .sypher/agents/orchestrator.py \\
      --prompt "Refactor main.cpp to use smart pointers" \\
      --workspace /path/to/project \\
      [--force-preset GPT_5_5_ULTRA]

Output (stdout): single JSON line when done
  {"status": "ok", "response": "...", "iterations": N, "tools_called": [...]}
Progress (stderr): human-readable ReAct trace
"""

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_SKILLS = _HERE.parent / "skills"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_SKILLS))


def _log(msg: str) -> None:
    print(f"[SYPHER] {msg}", file=sys.stderr, flush=True)


# ── Build-error detection ─────────────────────────────────────────────────────

_BUILD_ERROR_SIGNALS = (
    "error:",
    "error TS",
    "traceback (most recent call last)",
    "syntaxerror",
    "typeerror",
    "importerror",
    "make: ***",
    "cmake error",
    "ninja: build stopped",
    "failed with exit code",
    "compilation failed",
    "linker error",
    "undefined reference",
    "cannot find",
    "no such file or directory",
)


def _is_build_error(result: dict) -> bool:
    if result.get("exit_code", result.get("returncode", 0)) != 0:
        return True
    combined = (
        result.get("stderr", "") + result.get("stdout", "")
    ).lower()
    return any(sig in combined for sig in _BUILD_ERROR_SIGNALS)


def _extract_error_query(result: dict) -> str:
    stderr = str(result.get("stderr", "") or "").strip()
    stdout = str(result.get("stdout", "") or "").strip()
    cmd = str(result.get("command", "") or "").strip()

    pool = stderr or stdout
    if not pool:
        return (f"bash command failed: {cmd}").strip()

    # Prefer lines that look like explicit failures.
    candidates = [
        line.strip()
        for line in pool.splitlines()
        if line.strip() and any(tok in line.lower() for tok in ("error", "failed", "exception", "traceback", "no such"))
    ]
    core = " | ".join(candidates[:3]) if candidates else pool.splitlines()[-1].strip()

    if cmd:
        return f"{core} (while running: {cmd})"
    return core


# ── Tool registry ─────────────────────────────────────────────────────────────

def _build_registry(workspace: str) -> tuple[list[dict], dict]:
    from fs_tools import TOOL_DEFINITIONS as FS_DEFS, TOOL_FUNCTIONS as FS_FNS
    from bash_tools import TOOL_DEFINITIONS as BASH_DEFS, TOOL_FUNCTIONS as BASH_FNS
    from ui_controller import TOOL_DEFINITIONS as UI_DEFS, TOOL_FUNCTIONS as UI_FNS

    all_defs = FS_DEFS + BASH_DEFS + UI_DEFS
    dispatch: dict = {**FS_FNS, **BASH_FNS, **UI_FNS}

    orig_bash = dispatch.get("run_terminal")
    if orig_bash:
        def _bash_with_cwd(args: dict) -> dict:
            args.setdefault("cwd", workspace)
            return orig_bash(args)
        dispatch["run_terminal"] = _bash_with_cwd

    try:
        from web_navigator import TOOL_DEFINITIONS as WEB_DEFS, TOOL_FUNCTIONS as WEB_FNS
        all_defs += WEB_DEFS
        dispatch.update(WEB_FNS)
        _log("Web navigator loaded")
    except ImportError:
        _log("Web navigator skipped (pip install duckduckgo-search trafilatura)")

    return all_defs, dispatch


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """\
You are SYPHER, an autonomous coding agent embedded in YDN SYPHER IDE.
You have direct access to the user's filesystem, terminal, and web browser.
You are a HYBRID intelligence: HuggingFace specialist models for fast code tasks,
GPT-5.5 for deep architecture and self-healing builds.

Rules:
1. ALWAYS use tools to gather information before writing code.
2. Use read_file before editing ANY file — never guess current content.
3. Use fast_apply_diff for targeted edits; read_file first for context.
4. After editing, use run_terminal to compile/test and verify correctness.
5. Use research_web for deep documentation lookup.
6. Use mutate_ui to adapt the IDE layout to the task.
7. When done, issue a final natural-language summary — NO more tool calls.
8. When given a build error to fix: read the error carefully, identify the root
   cause, apply the minimal correct fix, then verify with run_terminal.
9. If a bash/build error is unclear, you MUST use research_web on the exact
   error message and base your fix on the fresh documentation retrieved.
"""

_SELF_HEAL_INJECTION = """\

⚠️  SELF-HEAL MODE ACTIVE — Build failure detected.
You are now running on GPT-5.5 (god-tier reasoning).
Policy:
1) Use the injected web research context to ground your diagnosis.
2) If uncertainty remains, call research_web yourself with the exact error string.
3) Identify root cause, apply the minimal fix via fast_apply_diff, and re-run
   run_terminal to verify.
4) Do not ask the user for permission. Fix autonomously.
"""


# ── ReAct loop ────────────────────────────────────────────────────────────────

def run(
    prompt: str,
    workspace: str,
    force_preset: str | None,
    max_iterations: int,
    config: dict,
) -> dict:
    from hf_matrix import complete

    tool_defs, dispatch = _build_registry(workspace)
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]

    tools_called: list[str] = []
    iterations = 0
    escalate_next = False          # True when previous tool call was a build error
    self_heal_count = 0
    pending_error_query: str | None = None

    _log(f"Starting ReAct loop. Force preset: {force_preset or 'auto'} | "
         f"Max iterations: {max_iterations} | Tools: {list(dispatch.keys())}")

    while iterations < max_iterations:
        iterations += 1
        _log(f"── Iteration {iterations}/{max_iterations}"
             + (" [SELF-HEAL]" if escalate_next else "") + " ──")

        # Determine which preset to use this turn
        effective_force: str | None = force_preset
        if escalate_next:
            effective_force = "GPT_5_5_ULTRA"

            # Autonomous documentation grounding before the repair turn.
            if pending_error_query and "research_web" in dispatch:
                _log(f"SELF-HEAL web research: {pending_error_query[:160]}")
                try:
                    web_result = dispatch["research_web"]({
                        "query": pending_error_query,
                        "top_k": 3,
                        "search_results": 8,
                    })
                except Exception as e:
                    web_result = {"ok": False, "error": f"research_web raised: {e}"}

                tools_called.append("research_web(auto)")
                messages.append({
                    "role": "system",
                    "content": (
                        "Fresh web research for the failing command is available below. "
                        "Use it to fix the build with current docs:\n"
                        + json.dumps(web_result, ensure_ascii=False)[:20_000]
                    ),
                })

            # Inject self-heal context as a system message so GPT-5.5 knows its role
            messages.append({
                "role": "system",
                "content": _SELF_HEAL_INJECTION,
            })
            escalate_next = False
            self_heal_count += 1
            _log(f"SELF-HEAL #{self_heal_count}: escalating to GPT_5_5_ULTRA")

        response = complete(
            messages=messages,
            tools=tool_defs,
            config=config,
            force_preset_name=effective_force,
            auto_route=(effective_force is None),
        )

        if "error" in response:
            return {
                "status": "error",
                "message": response["error"],
                "iterations": iterations,
                "tools_called": tools_called,
            }

        # ── No tool call → final answer ────────────────────────────────────
        if not response["tool_calls"]:
            final_text = response.get("content") or ""
            _log(f"Final answer ({len(final_text)} chars) via {response.get('preset')}")
            return {
                "status": "ok",
                "response": final_text,
                "iterations": iterations,
                "self_heals": self_heal_count,
                "tools_called": tools_called,
                "model": response.get("model"),
                "preset": response.get("preset"),
                "usage": response.get("usage", {}),
            }

        # ── Tool calls → execute and feed back ─────────────────────────────
        messages.append({
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in response["tool_calls"]
            ],
        })

        for tc in response["tool_calls"]:
            name = tc["name"]
            args = tc["arguments"]
            _log(f"Tool: {name}({json.dumps(args, separators=(',', ':'))[:120]})")

            fn = dispatch.get(name)
            if fn is None:
                result: dict = {"ok": False, "error": f"Unknown tool: {name}"}
            else:
                try:
                    result = fn(args)
                except Exception as e:
                    result = {"ok": False, "error": f"Tool raised: {e}"}

            tools_called.append(name)
            _log(f"  → {json.dumps(result, separators=(',', ':'))[:200]}")

            # Self-heal trigger: build failure from terminal
            if name == "run_terminal" and _is_build_error(result):
                escalate_next = True
                pending_error_query = _extract_error_query(result)
                _log("BUILD ERROR detected — next turn escalates to GPT_5_5_ULTRA")
            elif name == "run_terminal":
                pending_error_query = None

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": json.dumps(result, separators=(",", ":")),
            })

    return {
        "status": "max_iterations_reached",
        "response": "SYPHER hit the iteration limit.",
        "iterations": iterations,
        "self_heals": self_heal_count,
        "tools_called": tools_called,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="SYPHER ReAct Agent Orchestrator")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--workspace", default=str(Path.cwd()))
    ap.add_argument("--force-preset", default=None,
                    help="Force a named preset for every turn (e.g. GPT_5_5_ULTRA)")
    ap.add_argument("--max-iterations", type=int, default=None)
    args = ap.parse_args()

    # Load config — search workspace first, then YDNIDE parent
    config: dict = {}
    for candidate in [
        Path(args.workspace) / ".sypher" / "config.json",
        Path(args.workspace).parent / ".sypher" / "config.json",
    ]:
        if candidate.exists():
            config = json.loads(candidate.read_text())
            break

    max_iter = args.max_iterations or config.get("max_iterations", 12)

    result = run(
        prompt=args.prompt,
        workspace=args.workspace,
        force_preset=args.force_preset,
        max_iterations=max_iter,
        config=config,
    )

    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
