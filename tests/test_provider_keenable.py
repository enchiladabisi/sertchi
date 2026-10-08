"""Phase 2 (etc/planning/fable-prompt-keenable-exa-reorder.md): providers/keenable.py
-- Keenable adapter, #1 in the target six-provider order. Subclasses
ProviderConformanceTests so Keenable is held to the same contract every other
adapter is (SKETCH.md §11), plus cost-rule, auth-divergence, date-confidence,
and snippet-fallback tests specific to Keenable.

All fixtures under tests/fixtures/keenable/ are docs-derived
("_shape_source": "docs 2026-09-13") -- no live Keenable call was made to
build this test module (the plan's Phase 0-6 no-live-calls posture).

Run from ~/Code via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import ProviderBlocked
from sertchi.providers.keenable import KeenableProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, ProviderAuthError, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "keenable"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


WEB_OK = _load("web_ok.json")
NEWS_OK = _load("news_ok.json")
EMPTY = _load("empty.json")
MALFORMED = _load("malformed.json")


def _json_response(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _success_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, WEB_OK)


def _news_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, NEWS_OK)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, EMPTY)


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("mock keenable timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(429, {"message": "rate limited"})


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(500, {"message": "server error"})


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    # Keenable's "out of credits" signal is 402, per the plan's decision
    # table -- operational, falls through, unlike 401/403 which are
    # configuration errors (see KeenableAuthClassificationTest below).
    return _json_response(402, {"message": "out of credits"})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, MALFORMED)


def run(coro):
    return asyncio.run(coro)


class TestProviderKeenable(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> KeenableProvider:
        return KeenableProvider(api_key="keen_TESTKEY0000-9f2c")

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


class KeenableAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error, not an operational one (user
    decision, 2026-08-20, reused here). 402 ("out of credits") is the
    opposite: operational, falls through -- the two-directional check the
    plan specifically calls for, since getting this backwards is the
    expensive failure (a bad key silently degrading to a worse provider, or
    a broke account dead-ending the whole chain).

    Keenable is the SECOND adapter (after serper.py) whose _AUTH_STATUSES is
    {401, 403} rather than firecrawl.py's {401}-only set -- deliberate and
    evidenced (Keenable's docs: 401 = missing/invalid key, 403 = key
    disabled/revoked; both mean "fix your key"). Mirrors
    test_provider_serper.py::SerperAuthClassificationTest's own divergence
    pin.
    """

    def _call(self, handler):
        provider = KeenableProvider(api_key="keen_TESTKEY0000-9f2c")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        return run(go())

    def test_402_raises_provider_blocked_not_auth_error(self) -> None:
        """The two-directional check: 402 must be ProviderBlocked, AND must
        NOT be a ProviderAuthError -- both directions matter."""
        with self.assertRaises(ProviderBlocked):
            self._call(lambda request: _json_response(402, {"message": "out of credits"}))
        with self.assertRaises(Exception) as caught:
            self._call(lambda request: _json_response(402, {"message": "out of credits"}))
        self.assertNotIsInstance(caught.exception, ProviderAuthError)

    def test_401_raises_provider_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"message": "unauthorized"}))
        self.assertEqual(caught.exception.status_code, 401)

    def test_403_also_raises_provider_auth_error(self) -> None:
        """The divergence from firecrawl.py's {401}-only set, asserted
        explicitly so nobody 'harmonises' it away without reading why --
        Keenable's docs document 403 as a disabled/revoked key, not an
        exhausted plan (contrast firecrawl.py, where 403 stays operational)."""
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(403, {"message": "forbidden"}))
        self.assertEqual(caught.exception.provider, "keenable")
        self.assertEqual(caught.exception.status_code, 403)

    def test_message_never_leaks_the_key(self) -> None:
        key = "keen_TESTKEY0000-9f2c"
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(403, {"message": key}))
        self.assertNotIn(key, str(caught.exception))


class KeenableCostRuleTest(unittest.TestCase):
    """Flat 1 credit per search, independent of max_results -- unlike
    Firecrawl's/Serper's tiered formulas. Pinned so nobody "helpfully"
    scales this with result count."""

    def test_flat_one_credit_regardless_of_max_results(self) -> None:
        provider = KeenableProvider(api_key="x")
        for n in (1, 10, 50):
            cost = provider.estimate_cost(SearchType.WEB, max_results=n)
            self.assertEqual(cost.credits, 1, f"max_results={n}")
            self.assertEqual(cost.unit, "api_credit")
            self.assertEqual(cost.basis, "1 per search")

    def test_estimate_cost_is_pure_and_network_free(self) -> None:
        provider = KeenableProvider(api_key="x")
        provider.estimate_cost(SearchType.NEWS, max_results=50)  # must not raise


class KeenableNewsRequestTest(unittest.TestCase):
    """I8: Keenable has no native news endpoint -- NEWS is modeled as a date
    filter on the same /v1/search call. Assert the request body actually
    carries it."""

    def test_news_request_body_carries_published_after(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _json_response(200, NEWS_OK)

        provider = KeenableProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("published_after"), "7d")

    def test_web_request_body_has_no_published_after(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _json_response(200, WEB_OK)

        provider = KeenableProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertNotIn("published_after", captured[0])


class KeenableDateConfidenceTest(unittest.TestCase):
    """published_at is a genuinely native field (unlike DDG's emulated
    recency), so an absence reads as NONE, never INFERRED -- the
    firecrawl.py precedent, not serper.py's news-row handling."""

    def test_news_row_missing_published_at_is_none_confidence(self) -> None:
        provider = KeenableProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_news_handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        self.assertEqual(first.published_date, "2026-09-10T14:03:00Z")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)


class KeenableSnippetFallbackTest(unittest.TestCase):
    """snippet falls back to description when snippet is absent -- both
    fields are documented as "if available"."""

    def test_snippet_falls_back_to_description(self) -> None:
        provider = KeenableProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        # web_ok.json row 2 has no "snippet" key, only "description".
        second = response.results[1]
        self.assertEqual(second.snippet, "Second web result description used for conformance testing.")


class KeenableAuthHeaderTest(unittest.TestCase):
    """I6: the key goes in a header, never a URL."""

    def test_key_sent_as_header_never_in_url(self) -> None:
        key = "keen_TESTKEY0000-9f2c"
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _json_response(200, WEB_OK)

        provider = KeenableProvider(api_key=key)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers.get("x-api-key"), key)
        self.assertNotIn(key, str(captured[0].url))


class KeenableRegistryTest(unittest.TestCase):
    def test_registered_under_keenable(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIn(Provider.KEENABLE, REGISTRY)
        self.assertIsInstance(REGISTRY[Provider.KEENABLE], KeenableProvider)


if __name__ == "__main__":
    unittest.main()
