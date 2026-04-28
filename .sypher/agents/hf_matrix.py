#!/usr/bin/env python3
"""hf_matrix.py — SYPHER dual-provider LLM gateway with semantic auto-routing.

Supports:
  • OpenAI  (GPT_5_5_ULTRA, api_key_env=OPENAI_API_KEY, no api_base needed)
  • HuggingFace Inference API (all other presets, api_key_env=HF_TOKEN)
  • Any custom OpenAI-compatible endpoint via api_base

Auto-routing: on every complete() call, passes the last user message through
router.py. If the router finds a better preset, that preset is used for THIS
request only. The on-disk config is never modified by auto-routing.

Install: pip install litellm "semantic-router[fastembed]"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import litellm

# ── Silence litellm debug noise ───────────────────────────────────────────────
litellm.set_verbose = False

# ── Config / env resolution ───────────────────────────────────────────────────

_HERE = Path(__file__).parent                          # vscode/.sypher/agents/
_YDNIDE = _HERE.parent.parent.parent                   # YDNIDE/

_CONFIG_SEARCH = [
    _HERE.parent / "config.json",                      # vscode/.sypher/config.json
    _YDNIDE / ".sypher" / "config.json",               # YDNIDE/.sypher/config.json
    Path.home() / ".sypher" / "config.json",
]

_ENV_SEARCH = [
    _YDNIDE / ".env",                                  # YDNIDE/.env  ← primary
    _HERE.parent.parent / ".env",                      # vscode/.env
    Path.home() / ".env",
]


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())


def load_config() -> dict[str, Any]:
    for ep in _ENV_SEARCH:
        _load_env(ep)
    for cp in _CONFIG_SEARCH:
        if cp.exists():
            with cp.open() as f:
                return json.load(f)
    raise FileNotFoundError(
        f"hf_matrix: config.json not found. Searched: {[str(p) for p in _CONFIG_SEARCH]}"
    )


def _find_config_path() -> Path:
    for p in _CONFIG_SEARCH:
        if p.exists():
            return p
    raise FileNotFoundError("config.json not found")


def get_preset(config: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    if name is None:
        name = config.get("active_preset", "CODE_GOD")
    presets = config.get("presets", {})
    if name not in presets:
        raise KeyError(f"Preset '{name}' not in config. Available: {list(presets)}")
    p = dict(presets[name])
    p["name"] = name
    return p


def cycle_preset(config_path: Path | None = None) -> str:
    if config_path is None:
        config_path = _find_config_path()
    with config_path.open() as f:
        cfg = json.load(f)
    order = cfg.get("preset_order", list(cfg.get("presets", {}).keys()))
    current = cfg.get("active_preset", order[0])
    idx = order.index(current) if current in order else 0
    next_name = order[(idx + 1) % len(order)]
    cfg["active_preset"] = next_name
    with config_path.open("w") as f:
        json.dump(cfg, f, indent=2)
    return next_name


# ── UI event bus ──────────────────────────────────────────────────────────────

def _write_ui_event(event: dict[str, Any]) -> None:
    events_path = Path.cwd() / ".sypher" / "ui_events.jsonl"
    try:
        with events_path.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


# ── Prompt extraction for routing ─────────────────────────────────────────────

def _extract_routing_text(messages: list[dict[str, Any]]) -> tuple[str, bool]:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content, False
        if isinstance(content, list):
            texts: list[str] = []
            has_image = False
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") in ("image_url", "image"):
                    has_image = True
            return " ".join(texts), has_image
    return "", False


# ── Semantic auto-routing ─────────────────────────────────────────────────────

def _auto_route(
    messages: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an override preset for this request, or None to use active_preset."""
    try:
        from router import route_prompt  # type: ignore[import]
    except ImportError:
        return None

    text, has_image = _extract_routing_text(messages)
    if not text and not has_image:
        return None

    try:
        result = route_prompt(text, has_image=has_image)
    except Exception:
        return None

    if result.preset is None:
        return None

    current_name = config.get("active_preset", "")
    presets = config.get("presets", {})

    if result.preset == current_name or result.preset not in presets:
        return None

    preset = dict(presets[result.preset])
    preset["name"] = result.preset

    tier = preset.get("tier", "fast")
    mode = "god_mode" if tier == "god" else "fast_mode"

    _write_ui_event({
        "action": "chassis_notification",
        "preset": result.preset,
        "mode": mode,
        "forced": result.forced,
        "reason": result.reason,
    })

    return preset


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _build_litellm_kwargs(
    preset: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str,
) -> dict[str, Any]:
    api_key_env = preset.get("api_key_env", "HF_TOKEN")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"hf_matrix: env var '{api_key_env}' not set "
            f"(preset '{preset['name']}'). Source your .env or export {api_key_env}=..."
        )

    kwargs: dict[str, Any] = {
        "model": preset["model"],
        "messages": messages,
        "max_tokens": preset.get("max_tokens", 2048),
        "temperature": preset.get("temperature", 0.3),
        "api_key": api_key,
    }

    # HuggingFace and custom endpoints need explicit api_base; OpenAI does not.
    if "api_base" in preset:
        kwargs["api_base"] = preset["api_base"]

    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    return kwargs


# ── Public completion API ─────────────────────────────────────────────────────

def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    config: dict[str, Any] | None = None,
    preset: dict[str, Any] | None = None,
    force_preset_name: str | None = None,
    auto_route: bool = True,
) -> dict[str, Any]:
    """Execute a completion through the active (or auto-selected) preset.

    Args:
        messages: OpenAI-format message list.
        tools: Optional tool definitions.
        tool_choice: "auto", "none", or specific tool name.
        config: Pre-loaded config; loaded from disk if None.
        preset: Explicit preset dict; bypasses routing entirely.
        force_preset_name: Force a named preset by name (used by self-heal).
        auto_route: Semantically route the prompt when True.

    Returns:
        {content, tool_calls: [{id, name, arguments}], model, preset, tier, usage}
    """
    if config is None:
        config = load_config()

    # Priority: explicit preset dict > force_preset_name > auto_route > active_preset
    if preset is None:
        if force_preset_name is not None:
            preset = get_preset(config, force_preset_name)
            tier = preset.get("tier", "fast")
            _write_ui_event({
                "action": "chassis_notification",
                "preset": force_preset_name,
                "mode": "god_mode" if tier == "god" else "fast_mode",
                "forced": True,
                "reason": "self_heal",
            })
        elif auto_route:
            preset = _auto_route(messages, config)

    if preset is None:
        preset = get_preset(config)

    kwargs = _build_litellm_kwargs(preset, messages, tools, tool_choice)

    try:
        response = litellm.completion(**kwargs)
    except Exception as e:
        return {"error": str(e), "preset": preset.get("name", "unknown")}

    msg = response.choices[0].message

    tool_calls: list[dict[str, Any]] = []
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        for tc in msg.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})

    return {
        "content": msg.content,
        "tool_calls": tool_calls,
        "model": preset["model"],
        "preset": preset["name"],
        "tier": preset.get("tier", "fast"),
        "usage": dict(response.usage) if response.usage else {},
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        cfg = load_config()
        p = get_preset(cfg)
        print(json.dumps({
            "active_preset": p["name"],
            "model": p["model"],
            "tier": p.get("tier", "fast"),
            "description": p.get("description", ""),
        }, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
