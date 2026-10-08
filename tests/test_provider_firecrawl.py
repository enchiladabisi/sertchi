"""Phase 4: providers/firecrawl.py -- Firecrawl adapter, #4 in the provider
order. Subclasses ProviderConformanceTests (Phase 1a) so Firecrawl is held to
the same contract every other adapter is (SKETCH.md §11), plus cost-rule,
date-confidence, snippet-truncation, and one-request-per-call tests specific
to Firecrawl.

All fixtures under tests/fixtures/firecrawl/ are docs-derived
("_shape_source": "docs 2026-08-20") -- no live Firecrawl call was made to
build this test module (fable-prompt-build-v1.md §6 Q1/Q2 -- Firecrawl has no
account yet).

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import ProviderBlocked, ProviderRateLimited
from sertchi.providers.firecrawl import FirecrawlProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, ProviderAuthError, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "firecrawl"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


WEB_OK = _load("web_ok.json")
NEWS_OK = _load("news_ok.json")
EMPTY = _load("empty.json")
MALFORMED = _load("malformed.json")


def _json_response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _success_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, WEB_OK)


def _news_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, NEWS_OK)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, EMPTY)


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("mock firecrawl timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(429, {"error": "rate limited"})


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(500, {"error": "server error"})


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    # I3's closed set restated for Firecrawl, same convention as tavily.py:
    # 401/403 mapped to ProviderBlocked.
    return _json_response(403, {"error": "forbidden"})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, MALFORMED)


def run(coro):
    return asyncio.run(coro)


class TestProviderFirecrawl(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> FirecrawlProvider:
        return FirecrawlProvider(api_key="fc-TESTKEY-0000-9f2c")

    def fixtures(self) -> ConformanceFixtures:
        return ConformanceFixtures(
            success_handler=_success_handler,
            empty_handler=_empty_handler,
            timeout_handler=_timeout_handler,
            rate_limited_handler=_rate_limited_handler,
            unavailable_handler=_unavailable_handler,
            blocked_handler=_blocked_handler,
            malformed_handler=_malformed_handler,
        )


class FirecrawlCostRuleTest(unittest.TestCase):
    """SKETCH.md §7 -- 2 * ceil(results_requested / 10), confirmed against
    Firecrawl's own docs' "2 credits per 10 results" framing, 2026-08-20."""

    def test_default_limit_ten_two_credits(self) -> None:
        provider = FirecrawlProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.credits, 2)
        self.assertEqual(cost.basis, "ceil(10/10)*2")

    def test_boundary_values_match_ceil_formula(self) -> None:
        provider = FirecrawlProvider(api_key="x")
        expected = [2, 2, 4, 6]
        got = [provider.estimate_cost(SearchType.WEB, limit=n).credits for n in (1, 10, 11, 23)]
        self.assertEqual(got, expected)

    def test_estimate_cost_is_pure_and_network_free(self) -> None:
        provider = FirecrawlProvider(api_key="x")
        provider.estimate_cost(SearchType.NEWS, limit=50)  # must not raise


class FirecrawlDateConfidenceTest(unittest.TestCase):
    """News rows: REPORTED when Firecrawl returns a real `date` string, NONE
    when a news row omits it -- no INFERRED emulation here, since Firecrawl's
    news vertical is a genuinely native endpoint (unlike DDG's recency-biased
    query emulation); a missing date on one row is an absence, not a guess."""

    def test_news_date_reported_when_present_none_when_absent(self) -> None:
        provider = FirecrawlProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_news_handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        self.assertEqual(first.published_date, "2026-07-22")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)

    def test_web_result_is_none_confidence(self) -> None:
        provider = FirecrawlProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertTrue(response.results)
        for row in response.results:
            self.assertIsNone(row.published_date)
            self.assertEqual(row.date_confidence, DateConfidence.NONE)


class FirecrawlSnippetTruncationTest(unittest.TestCase):
    """SNIPPET_MAX_CHARS = 400 (matches tributary-cpp-v2/config.py's
    snippet_char_cap). Sertchi is not a content store (SKETCH.md §9)."""

    def test_snippet_truncated_to_400_chars(self) -> None:
        from sertchi.providers import firecrawl as firecrawl_module

        long_description = "x" * 900
        payload = {
            "_shape_source": "docs 2026-08-20",
            "success": True,
            "data": {"web": [{
                "title": "Long snippet probe",
                "url": "https://example.com/long",
                "description": long_description,
            }]},
            "id": "sertchi-long-snippet-fixture",
            "creditsUsed": 2,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        provider = FirecrawlProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertEqual(len(response.results[0].snippet), firecrawl_module.SNIPPET_MAX_CHARS)


class FirecrawlOneRequestPerCallTest(unittest.TestCase):
    """I7/§6 -- Firecrawl's free tier caps at 2 concurrent requests; Sertchi
    must never issue more than one httpx call per search() invocation."""

    def test_exactly_one_request_issued(self) -> None:
        calls: list[httpx.Request] = []

        def counting_handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(200, WEB_OK)

        provider = FirecrawlProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(counting_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        run(go())
        self.assertEqual(len(calls), 1)


class FirecrawlRateLimitTest(unittest.TestCase):
    """429 -> ProviderRateLimited specifically, not just the
    ProviderOperationalError base class the generic conformance check settles
    for."""

    def test_429_maps_to_provider_rate_limited_specifically(self) -> None:
        provider = FirecrawlProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rate_limited_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        with self.assertRaises(ProviderRateLimited):
            run(go())


class FirecrawlRegistryTest(unittest.TestCase):
    def test_registered_under_firecrawl(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIn(Provider.FIRECRAWL, REGISTRY)
        self.assertIsInstance(REGISTRY[Provider.FIRECRAWL], FirecrawlProvider)


if __name__ == "__main__":
    unittest.main()


class FirecrawlAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error, not an operational one
    (user decision, 2026-08-20). Firecrawl uses 401 for a bad bearer token;
    its 403 reads as an exhausted plan and stays operational."""

    def _call(self, handler):
        provider = FirecrawlProvider(api_key="fc-TESTKEY-0000-9f2c")

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        return run(go())

    def test_401_raises_provider_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"error": "unauthorized"}))
        self.assertEqual(caught.exception.provider, "firecrawl")
        self.assertEqual(caught.exception.status_code, 401)

    def test_403_stays_operational_and_falls_through(self) -> None:
        with self.assertRaises(ProviderBlocked):
            self._call(lambda request: _json_response(403, {"error": "forbidden"}))

    def test_message_never_leaks_the_key(self) -> None:
        key = "fc-TESTKEY-0000-9f2c"
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"error": key}))
        self.assertNotIn(key, str(caught.exception))
