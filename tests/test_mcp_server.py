"""Tests for ``sertchi.mcp_server`` -- the stdio MCP front door.

Every test injects a fake ``search`` through ``Server(search=...)`` (the DI
seam), so nothing here can reach a provider or the ledger (I9). The one
subprocess test runs the real module but only speaks ``initialize`` and
``tools/list``, which never search.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from sertchi import mcp_server
from sertchi.mcp_server import Server, render, serve
from sertchi.types import (
    AttemptOutcome,
    AttemptRecord,
    DateConfidence,
    InvalidRequest,
    Provider,
    ProviderAuthError,
    ResultItem,
    SearchResult,
    SearchStatus,
    SearchType,
)


def _row(i: int, **kw) -> ResultItem:
    base = dict(
        title=f"Title {i}",
        url=f"https://example.com/{i}",
        snippet=f"snippet   {i}\nwith  whitespace",
        domain="example.com",
        registrable_domain="example.com",
        published_date=None,
        date_confidence=DateConfidence.NONE,
        provider="keenable",
        provider_rank=i,
    )
    base.update(kw)
    return ResultItem(**base)


def _result(rows, *, status=SearchStatus.OK, provider="keenable", attempts=None,
            limitations=None, search_type=SearchType.WEB) -> SearchResult:
    return SearchResult(
        query="q",
        search_type=search_type,
        status=status,
        provider_used=provider,
        attempts=attempts or [],
        results=list(rows),
        limitations=limitations or [],
    )


class FakeSearch:
    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else _result([_row(1)])
        self.raises = raises
        self.calls = []

    def __call__(self, query, search_type, **kw):
        self.calls.append((query, search_type, kw))
        if self.raises is not None:
            raise self.raises
        return self.result


def _call(server, name, args, msg_id=1):
    return server.handle({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSearch()
        self.server = Server(search=self.fake)

    def test_initialize_echoes_supported_version(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "sertchi")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_initialize_unknown_version_gets_newest(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"],
                         mcp_server.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_notification_gets_no_reply(self):
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": "p", "method": "ping"})
        self.assertEqual(r, {"jsonrpc": "2.0", "id": "p", "result": {}})

    def test_unknown_method(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(r["error"]["code"], mcp_server.METHOD_NOT_FOUND)

    def test_not_jsonrpc(self):
        r = self.server.handle({"id": 1, "method": "ping"})
        self.assertEqual(r["error"]["code"], mcp_server.INVALID_REQUEST)

    def test_tools_list_names_and_schema(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"web", "news"})
        schema = tools["web"]["inputSchema"]
        self.assertEqual(schema["required"], ["query"])
        # Provider list is derived from the enum, so a new adapter shows up here.
        for p in Provider:
            self.assertIn(p.value, schema["properties"]["providers"]["description"])

    def test_unknown_tool_is_protocol_error(self):
        r = _call(self.server, "images", {"query": "x"})
        self.assertEqual(r["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertEqual(self.fake.calls, [])


class TestToolCall(unittest.TestCase):
    def test_web_call_forwards_type_and_limit_knobs(self):
        fake = FakeSearch()
        r = _call(Server(search=fake), "web", {"query": "llama", "limit": 3})
        self.assertFalse(r["result"]["isError"])
        (query, stype, kw), = fake.calls
        self.assertEqual((query, stype), ("llama", SearchType.WEB))
        self.assertIsNone(kw["providers"])
        for knob in ("limit", "num", "max_results", "numResults"):
            self.assertEqual(kw[knob], 3)

    def test_news_tool_uses_news_type(self):
        fake = FakeSearch()
        _call(Server(search=fake), "news", {"query": "x"})
        self.assertEqual(fake.calls[0][1], SearchType.NEWS)
        self.assertEqual(fake.calls[0][2]["limit"], mcp_server.DEFAULT_LIMIT)

    def test_limit_is_clamped_and_accepts_numeric_strings(self):
        fake = FakeSearch()
        _call(Server(search=fake), "web", {"query": "x", "limit": "50"})
        _call(Server(search=fake), "web", {"query": "x", "limit": 0})
        self.assertEqual([c[2]["limit"] for c in fake.calls], [mcp_server.MAX_LIMIT, 1])

    def test_bad_limit_is_tool_error_not_search(self):
        fake = FakeSearch()
        r = _call(Server(search=fake), "web", {"query": "x", "limit": 2.5})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(fake.calls, [])

    def test_providers_parsed_from_string_or_list(self):
        fake = FakeSearch()
        _call(Server(search=fake), "web", {"query": "x", "providers": " DDG, tavily "})
        _call(Server(search=fake), "web", {"query": "x", "providers": ["exa"]})
        _call(Server(search=fake), "web", {"query": "x", "providers": ""})
        self.assertEqual(fake.calls[0][2]["providers"], [Provider.DDG, Provider.TAVILY])
        self.assertEqual(fake.calls[1][2]["providers"], [Provider.EXA])
        self.assertIsNone(fake.calls[2][2]["providers"])

    def test_unknown_provider_is_tool_error(self):
        fake = FakeSearch()
        r = _call(Server(search=fake), "web", {"query": "x", "providers": "bing"})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("bing", r["result"]["content"][0]["text"])
        self.assertEqual(fake.calls, [])

    def test_blank_query_is_tool_error(self):
        fake = FakeSearch()
        r = _call(Server(search=fake), "web", {"query": "   "})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(fake.calls, [])

    def test_auth_error_surfaces_as_configuration_error(self):
        # I3: a rejected key must not look like an empty result.
        fake = FakeSearch(raises=ProviderAuthError("tavily", 401))
        r = _call(Server(search=fake), "web", {"query": "x"})
        self.assertTrue(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("configuration error", text)
        self.assertIn("keys set tavily", text)

    def test_invalid_request_is_tool_error(self):
        fake = FakeSearch(raises=InvalidRequest("nope"))
        r = _call(Server(search=fake), "web", {"query": "x"})
        self.assertTrue(r["result"]["isError"])

    def test_unexpected_exception_keeps_server_alive(self):
        fake = FakeSearch(raises=KeyError("boom"))
        server = Server(search=fake)
        r = _call(server, "web", {"query": "x"})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("KeyError", r["result"]["content"][0]["text"])
        ping = server.handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(ping["result"], {})

    def test_structured_content(self):
        fake = FakeSearch(result=_result([_row(1), _row(2)], provider="exa"))
        r = _call(Server(search=fake), "web", {"query": "x", "limit": 1})
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc, {"status": "ok", "provider_used": "exa", "results": 1,
                              "limitations": []})


class TestRender(unittest.TestCase):
    def test_rows_are_sliced_and_whitespace_collapsed(self):
        text = render(_result([_row(i) for i in range(1, 8)]), 2)
        self.assertTrue(text.startswith('2 result(s) for "q" via keenable (status: ok)'))
        self.assertIn("1. Title 1\n   https://example.com/1\n   snippet 1 with whitespace", text)
        self.assertNotIn("Title 3", text)

    def test_reported_date_shown_inferred_date_hidden(self):
        text = render(_result([
            _row(1, published_date="2026-09-20", date_confidence=DateConfidence.REPORTED),
            _row(2, published_date="2026-09-21", date_confidence=DateConfidence.INFERRED),
        ]), 5)
        self.assertIn("Title 1 — 2026-09-20", text)
        self.assertNotIn("2026-09-21", text)

    def test_empty_result_explains_why(self):
        text = render(_result(
            [], status=SearchStatus.EMPTY, provider=None,
            attempts=[
                AttemptRecord("ddg", AttemptOutcome.ERROR, 12.0, "ProviderRateLimited"),
                AttemptRecord("exa", AttemptOutcome.SUCCESS, 5.0, None),
            ],
            limitations=["tavily skipped: no API key"],
        ), 5)
        self.assertIn("via no provider (status: empty)", text)
        self.assertIn("- tavily skipped: no API key", text)
        self.assertIn("- ddg: error (ProviderRateLimited)", text)
        self.assertNotIn("exa", text)


class TestServeLoop(unittest.TestCase):
    def test_one_reply_per_request_and_parse_error(self):
        fake = FakeSearch()
        stdin = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
            "{not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "web", "arguments": {"query": "x"}}}),
        ]) + "\n")
        stdout = io.StringIO()
        serve(stdin, stdout, Server(search=fake))
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], mcp_server.PARSE_ERROR)
        self.assertEqual(len(fake.calls), 1)

    def test_module_launch_from_inside_package_dir(self):
        # The documented launch line must survive a cwd of sertchi/ itself,
        # where sertchi/types.py would otherwise shadow stdlib `types`.
        pkg = Path(mcp_server.__file__).resolve().parent
        env = dict(os.environ, PYTHONPATH=str(pkg.parent))
        msgs = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]) + "\n"
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "sertchi.mcp_server"],
            input=msgs, capture_output=True, text=True, cwd=pkg, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(l) for l in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]},
                         {"web", "news"})


if __name__ == "__main__":
    unittest.main()
