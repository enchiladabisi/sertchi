"""Phase 3: providers/tavily.py -- the #3-ranked, key-gated Tavily adapter.
Subclasses ProviderConformanceTests (Phase 1a) so Tavily is held to the same
contract every other adapter is (SKETCH.md §11), plus cost-rule and
D13-derived (published_date/provider_score) tests specific to Tavily.

All fixtures under tests/fixtures/tavily/ are docs-derived
("_shape_source": "docs 2026-08-20") -- no live Tavily call was made to
build this test module (fable-prompt-build-v1.md §6 Q2).

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import (
    ProviderBlocked,
    ProviderOperationalError,
    ProviderRateLimited,
)
from sertchi.types import ProviderAuthError
from sertchi.providers.tavily import TavilyProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "tavily"


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
    raise httpx.ConnectTimeout("mock tavily timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(429, {"detail": "rate limited"})


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(500, {"detail": "server error"})


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    # I3's closed set restated for Tavily: no HTTP-200 challenge page like
    # DDG's -- a 403 (auth/permission refusal) is this provider's "you are
    # not getting an answer from me right now" signal instead.
    return _json_response(403, {"detail": "forbidden"})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, MALFORMED)


def run(coro):
    return asyncio.run(coro)


class TestProviderTavily(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> TavilyProvider:
        return TavilyProvider(api_key="tvly-TESTKEY-0000-9f2c")

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


class TavilyCostRuleTest(unittest.TestCase):
    """SKETCH.md §7/#8 -- the adapter owns its own cost rule; Sertchi
    requests `basic` by default, `advanced` only when the caller passes it."""

    def test_default_depth_is_basic_one_credit(self) -> None:
        provider = TavilyProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.credits, 1)
        self.assertEqual(cost.basis, "basic depth")

    def test_explicit_basic_depth_one_credit(self) -> None:
        provider = TavilyProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, search_depth="basic")
        self.assertEqual(cost.credits, 1)
        self.assertEqual(cost.basis, "basic depth")

    def test_advanced_depth_two_credits(self) -> None:
        provider = TavilyProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, search_depth="advanced")
        self.assertEqual(cost.credits, 2)
        self.assertEqual(cost.basis, "advanced depth")

    def test_estimate_cost_is_pure_and_network_free(self) -> None:
        # No client, no await inside estimate_cost -- calling it directly,
        # synchronously, with no MockTransport at all, proves it never
        # touches the network (I9/§11).
        provider = TavilyProvider(api_key="x")
        provider.estimate_cost(SearchType.NEWS, search_depth="advanced")  # must not raise


class TavilyDateParsingTest(unittest.TestCase):
    """D13 -- published_date is real and populated in RFC-822 form; the
    exact sample string `Wed, 22 Jul 2026 11:18:07 GMT` must parse."""

    def test_news_date_parsed_to_iso_with_reported_confidence(self) -> None:
        provider = TavilyProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_news_handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        self.assertEqual(first.published_date, "2026-07-22")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        self.assertEqual(second.published_date, "2026-07-23")
        self.assertEqual(second.date_confidence, DateConfidence.REPORTED)

    def test_web_result_without_published_date_is_none_confidence(self) -> None:
        provider = TavilyProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertTrue(response.results)
        for row in response.results:
            self.assertIsNone(row.published_date)
            self.assertEqual(row.date_confidence, DateConfidence.NONE)


class TavilyScoreOrderingTest(unittest.TestCase):
    """D13 -- provider_score is relevance, not quality, and must never
    reorder results. web_ok.json deliberately lists the LOWER-scoring row
    first to make an accidental sort-by-score visible if introduced."""

    def test_provider_score_populated_but_response_order_preserved(self) -> None:
        provider = TavilyProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        # Provider's own order preserved (low score first, high score
        # second) -- NOT re-sorted descending by provider_score.
        self.assertEqual(first.provider_score, 0.35)
        self.assertEqual(first.provider_rank, 1)
        self.assertEqual(second.provider_score, 0.81)
        self.assertEqual(second.provider_rank, 2)


class TavilyRateLimitTest(unittest.TestCase):
    """The build plan calls this out by name: 429 -> ProviderRateLimited
    specifically, not just the ProviderOperationalError base class the
    generic conformance check settles for."""

    def test_429_maps_to_provider_rate_limited_specifically(self) -> None:
        provider = TavilyProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rate_limited_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        with self.assertRaises(ProviderRateLimited):
            run(go())


class TavilyRegistryTest(unittest.TestCase):
    def test_registered_under_tavily(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIn(Provider.TAVILY, REGISTRY)
        self.assertIsInstance(REGISTRY[Provider.TAVILY], TavilyProvider)


if __name__ == "__main__":
    unittest.main()


class TavilyAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error, not an operational one
    (user decision, 2026-08-20). 401 escapes; 403 still falls through.

    The split is plain HTTP semantics: 401 is "I don't know who you are",
    which for a keyed adapter means the key is wrong or missing; 403 is
    "I know who you are and still refuse", which covers an exhausted plan
    or a region block -- things a different provider might succeed at.
    """

    def _call(self, handler):
        provider = TavilyProvider(api_key="tvly-TESTKEY-0000-9f2c")

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        return run(go())

    def test_401_raises_provider_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"detail": "unauthorized"}))
        self.assertEqual(caught.exception.provider, "tavily")
        self.assertEqual(caught.exception.status_code, 401)

    def test_provider_auth_error_is_not_operational(self) -> None:
        """The load-bearing assertion. If ProviderAuthError were ever made a
        ProviderOperationalError subclass, the orchestrator's
        `except ProviderOperationalError` would swallow it and a bad key
        would go back to falling through silently."""
        self.assertFalse(issubclass(ProviderAuthError, ProviderOperationalError))

    def test_403_stays_operational_and_falls_through(self) -> None:
        with self.assertRaises(ProviderBlocked):
            self._call(lambda request: _json_response(403, {"detail": "forbidden"}))

    def test_message_never_leaks_the_key(self) -> None:
        """I6 -- the exception text is user-facing and must name the provider
        and status only."""
        key = "tvly-TESTKEY-0000-9f2c"
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"detail": key}))
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn("TESTKEY", str(caught.exception))
