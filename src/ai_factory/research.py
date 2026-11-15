"""Web research for the Project Planning Agent on local models.

Claude nodes use Anthropic's own web search and fetch tools (see llm.py). Local
OpenAI-compatible models get two equivalent function tools that ai-factory runs
itself, when a search service is configured (``AI_FACTORY_SEARCH_URL``: ai-search or
any SearXNG instance with JSON output enabled, e.g. ``http://127.0.0.1:8888``; several
comma-separated URLs are tried in order, and ``AI_FACTORY_SEARCH_TOKEN`` is sent as a
Bearer token when set):

- ``web_search(query)``: titles, links and snippets of the top results;
- ``web_fetch(url)``: the text of one public web page.

Both come from ``ai_common.web``. Fetching only reaches public addresses (every redirect hop
is checked too): localhost, private, link-local and other reserved networks are refused, so
research can't be used to look into your LAN.
"""

from __future__ import annotations

from typing import Any

from ai_common import web

from ai_factory.config import get_settings

MAX_RESULTS = 6
MAX_PAGE_CHARS = 12_000
MAX_ROUNDS = 8  # tool rounds per planner reply

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns the top results with title, link and "
            "snippet. Use it to research topics you don't know well.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Read the text of one public web page (e.g. a search result).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL"}},
                "required": ["url"],
            },
        },
    },
]


def available() -> bool:
    return bool(search_urls())


def search_urls() -> list[str]:
    return [u.strip() for u in get_settings().search_url.split(",") if u.strip()]


async def run_tool(name: str, args: dict[str, Any]) -> str:
    """Run one research tool; errors come back as text the model can read."""
    try:
        if name == "web_search":
            return await search(str(args.get("query", "")))
        if name == "web_fetch":
            return await fetch(str(args.get("url", "")))
        return f"unknown tool {name}"
    except (web.WebError, ValueError, OSError) as e:
        return f"{name} failed: {e}"


async def search(query: str) -> str:
    if not query.strip():
        raise ValueError("empty query")
    hits = await web.search(
        search_urls(), query, count=MAX_RESULTS, token=get_settings().search_token, timeout=20
    )
    if not hits:
        return "no results"
    return "\n\n".join(f"{i}. {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits, 1))


async def fetch(url: str) -> str:
    page = await web.fetch(
        url, timeout=20, page_chars=MAX_PAGE_CHARS + 1, user_agent="ai-factory-planner/1"
    )
    if page.error:
        raise ValueError(page.error)
    text = page.text
    return text[:MAX_PAGE_CHARS] + (" ..." if len(text) > MAX_PAGE_CHARS else "")
