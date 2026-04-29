#!/usr/bin/env python3
"""
web_navigator.py — autonomous web research bridge for SYPHER.

Dependencies (install in .sypher_env):
  pip install duckduckgo-search trafilatura

Capabilities:
  - search_web: query DuckDuckGo via duckduckgo-search
  - read_url: extract clean Markdown from URL via trafilatura
  - research_web: search -> read top 3 -> synthesize actionable solution
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _write_ui_event(event: dict[str, Any]) -> None:
    events_path = Path.cwd() / ".sypher" / "ui_events.jsonl"
    try:
        with events_path.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        # UI event bus is best-effort only.
        pass


def search_web(query: str, max_results: int = 8) -> dict[str, Any]:
    """Search DuckDuckGo with duckduckgo-search and return normalized results."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return {
            "ok": False,
            "error": "duckduckgo-search not installed. Run: pip install duckduckgo-search",
        }

    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results) or [])
    except Exception as e:
        return {"ok": False, "error": str(e), "query": query}

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
        }
        for r in raw
        if isinstance(r, dict) and r.get("href")
    ]

    return {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
    }


def read_url(url: str, max_chars: int = 14_000) -> dict[str, Any]:
    """Read URL and extract clean markdown with trafilatura."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": f"Only http/https URLs supported, got: {url}"}

    try:
        import trafilatura
    except ImportError:
        return {
            "ok": False,
            "error": "trafilatura not installed. Run: pip install trafilatura",
        }

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"ok": False, "error": f"Failed to download URL: {url}", "url": url}

        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=False,
            include_images=False,
            no_fallback=False,
        )

        if not text:
            return {
                "ok": False,
                "error": "trafilatura could not extract meaningful content",
                "url": url,
            }

        if len(text) > max_chars:
            text = text[:max_chars]

        title = _extract_title(text, fallback=url)
        return {
            "ok": True,
            "url": url,
            "title": title,
            "length": len(text),
            "text": text,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}


def _extract_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("# ").strip() or fallback
    return fallback


def _clean_excerpt(markdown: str, max_chars: int = 450) -> str:
    text = re.sub(r"[`*_>#-]", " ", markdown)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _synthesize(query: str, docs: list[dict[str, Any]]) -> str:
    if not docs:
        return (
            "No reliable pages were extracted for this query. "
            "Refine the error text and retry research_web."
        )

    lines: list[str] = [
        f"Query: {query}",
        "",
        "Synthesis:",
    ]

    for i, d in enumerate(docs, start=1):
        title = d.get("title") or d.get("url") or f"Source {i}"
        excerpt = _clean_excerpt(d.get("text", ""))
        lines.append(f"{i}. {title}: {excerpt}")

    lines.extend([
        "",
        "Actionable plan:",
        "- Compare overlapping guidance across all 3 sources and prioritize official docs.",
        "- Apply the minimal code/config change that addresses the shared root cause.",
        "- Re-run the failing command to verify resolution.",
    ])

    return "\n".join(lines)


def research_web(query: str, top_k: int = 3, search_results: int = 8) -> dict[str, Any]:
    """Agentic web pass: search, read top-k pages, synthesize result."""
    _write_ui_event({
        "action": "web_research_state",
        "active": True,
        "message": "WEAVER IS SEARCHING THE WEB...",
    })

    try:
        search = search_web(query, max_results=search_results)
        if not search.get("ok"):
            return {
                "ok": False,
                "query": query,
                "error": search.get("error", "search_web failed"),
            }

        candidates = search.get("results", [])[: max(1, int(top_k))]
        docs: list[dict[str, Any]] = []
        for item in candidates:
            url = item.get("url", "")
            if not url:
                continue
            extracted = read_url(url)
            if extracted.get("ok"):
                docs.append(extracted)

        synthesis = _synthesize(query, docs)
        return {
            "ok": True,
            "query": query,
            "searched": len(search.get("results", [])),
            "read": len(docs),
            "sources": [
                {
                    "title": d.get("title", ""),
                    "url": d.get("url", ""),
                    "length": d.get("length", 0),
                }
                for d in docs
            ],
            "documents": docs,
            "synthesis": synthesis,
        }
    finally:
        _write_ui_event({
            "action": "web_research_state",
            "active": False,
            "message": "WEAVER IS SEARCHING THE WEB...",
        })


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search DuckDuckGo using duckduckgo-search and return top results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "How many results to fetch (default 8).",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Read a URL and extract clean markdown using trafilatura.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_web",
            "description": (
                "Run full web research: search query, read top 3 pages, and synthesize an "
                "actionable solution from fresh documentation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Question or error to research."},
                    "top_k": {
                        "type": "integer",
                        "description": "How many top results to read (default 3).",
                        "default": 3,
                    },
                    "search_results": {
                        "type": "integer",
                        "description": "How many search hits to gather before selection (default 8).",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


TOOL_FUNCTIONS = {
    "search_web": lambda args: search_web(
        args["query"],
        max_results=int(args.get("max_results", 8)),
    ),
    "read_url": lambda args: read_url(args["url"]),
    "research_web": lambda args: research_web(
        args["query"],
        top_k=int(args.get("top_k", 3)),
        search_results=int(args.get("search_results", 8)),
    ),
}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": (
                "Usage: web_navigator.py <search_web|read_url|research_web> "
                "<query_or_url>"
            )
        }))
        sys.exit(1)

    fn = sys.argv[1]
    arg = sys.argv[2]
    handler = TOOL_FUNCTIONS.get(fn)
    if handler is None:
        print(json.dumps({"error": f"Unknown function: {fn}"}))
        sys.exit(1)

    result = handler({"query": arg, "url": arg})
    print(json.dumps(result, ensure_ascii=False, indent=2))

