#!/usr/bin/env python3
"""
web_surfer.py — SYPHER internet bridge.
search_web: DuckDuckGo search → top N results as JSON.
read_url:   trafilatura → clean Markdown article text.

Dependencies (install into .sypher_env):
  pip install ddgs trafilatura
"""

import json
import sys
from urllib.parse import urlparse


def search_web(query: str, max_results: int = 5) -> dict:
    """
    Search the web with DuckDuckGo and return top results.
    Returns: {"ok": True, "results": [{"title", "url", "snippet"}, ...]}
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "ok": False,
            "error": "ddgs not installed. Run: pip install ddgs",
        }

    try:
        raw = DDGS().text(query, max_results=max_results)
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in (raw or [])
        ]
        return {"ok": True, "query": query, "count": len(results), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e), "query": query}


def read_url(url: str) -> dict:
    """
    Download a URL, strip boilerplate, and return clean Markdown text.
    Returns: {"ok": True, "url", "title", "text"}
    """
    # Validate URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": f"Only http/https URLs are supported, got: {url}"}

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
            return {"ok": False, "error": f"Failed to download URL: {url}"}

        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=False,
            include_images=False,
            no_fallback=False,
        )

        if not text:
            return {"ok": False, "error": "trafilatura could not extract meaningful content."}

        # Extract title heuristically from first markdown heading
        lines = text.splitlines()
        title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), url)

        return {
            "ok": True,
            "url": url,
            "title": title,
            "length": len(text),
            "text": text,
        }

    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the internet with DuckDuckGo. Use this when the user asks about "
                "a current event, a new library, an API you don't know, or anything "
                "outside your knowledge cutoff. Returns top URLs and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5).",
                        "default": 5,
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
            "description": (
                "Download a web page and extract its clean Markdown text — "
                "stripping ads, navbars, and boilerplate. Use after search_web "
                "to ingest the full content of a documentation page or article."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full https:// URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "search_web": lambda args: search_web(
        args["query"], max_results=int(args.get("max_results", 5))
    ),
    "read_url": lambda args: read_url(args["url"]),
}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: web_surfer.py search_web <query>  |  read_url <url>"}))
        sys.exit(1)
    fn, arg = sys.argv[1], sys.argv[2]
    result = TOOL_FUNCTIONS.get(fn, lambda a: {"error": f"Unknown: {fn}"})({
        "query": arg, "url": arg
    })
    print(json.dumps(result, indent=2))
