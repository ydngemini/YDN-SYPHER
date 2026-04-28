#!/usr/bin/env python3
"""router.py — SYPHER Semantic Intent Router.

Classifies user prompts and returns the best preset name.
Uses BAAI/bge-small-en-v1.5 (ONNX, quantized) via FastEmbed — pure CPU,
~50-200ms per query on a 2016 Intel Core i7. Model is stored locally so
no network call is needed after first install.

Install: pip install "semantic-router[fastembed]"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Local ONNX model — downloaded by `pip install fastembed` bootstrap script.
# Path: YDNIDE/.sypher/models/ (4 levels up from this file at vscode/.sypher/agents/)
_MODEL_CACHE = str(Path(__file__).parent.parent.parent.parent / ".sypher" / "models")

from semantic_router import Route
from semantic_router.routers import SemanticRouter
from semantic_router.encoders import FastEmbedEncoder

# ── 10 preset routes × 10 anchor utterances ──────────────────────────────────

_ROUTES: list[Route] = [
    Route(
        name="GPT_5_5_ULTRA",
        utterances=[
            "design the full system architecture for this project",
            "I need the highest quality reasoning for this problem",
            "this is a complex multi-step build failure I cannot debug",
            "architect a scalable kernel-level solution",
            "perform deep multi-file codebase analysis",
            "self-heal this broken build pipeline",
            "I need you at maximum intelligence for this",
            "analyze cross-language interop at the ABI level",
            "critical production bug needs immediate root cause analysis",
            "generate a complete system design document",
        ],
    ),
    Route(
        name="CODE_GOD",
        utterances=[
            "fix this broken function",
            "refactor this class to be cleaner",
            "write a unit test for this module",
            "there is a bug in this code",
            "implement this feature in TypeScript",
            "optimize this algorithm for speed",
            "debug why this is failing",
            "help me with this code",
            "make this code correct",
            "write a complete implementation of",
        ],
    ),
    Route(
        name="LOGIC_BEAST",
        utterances=[
            "analyze this problem step by step",
            "what are the tradeoffs between these approaches",
            "compare Redis and Kafka for this use case",
            "explain the system architecture",
            "design a distributed system for",
            "reason through the implications carefully",
            "what is the best design pattern here",
            "analyze time and space complexity",
            "break this down into components",
            "evaluate these three strategies",
        ],
    ),
    Route(
        name="THE_SURGEON",
        utterances=[
            "make this one small edit",
            "change this variable name to",
            "rename this function",
            "apply this exact patch",
            "quick fix on line 42",
            "swap this import for that one",
            "delete this line",
            "add a semicolon at the end",
            "move this block above that one",
            "one line change only",
        ],
    ),
    Route(
        name="LIGHTSPEED",
        utterances=[
            "quick answer please",
            "fast response needed",
            "simple yes or no",
            "what is the syntax for a for loop",
            "one word answer",
            "brief explanation of async await",
            "short answer only",
            "just tell me if this is correct",
            "is Python 3.12 stable",
            "quick question",
        ],
    ),
    Route(
        name="HARDWARE_EYES",
        utterances=[
            "look at this image",
            "analyze this diagram",
            "what do you see in this picture",
            "read this schematic for me",
            "examine this circuit board layout",
            "describe what is in this screenshot",
            "look at my PCB design",
            "what components are shown in this photo",
            "analyze this oscilloscope waveform",
            "read this datasheet diagram",
        ],
    ),
    Route(
        name="CREATIVE_FORGE",
        utterances=[
            "brainstorm ideas for this project",
            "design a creative architecture for my app",
            "imagine a new system that solves this",
            "generate creative concepts for the UI",
            "what if we built this differently",
            "invent a novel way to handle this",
            "think outside the box about concurrency",
            "what is an innovative approach here",
            "come up with ten ideas for this feature",
            "creative solution for this constraint",
        ],
    ),
    Route(
        name="MINT_SPECIALIST",
        utterances=[
            "format this data as JSON",
            "generate a structured report from this",
            "output this configuration as YAML",
            "create a Pydantic schema for this",
            "tabulate this dataset",
            "parse this text into a structured format",
            "generate a template with these fields",
            "create a TypeScript type from this JSON",
            "serialize this to a clean schema",
            "structured output for this API response",
        ],
    ),
    Route(
        name="WEB_STREAKER",
        utterances=[
            "search for information about semantic-router",
            "what is the latest release of FastEmbed",
            "look up the documentation for litellm",
            "research this library and summarize",
            "summarize this article for me",
            "find the current stable version of CUDA",
            "search the web for examples of",
            "look up recent news about AI hardware",
            "find real examples of this pattern online",
            "what is the current state of the art in",
        ],
    ),
    Route(
        name="KIMI_BRIDGE",
        utterances=[
            "read through this entire large codebase",
            "analyze this very long document end to end",
            "process this giant log file",
            "summarize this entire book for me",
            "full context analysis of all these files",
            "read all of these files and give me insights",
            "long context reasoning about this project",
            "analyze every file in this directory",
            "read this hundred page PDF document",
            "process the entire history of this repository",
        ],
    ),
    Route(
        name="THE_DRAFTSMAN",
        utterances=[
            "write boilerplate code for a new module",
            "generate a project template",
            "fill in this function stub with placeholder logic",
            "complete this class skeleton",
            "write docstrings for every function",
            "generate inline comments for this code",
            "scaffold a new FastAPI server module",
            "write a README for this project",
            "generate copyright headers for these files",
            "create placeholder implementations for this interface",
        ],
    ),
]

# ── Vision override ───────────────────────────────────────────────────────────

_VISION_RE = re.compile(
    r'\b(look\s+at\s+(this|my)|analyze\s+this\s+(image|photo|picture|diagram|schematic|circuit|screenshot|pcb|waveform)|'
    r'what\s+(do\s+you\s+see|is\s+shown|can\s+you\s+see)|read\s+this\s+(image|diagram|schematic|photo)|'
    r'attached\s+(image|photo|file|screenshot)|see\s+(attached|the\s+image)|<image>|<img)',
    re.IGNORECASE,
)

# ── Lazy singleton ────────────────────────────────────────────────────────────

_router: SemanticRouter | None = None


def _get_router() -> SemanticRouter:
    global _router
    if _router is None:
        encoder = FastEmbedEncoder(
            name="BAAI/bge-small-en-v1.5",
            score_threshold=0.5,
            cache_dir=_MODEL_CACHE,
        )
        _router = SemanticRouter(encoder=encoder, routes=_ROUTES, auto_sync="local")
    return _router


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class RouteResult:
    preset: str | None
    confidence: float
    forced: bool = False
    reason: str = field(default="")


def route_prompt(text: str, has_image: bool = False) -> RouteResult:
    """Classify the prompt and return the best preset.

    Returns RouteResult with .preset = None when no route clears the threshold.
    .forced = True means the override was regex/image-based, not embedding.
    """
    if has_image:
        return RouteResult(preset="HARDWARE_EYES", confidence=1.0, forced=True, reason="image_attached")

    if _VISION_RE.search(text):
        return RouteResult(preset="HARDWARE_EYES", confidence=1.0, forced=True, reason="vision_pattern")

    router = _get_router()
    result = router(text)
    if result.name is None:
        return RouteResult(preset=None, confidence=0.0, reason="below_threshold")

    return RouteResult(preset=result.name, confidence=0.85, reason="embedding_match")


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    samples = [
        ("design the full system architecture for this kernel module", False),
        ("fix this broken Python function", False),
        ("look at this circuit diagram", False),
        ("look at this", True),
        ("search for the latest semantic-router release", False),
        ("write boilerplate for a FastAPI server", False),
        ("analyze the tradeoffs between Redis and Kafka", False),
        ("quick yes or no: is Python 3.12 stable", False),
        ("read this entire 200 page specification document", False),
        ("format this as a JSON schema", False),
    ]

    for prompt, has_img in samples:
        r = route_prompt(prompt, has_image=has_img)
        print(json.dumps({
            "prompt": prompt[:60],
            "preset": r.preset,
            "forced": r.forced,
            "reason": r.reason,
        }))
