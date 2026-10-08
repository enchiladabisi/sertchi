"""providers/brave.py -- Brave Search adapter. Subclasses
ProviderConformanceTests so Brave is held to the same contract every other
adapter is (SKETCH.md §11), plus the Brave-specific behaviours: the
overloaded-422 auth rule, per-vertical row locations, HTML stripping, the
page_age-only date rule, and the micro-dollar cost.

Fixtures under tests/fixtures/brave/ are docs-derived ("_shape_source":
"docs 2026-10-08") -- no live Brave call was made to build this module. The
bad-key 422 was confirmed live 2026-10-08; the 402 credits-spent status and
news pricing remain UNVERIFIED (see providers/brave.py) and are pinned as
behaviour Sertchi chose.

Run from ~/Code via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import ProviderBlocked, ProviderRateLimited
from sertchi.providers.brave import BraveProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, ProviderAuthError, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "brave"
KEY = "BSA_TESTKEY0000-9f2c"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


WEB_OK = _load("web_ok.json")
NEWS_OK = _load("news_ok.json")
EMPTY = _load("empty.json")
MALFORMED = _load("malformed.json")


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("mock brave timeout")


def run(coro):
    return asyncio.run(coro)


def _call(handler, search_type=SearchType.WEB, **opts):
    provider = BraveProvider(api_key=KEY)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider.search("q", search_type, client=client, **opts)

    return run(go())


class TestProviderBrave(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> BraveProvider:
        return BraveProvider(api_key=KEY)

    def fixtures(self) -> ConformanceFixtures:
        return ConformanceFixtures(
            success_handler=lambda r: _json(200, WEB_OK),
            empty_handler=lambda r: _json(200, EMPTY),
            timeout_handler=_timeout_handler,
            rate_limited_handler=lambda r: _json(429, {"error": {"code": "RATE_LIMITED"}}),
            unavailable_handler=lambda r: _json(500, {"message": "server error"}),
            blocked_handler=lambda r: _json(402, {"message": "out of credits"}),
            malformed_handler=lambda r: _json(200, MALFORMED),
        )


class BraveAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error; spent credits are
    operational. Brave's 422 is overloaded (validation AND, per memory of its
    error schema, a bad token), so it is told apart by error.code."""

    def test_401_is_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            _call(lambda r: _json(401, {"message": "unauthorized"}))
        self.assertEqual(caught.exception.status_code, 401)

    def test_422_with_token_code_is_auth_error(self) -> None:
        # Body shape captured live 2026-10-08 from a bogus token.
        body = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID",
                          "detail": "The provided subscription token is invalid.",
                          "meta": {"component": "authentication"}, "status": 422},
                "type": "ErrorResponse"}
        with self.assertRaises(ProviderAuthError) as caught:
            _call(lambda r: _json(422, body))
        self.assertEqual(caught.exception.provider, "brave")
        self.assertEqual(caught.exception.status_code, 422)

    def test_plain_422_is_not_auth_and_not_swallowed(self) -> None:
        """A validation failure is our bug: it must neither blame the key
        nor fall through to another provider."""
        body = {"error": {"status": 422, "code": "VALIDATION"}}
        with self.assertRaises(httpx.HTTPStatusError):
            _call(lambda r: _json(422, body))

    def test_422_with_unparseable_body_is_not_auth(self) -> None:
        with self.assertRaises(httpx.HTTPStatusError):
            _call(lambda r: httpx.Response(422, text="<html>nope</html>"))

    def test_402_is_blocked_and_not_auth(self) -> None:
        with self.assertRaises(ProviderBlocked) as caught:
            _call(lambda r: _json(402, {"message": "out of credits"}))
        self.assertNotIsInstance(caught.exception, ProviderAuthError)

    def test_403_is_operational(self) -> None:
        with self.assertRaises(ProviderBlocked):
            _call(lambda r: _json(403, {"message": "forbidden"}))

    def test_429_is_rate_limited(self) -> None:
        with self.assertRaises(ProviderRateLimited):
            _call(lambda r: _json(429, {}))

    def test_message_never_leaks_the_key(self) -> None:
        body = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": KEY}}
        with self.assertRaises(ProviderAuthError) as caught:
            _call(lambda r: _json(422, body))
        self.assertNotIn(KEY, str(caught.exception))


