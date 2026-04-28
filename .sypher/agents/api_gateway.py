#!/usr/bin/env python3
"""
api_gateway.py — SYPHER Universal API Gateway.
Routes prompts to any LiteLLM-supported endpoint (Hugging Face, Kimi, Qwen,
local Ollama, or any OpenAI-compatible API) based on .sypher/config.json.
Normalizes tool-calling responses across all providers.

Dependencies: pip install litellm
"""

import json
import os
import sys
from pathlib import Path
from typing import Any


# ── Config loader ─────────────────────────────────────────────────────────────

_CONFIG_SEARCH = [
    Path(__file__).parents[2] / ".sypher" / "config.json",   # workspace root
    Path.home() / ".sypher" / "config.json",
]


def _load_config() -> dict:
    for p in _CONFIG_SEARCH:
        if p.exists():
            return json.loads(p.read_text())
    return {}


# ── Provider resolution ───────────────────────────────────────────────────────

def _resolve(model_override: str | None, config: dict) -> tuple[str, str | None, str | None]:
    """
    Return (litellm_model_string, api_base, api_key).
    Accepts: "huggingface", "kimi", "qwen", "custom", or a full litellm model string.
    """
    target = model_override or config.get("default_model", "gpt-4o-mini")
    providers: dict = config.get("providers", {})

    # Named provider shorthand
    if target in providers:
        prov = providers[target]
        model = prov.get("default_model", target)
        api_base = prov.get("api_base")
        key_env = prov.get("api_key_env", "")
        api_key = os.environ.get(key_env, "") if key_env else None
        return model, api_base, api_key

    # Full litellm model string (e.g. "huggingface/Qwen/Qwen2.5-Coder-32B-Instruct")
    prefix = target.split("/")[0] if "/" in target else None
    if prefix and prefix in providers:
        prov = providers[prefix]
        api_base = prov.get("api_base")
        key_env = prov.get("api_key_env", "")
        api_key = os.environ.get(key_env, "") if key_env else None
        return target, api_base, api_key

    return target, None, None


# ── Core completion ───────────────────────────────────────────────────────────

def complete(
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str | None = None,
    config: dict | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict:
    """
    Send messages to the resolved model and return a normalized response dict:
    {
      "content":    str | None,          # assistant text if no tool call
      "tool_calls": list[ToolCall] | [], # populated when model wants a tool
      "model":      str,
      "usage":      {...},
    }
    """
    try:
        import litellm
    except ImportError:
        return {"error": "litellm not installed. Run: pip install litellm"}

    cfg = config or _load_config()
    litellm_model, api_base, api_key = _resolve(model, cfg)

    kwargs: dict[str, Any] = dict(
        model=litellm_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    try:
        response = litellm.completion(**kwargs)
    except Exception as e:
        return {"error": str(e), "model": litellm_model}

    msg = response.choices[0].message

    # Normalize tool_calls across providers
    normalized_calls = []
    raw_calls = getattr(msg, "tool_calls", None) or []
    for tc in raw_calls:
        normalized_calls.append({
            "id": tc.id,
            "name": tc.function.name,
            "arguments": _safe_parse(tc.function.arguments),
        })

    return {
        "content": msg.content if not normalized_calls else None,
        "tool_calls": normalized_calls,
        "model": litellm_model,
        "usage": dict(response.usage) if response.usage else {},
        "finish_reason": response.choices[0].finish_reason,
    }


def _safe_parse(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": s}


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = _load_config()
    print(f"[api_gateway] Config loaded. Default model: {cfg.get('default_model')}")
    print(f"[api_gateway] Providers: {list(cfg.get('providers', {}).keys())}")

    # Quick ping with no tools
    if "--ping" in sys.argv:
        model_arg = next((sys.argv[i + 1] for i, a in enumerate(sys.argv)
                          if a == "--model" and i + 1 < len(sys.argv)), None)
        result = complete(
            messages=[{"role": "user", "content": "Reply with just the word ONLINE."}],
            model=model_arg,
            config=cfg,
        )
        print(json.dumps(result, indent=2))
