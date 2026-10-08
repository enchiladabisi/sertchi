"""Phase 4: providers/serper.py -- Serper adapter, #5 in the provider order.
Subclasses ProviderConformanceTests (Phase 1a) so Serper is held to the same
contract every other adapter is (SKETCH.md §11), plus cost-rule,
date-confidence, and field-name tests specific to Serper.

All fixtures under tests/fixtures/serper/ are docs-derived
("_shape_source": "docs 2026-08-20") -- no live Serper call was made to build
this test module (fable-prompt-build-v1.md §6 Q1/Q2 -- Serper has no account
yet).

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import ProviderRateLimited
from sertchi.providers.serper import SerperProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, ProviderAuthError, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "serper"


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
    raise httpx.ConnectTimeout("mock serper timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(429, {"message": "rate limited"})


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(500, {"message": "server error"})


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    # Serper has no block/challenge signal distinct from its auth and
    # rate-limit responses, so this deliberately duplicates the 429 case.
    #
    # It USED to be a 403, but 403 is Serper's bad-key signal (see
    # providers/serper.py's _AUTH_STATUSES) and now raises ProviderAuthError
    # -- a configuration error that escapes rather than an operational one
    # that falls through. Reusing 429 here keeps conformance check #7
    # asserting real behaviour instead of a status code Serper never sends.
    return _json_response(429, {"message": "rate limited"})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, MALFORMED)


def run(coro):
    return asyncio.run(coro)


class TestProviderSerper(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> SerperProvider:
        return SerperProvider(api_key="serper-TESTKEY-0000-9f2c")

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


class SerperCostRuleTest(unittest.TestCase):
    """SKETCH.md §7 -- 1 credit for <=10 results, 2 for 11-100. Confirmed
    uniform across web/news/images 2026-08-20 (module docstring point (i));
    do NOT simplify to a flat 1."""

    def test_default_num_ten_one_credit(self) -> None:
        provider = SerperProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.credits, 1)
        self.assertEqual(cost.basis, "<=10 results")

    def test_boundary_values_match_tiered_formula(self) -> None:
        provider = SerperProvider(api_key="x")
        expected = [1, 1, 2, 2]
        got = [provider.estimate_cost(SearchType.WEB, num=n).credits for n in (1, 10, 11, 100)]
        self.assertEqual(got, expected)

    def test_eleven_results_two_credits_basis(self) -> None:
        provider = SerperProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, num=11)
        self.assertEqual(cost.credits, 2)
        self.assertEqual(cost.basis, "11-100 results")

    def test_estimate_cost_is_pure_and_network_free(self) -> None:
        provider = SerperProvider(api_key="x")
        provider.estimate_cost(SearchType.NEWS, num=50)  # must not raise


class SerperDateConfidenceTest(unittest.TestCase):
    """News rows: REPORTED only when the news endpoint returns a real `date`
    field; otherwise INFERRED for news, NONE for web (fable-prompt-build-v1.md
    §5 Phase 4 -- pinned explicitly, unlike Firecrawl's adjacent judgment
    call, see providers/firecrawl.py's docstring)."""

    def test_news_date_reported_when_present_inferred_when_absent(self) -> None:
        provider = SerperProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_news_handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        self.assertEqual(first.published_date, "2026-07-22")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.INFERRED)

    def test_web_result_is_none_confidence(self) -> None:
        provider = SerperProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertTrue(response.results)
        for row in response.results:
            self.assertIsNone(row.published_date)
            self.assertEqual(row.date_confidence, DateConfidence.NONE)


class SerperFieldNameTest(unittest.TestCase):
    """Serper's rows use `link`, not `url` -- the one field-name divergence
    from every other adapter in this tree. This test exists specifically to
    catch a future accidental `row.get("url")` regression."""

    def test_organic_link_field_maps_to_result_url(self) -> None:
        provider = SerperProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertTrue(response.results)
        self.assertTrue(all(row.url.startswith("http") for row in response.results))


class SerperRateLimitTest(unittest.TestCase):
    """429 -> ProviderRateLimited specifically, not just the
    ProviderOperationalError base class the generic conformance check settles
    for."""

    def test_429_maps_to_provider_rate_limited_specifically(self) -> None:
        provider = SerperProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rate_limited_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        with self.assertRaises(ProviderRateLimited):
            run(go())


class SerperRegistryTest(unittest.TestCase):
    def test_registered_under_serper(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIn(Provider.SERPER, REGISTRY)
        self.assertIsInstance(REGISTRY[Provider.SERPER], SerperProvider)


if __name__ == "__main__":
    unittest.main()


class SerperAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error, not an operational one
    (user decision, 2026-08-20).

    Serper is the ONE adapter where 403 also means "bad key" -- tavily.py
    and firecrawl.py treat 403 as operational. Evidence is secondary
    (crewAI issue #633 and similar reports; Serper's own docs page 404s),
    and 403 is overloaded to cover account restrictions too. The accepted
    trade-off is to surface it rather than fall through silently.
    """

    def _call(self, handler):
        provider = SerperProvider(api_key="serper-TESTKEY-0000-9f2c")

        async def go():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        return run(go())

    def test_401_raises_provider_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"message": "unauthorized"}))
        self.assertEqual(caught.exception.status_code, 401)

    def test_403_also_raises_provider_auth_error(self) -> None:
        """The divergence from the other two adapters, asserted explicitly so
        nobody 'harmonises' it away without reading why."""
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(403, {"message": "forbidden"}))
        self.assertEqual(caught.exception.provider, "serper")
        self.assertEqual(caught.exception.status_code, 403)

    def test_message_never_leaks_the_key(self) -> None:
        key = "serper-TESTKEY-0000-9f2c"
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(403, {"message": key}))
        self.assertNotIn(key, str(caught.exception))
