"""Sertchi as an MCP server -- ``web``/``news`` search tools over stdio.

Tool names are deliberately bare: every MCP client prefixes them with the
server name it was configured under, so registering this as ``sertchi``
yields ``sertchi_web``/``sertchi_news`` in OpenCode and
``mcp__sertchi__web``/``mcp__sertchi__news`` in Claude Code.

WHY THIS MODULE EXISTS
-----------------------
Sertchi is a library (no server, no daemon), but several consumers on this
machine are LLM agents whose only extension point is MCP: OpenCode 2.x
dropped the ``tool/*.ts`` loader the old ``sertchi.ts`` bridge relied on, and
Claude Code speaks MCP natively. Rather than each consumer growing its own
bridge, this module is the one shared MCP front door. It is still not a
daemon: an MCP client spawns it per session, talks JSON-RPC over its
stdin/stdout, and it exits when stdin closes.

I1 -- STDLIB ONLY, NO ``mcp`` SDK
---------------------------------
MCP's stdio transport is newline-delimited JSON-RPC 2.0, and a tools-only
server needs five methods (``initialize``, ``notifications/initialized``,
``ping``, ``tools/list``, ``tools/call``). That is a few dozen lines of
``json`` -- not worth breaking I1 for the official SDK and its pydantic tree.

SEAM
----
This is a *consumer* of ``sertchi.sync``, sitting on the public API exactly
as ``etc/SERTCHI-INTEGRATION-GUIDE.md`` describes: it never touches an
adapter, the ledger, or the fallback order. ``Server(search=...)`` is the
injectable seam (guide §5) -- tests pass a fake, and the suite never reaches
the network (I9).

STDOUT IS THE PROTOCOL
----------------------
Anything printed to stdout that is not a JSON-RPC message corrupts the
session. ``serve()`` keeps the real stdout for itself and points
``sys.stdout`` at stderr for everything else, so a stray ``print`` anywhere
below cannot break the client.

LAUNCHING
---------
Must run as a module with ``~/Code`` on the path and the cwd kept OFF it --
from inside ``sertchi/``, ``sertchi/types.py`` shadows stdlib ``types``::

    PYTHONPATH=~/Code python3 -P -m sertchi.mcp_server

``-P`` (Python 3.11+) stops Python prepending the cwd, so this works from
whatever directory the MCP client happens to spawn it in.

ERRORS
------
Per MCP, a failed *search* is a tool result with ``isError: true`` (the model
should see it), while a malformed *request* -- unknown tool, unknown method --
is a JSON-RPC error. I3 carries through: a rejected API key
(``ProviderAuthError``) is reported as a configuration error naming the fix,
never dressed up as an empty result set.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from . import sync as _sync
from .types import InvalidRequest, Provider, ProviderAuthError, SearchResult, SearchType

__all__ = ["Server", "serve", "main"]

SERVER_NAME = "sertchi"
SERVER_VERSION = "1.0.0"

# Newest first; an unrecognised client version gets the newest.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
SNIPPET_CHARS = 500

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_PROVIDER_NAMES = ", ".join(p.value for p in Provider)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": f"Max results to return (default {DEFAULT_LIMIT}).",
        },
        "providers": {
            "type": "string",
            "description": (
                f"Optional comma-separated provider subset from {_PROVIDER_NAMES}. "
                "Omit to use the normal fallback chain. Use 'ddg' to force the "
                "free provider."
            ),
        },
    },
    "required": ["query"],
}

TOOLS: dict[str, dict[str, Any]] = {
    "web": {
        "search_type": SearchType.WEB,
        "description": (
            "Search the web via Sertchi (automatic fallback across "
            f"{_PROVIDER_NAMES}). Returns ranked title/url/snippet rows. "
            "Use this to FIND pages; use a fetch tool to READ one."
        ),
    },
    "news": {
        "search_type": SearchType.NEWS,
        "description": (
            "Search recent news via Sertchi. Same providers as the web tool but "
            "restricted to news, so results skew recent and may carry a "
            "published date. Use for current events; use the web tool for "
            "general lookups."
        ),
    },
}


class ToolInputError(ValueError):
    """Bad tool arguments -- reported to the model as an ``isError`` result."""


def _parse_providers(raw: Any) -> list[Provider] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, list) and all(isinstance(n, str) for n in raw):
        names = [n.strip().lower() for n in raw if n.strip()]
    else:
        raise ToolInputError("providers must be a comma-separated string")
    if not names:
        return None
    known = {p.value: p for p in Provider}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ToolInputError(
            f"unknown provider(s): {', '.join(unknown)} (known: {_PROVIDER_NAMES})"
        )
    return [known[n] for n in names]


def _parse_limit(raw: Any) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    # Small models often send "5" or 5.0; accept anything that is a whole number.
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ToolInputError("limit must be an integer") from None
    if value != int(value) or isinstance(raw, bool):
        raise ToolInputError("limit must be an integer")
    return max(1, min(MAX_LIMIT, int(value)))


def render(result: SearchResult, limit: int) -> str:
    """Markdown the model reads. Same shape the old OpenCode bridge produced."""
    rows = list(result.results)[:limit]
    head = (
        f'{len(rows)} result(s) for "{result.query}" via '
        f"{result.provider_used or 'no provider'} (status: {result.status.value})"
    )
    if not rows:
        # A zero-row answer is only useful if it says why.
        why = list(result.limitations) + [
            f"{a.provider}: {a.outcome.value}" + (f" ({a.error})" if a.error else "")
            for a in (result.attempts or [])
            if a.outcome.value != "success"
        ]
        return head + ("\n\nWhy:\n" + "\n".join(f"- {w}" for w in why) if why else "")

    body = []
    for i, r in enumerate(rows, 1):
        date = ""
        if r.published_date and r.date_confidence.value != "inferred":
            date = f" — {r.published_date}"
        snippet = " ".join((r.snippet or "").split())[:SNIPPET_CHARS]
        body.append(f"{i}. {r.title}{date}\n   {r.url}\n   {snippet}")
    notes = f"\nNotes: {'; '.join(result.limitations)}" if result.limitations else ""
    return head + "\n\n" + "\n\n".join(body) + notes


class Server:
    """Stateless MCP request handler. ``handle()`` maps one message to one reply."""

    def __init__(self, search: Callable[..., SearchResult] | None = None) -> None:
        self._search = search if search is not None else _sync.search

    # -- JSON-RPC ---------------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        """Return the response for ``message``, or ``None`` for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            # A response from the client (we never send requests) -- ignore.
            return None if is_notification or "result" in message or "error" in message \
                else _error(msg_id, INVALID_REQUEST, "missing method")
        if is_notification:
            return None  # notifications/initialized, notifications/cancelled, ...

        params = message.get("params") or {}
        if method == "initialize":
            return _result(msg_id, self._initialize(params))
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self._tool_list()})
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if name not in TOOLS:
                return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
            return _result(msg_id, self.call_tool(name, args))
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Web search via Sertchi. Use the web tool for general lookups and "
                "the news tool for current events."
            ),
        }

    @staticmethod
    def _tool_list() -> list[dict]:
        return [
            {"name": name, "description": spec["description"], "inputSchema": _INPUT_SCHEMA}
            for name, spec in TOOLS.items()
        ]

    # -- the tool itself --------------------------------------------------

    def call_tool(self, name: str, args: dict) -> dict:
        try:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ToolInputError("query must be a non-empty string")
            limit = _parse_limit(args.get("limit"))
            providers = _parse_providers(args.get("providers"))
        except ToolInputError as exc:
            return _tool_error(f"Invalid arguments: {exc}")

        # Each adapter reads only its own count knob; pass them all so whichever
        # provider answers returns about `limit` rows (Tavily has no knob, hence
        # the slice in render()).
        opts = {"limit": limit, "num": limit, "max_results": limit, "numResults": limit}
        try:
            result = self._search(
                query,
                TOOLS[name]["search_type"],
                providers=providers,
                include_attempts=True,
                **opts,
            )
        except ProviderAuthError as exc:
            # I3: a rejected key is a configuration error, not an empty result.
            # The exception's own message already names the fix (I6: never the key).
            return _tool_error(f"Search configuration error: {exc}")
        except InvalidRequest as exc:
            return _tool_error(f"Invalid search request: {exc}")
        except Exception as exc:  # noqa: BLE001 -- keep the server alive for the next call
            return _tool_error(f"Sertchi failed: {type(exc).__name__}: {exc}")

        shown = min(len(result.results), limit)
        return {
            "content": [{"type": "text", "text": render(result, limit)}],
            "structuredContent": {
                "status": result.status.value,
                "provider_used": result.provider_used,
                "results": shown,
                "limitations": list(result.limitations),
            },
            "isError": False,
        }


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(stdin: TextIO, stdout: TextIO, server: Server | None = None) -> None:
    """Read one JSON-RPC message per line from ``stdin`` until EOF."""
    server = server or Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, PARSE_ERROR, "parse error")
        else:
            # Batches were dropped from MCP in 2025-06-18; answer each element anyway.
            if isinstance(message, list):
                replies = [r for r in (server.handle(m) for m in message) if r is not None]
                response = replies or None
            else:
                response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # nothing but JSON-RPC may reach the real stdout
    try:
        serve(sys.stdin, protocol_out)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