class BraveRequestShapeTest(unittest.TestCase):
    def _capture(self, search_type, payload, **opts) -> httpx.Request:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _json(200, payload)

        _call(handler, search_type, **opts)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_web_hits_web_endpoint_with_no_freshness(self) -> None:
        req = self._capture(SearchType.WEB, WEB_OK)
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.url.path, "/res/v1/web/search")
        self.assertNotIn("freshness", req.url.params)

    def test_news_hits_news_endpoint_with_week_freshness(self) -> None:
        req = self._capture(SearchType.NEWS, NEWS_OK)
        self.assertEqual(req.url.path, "/res/v1/news/search")
        self.assertEqual(req.url.params["freshness"], "pw")

    def test_key_is_a_header_never_in_the_url(self) -> None:
        req = self._capture(SearchType.WEB, WEB_OK)
        self.assertEqual(req.headers["x-subscription-token"], KEY)
        self.assertNotIn(KEY, str(req.url))

    def test_count_is_clamped_to_the_vertical_cap(self) -> None:
        self.assertEqual(
            self._capture(SearchType.WEB, WEB_OK, count=99).url.params["count"], "20")
        self.assertEqual(
            self._capture(SearchType.NEWS, NEWS_OK, count=99).url.params["count"], "50")
        self.assertEqual(
            self._capture(SearchType.WEB, WEB_OK).url.params["count"], "10")


class BraveRowParsingTest(unittest.TestCase):
    def test_web_rows_come_from_web_results(self) -> None:
        rows = _call(lambda r: _json(200, WEB_OK)).results
        self.assertEqual(len(rows), 2)

    def test_news_rows_come_from_top_level_results(self) -> None:
        rows = _call(lambda r: _json(200, NEWS_OK), SearchType.NEWS).results
        self.assertEqual(len(rows), 2)

    def test_html_and_entities_are_stripped_from_title_and_snippet(self) -> None:
        first = _call(lambda r: _json(200, WEB_OK)).results[0]
        self.assertEqual(first.title, "Sertchi & Conformance Probe Result One")
        self.assertEqual(
            first.snippet, "First web result used for conformance testing & nothing else.")

    def test_page_age_is_the_date_and_relative_age_is_ignored(self) -> None:
        first, second = _call(lambda r: _json(200, WEB_OK)).results
        self.assertEqual(first.published_date, "2026-10-06T14:03:00")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        # Row two has age="3 weeks ago" only -- an absence, never INFERRED.
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)

    def test_news_row_without_page_age_is_none_confidence(self) -> None:
        first, second = _call(lambda r: _json(200, NEWS_OK), SearchType.NEWS).results
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)

    def test_missing_web_object_is_empty_not_malformed(self) -> None:
        self.assertEqual(_call(lambda r: _json(200, EMPTY)).results, [])

    def test_non_dict_web_object_is_malformed(self) -> None:
        from sertchi.providers.base import ProviderMalformedResponse
        with self.assertRaises(ProviderMalformedResponse):
            _call(lambda r: _json(200, {"web": []}))


class BraveCostRuleTest(unittest.TestCase):
    """One search = $0.005 = 5,000 micro-dollars, flat for any count and for
    both verticals (news pricing UNVERIFIED -- see providers/brave.py)."""

    def test_flat_5000_usd_micro(self) -> None:
        provider = BraveProvider(api_key="x")
        for search_type in SearchType:
            for count in (1, 10, 50):
                cost = provider.estimate_cost(search_type, count=count)
                self.assertEqual((cost.credits, cost.unit), (5_000, "usd_micro"))

    def test_response_usage_comes_from_the_formula(self) -> None:
        response = _call(lambda r: _json(200, WEB_OK))
        self.assertEqual(response.usage.credits, 5_000)

    def test_rate_limit_header_is_recorded_but_never_reported_as_remaining(self) -> None:
        """X-RateLimit-Remaining is in requests; the ledger is in
        micro-dollars, so it must not reach reported_remaining."""
        def handler(request):
            return httpx.Response(200, json=WEB_OK, headers={"X-RateLimit-Remaining": "1, 998"})

        meta = _call(handler).provider_metadata
        self.assertIsNone(meta["reported_remaining"])
        self.assertEqual(meta["rate_limit_remaining_raw"], "1, 998")


class BraveLedgerDefaultsTest(unittest.TestCase):
    def test_default_pool_is_the_five_dollar_monthly_credit(self) -> None:
        from sertchi import usage
        from sertchi.types import QuotaKind

        d = usage.DEFAULTS["brave"]
        self.assertEqual((d.quota, d.quota_kind, d.plan), (5_000_000, QuotaKind.MONTHLY, "free"))
        # The reserve is five searches' worth, so the gate trips before the
        # credit is truly spent.
        self.assertEqual(d.reserve, 5 * 5_000)


class BraveRegistryTest(unittest.TestCase):
    def test_registered_under_brave(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIsInstance(REGISTRY[Provider.BRAVE], BraveProvider)


if __name__ == "__main__":
    unittest.main()
