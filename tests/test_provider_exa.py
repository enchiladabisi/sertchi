"""Phase 3 (etc/planning/fable-prompt-keenable-exa-reorder.md): providers/exa.py
-- Exa adapter, #2 in the target six-provider order. Subclasses
ProviderConformanceTests so Exa is held to the same contract every other
adapter is (SKETCH.md §11), plus cost-rule, auth-divergence,
contents/highlights-boundary, category, and date-confidence tests specific
to Exa.

All fixtures under tests/fixtures/exa/ are docs-derived
("_shape_source": "docs 2026-09-13") -- no live Exa call was made to build
this test module (the plan's Phase 0-6 no-live-calls posture).

Run from ~/Code via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from sertchi.providers.base import ProviderBlocked, ProviderOperationalError
from sertchi.providers.exa import ExaProvider
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, ProviderAuthError, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "exa"


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
    raise httpx.ConnectTimeout("mock exa timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(429, {"message": "rate limited"})


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(500, {"message": "server error"})


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    # Exa's "out of credits" signal is 402, per the plan's decision table --
    # operational, falls through, unlike 401 (the ONLY documented auth
    # code) which is a configuration error (see ExaAuthClassificationTest
    # below).
    return _json_response(402, {"message": "out of credits"})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return _json_response(200, MALFORMED)


def run(coro):
    return asyncio.run(coro)


class TestProviderExa(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> ExaProvider:
        return ExaProvider(api_key="exa_TESTKEY0000-9f2c")

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


class ExaAuthClassificationTest(unittest.TestCase):
    """A rejected key is a CONFIGURATION error, not an operational one (user
    decision, 2026-08-20, reused here). 402 ("out of credits") is the
    opposite: operational, falls through.

    Exa is the FIRST adapter in this tree whose _AUTH_STATUSES is a bare
    {401} sitting right next to keenable.py's {401, 403} -- deliberate and
    evidenced: Exa's documented /search status codes are
    400/401/402/429/500/503. There is NO 403 at all. This test pins the
    divergence in BOTH directions: 401 escapes, 403 does NOT escape (it
    falls through as operational instead) -- do not "harmonise" this with
    keenable.py's or serper.py's {401, 403} set.
    """

    def _call(self, handler):
        provider = ExaProvider(api_key="exa_TESTKEY0000-9f2c")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        return run(go())

    def test_401_raises_provider_auth_error(self) -> None:
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"message": "unauthorized"}))
        self.assertEqual(caught.exception.status_code, 401)

    def test_403_does_not_raise_provider_auth_error(self) -> None:
        """The deliberate divergence from serper.py's/keenable.py's
        {401, 403}: Exa has no documented 403 for /search at all, so a live
        403 is classified operational (ProviderOperationalError), NOT a
        configuration error."""
        with self.assertRaises(ProviderOperationalError) as caught:
            self._call(lambda request: _json_response(403, {"message": "forbidden"}))
        self.assertNotIsInstance(caught.exception, ProviderAuthError)

    def test_402_raises_provider_blocked_not_auth_error(self) -> None:
        with self.assertRaises(ProviderBlocked) as caught:
            self._call(lambda request: _json_response(402, {"message": "out of credits"}))
        self.assertNotIsInstance(caught.exception, ProviderAuthError)

    def test_message_never_leaks_the_key(self) -> None:
        key = "exa_TESTKEY0000-9f2c"
        with self.assertRaises(ProviderAuthError) as caught:
            self._call(lambda request: _json_response(401, {"message": key}))
        self.assertNotIn(key, str(caught.exception))


class ExaCostRuleTest(unittest.TestCase):
    """Micro-dollar formula: base(type) + 1,000 content + 1,000/extra
    result. Every expected figure below is a HARD-CODED INTEGER LITERAL,
    never computed, so a formula regression trips a literal mismatch
    instead of quietly recomputing the same wrong answer."""

    def test_default_is_8000(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.credits, 8_000)

    def test_numresults_25_is_23000(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, numResults=25)
        self.assertEqual(cost.credits, 23_000)

    def test_type_deep_is_13000(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, type="deep")
        self.assertEqual(cost.credits, 13_000)

    def test_type_deep_reasoning_is_16000(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, type="deep-reasoning")
        self.assertEqual(cost.credits, 16_000)

    def test_type_deep_lite_is_13000(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, type="deep-lite")
        self.assertEqual(cost.credits, 13_000)

    def test_unknown_type_bills_standard_base_8000(self) -> None:
        # Pinned deliberately so nobody "tightens" this into a raise -- an
        # unrecognized type string must still bill something sane, not
        # crash a caller mid-chain.
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB, type="some-future-tier")
        self.assertEqual(cost.credits, 8_000)

    def test_unit_is_usd_micro(self) -> None:
        provider = ExaProvider(api_key="x")
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.unit, "usd_micro")

    def test_estimate_cost_is_pure_and_network_free(self) -> None:
        provider = ExaProvider(api_key="x")
        provider.estimate_cost(SearchType.NEWS, numResults=50)  # must not raise


class ExaContentsBoundaryTest(unittest.TestCase):
    """I7: exactly one content type, always -- highlights, never text or
    summary. Asserted directly on the captured request body, both nested
    (inside 'contents') and at the top level."""

    def _capture(self, search_type: SearchType) -> dict:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _json_response(200, WEB_OK if search_type is SearchType.WEB else NEWS_OK)

        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", search_type, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_contents_highlights_always_true(self) -> None:
        body = self._capture(SearchType.WEB)
        self.assertIn("contents", body)
        self.assertTrue(body["contents"].get("highlights"))

    def test_contents_has_no_text_or_summary_key(self) -> None:
        body = self._capture(SearchType.WEB)
        self.assertNotIn("text", body["contents"])
        self.assertNotIn("summary", body["contents"])

    def test_top_level_body_has_no_text_or_summary_key(self) -> None:
        body = self._capture(SearchType.WEB)
        self.assertNotIn("text", body)
        self.assertNotIn("summary", body)


class ExaCategoryRequestTest(unittest.TestCase):
    """I8: no separate news endpoint -- NEWS sends category='news' on the
    same /search call; WEB sends no category key at all."""

    def test_news_request_body_carries_category_news(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _json_response(200, NEWS_OK)

        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("category"), "news")

    def test_web_request_body_has_no_category_key(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _json_response(200, WEB_OK)

        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertNotIn("category", captured[0])


class ExaDateConfidenceTest(unittest.TestCase):
    """publishedDate is a genuinely native field (unlike DDG's emulated
    recency), so an absence reads as NONE, never INFERRED -- the
    firecrawl.py precedent. Passed through byte-identical, no reparse."""

    def test_row_with_published_date_passes_through_unmodified(self) -> None:
        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        # web_ok.json row 1's publishedDate, byte-identical.
        self.assertEqual(first.published_date, "2026-09-10T14:03:00.000Z")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        # web_ok.json row 2's publishedDate is null.
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)

    def test_news_row_missing_published_date_key_is_none_confidence(self) -> None:
        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_news_handler)) as client:
                return await provider.search("q", SearchType.NEWS, client=client)

        response = run(go())
        self.assertEqual(len(response.results), 2)
        first, second = response.results
        self.assertEqual(first.published_date, "2026-09-10T14:03:00.000Z")
        self.assertEqual(first.date_confidence, DateConfidence.REPORTED)
        # news_ok.json row 2 has no publishedDate key at all.
        self.assertIsNone(second.published_date)
        self.assertEqual(second.date_confidence, DateConfidence.NONE)


class ExaScoreTest(unittest.TestCase):
    """provider_score is populated from Exa's own `score` field -- recorded,
    never filtered or reordered on (tavily.py precedent)."""

    def test_provider_score_populated_from_score(self) -> None:
        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        first, second = response.results
        self.assertEqual(first.provider_score, 0.83)
        self.assertEqual(second.provider_score, 0.71)


class ExaAuthHeaderTest(unittest.TestCase):
    """I6: the key goes in a Bearer header, never a URL."""

    def test_key_sent_as_bearer_header_never_in_url(self) -> None:
        key = "exa_TESTKEY0000-9f2c"
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _json_response(200, WEB_OK)

        provider = ExaProvider(api_key=key)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        run(go())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers.get("authorization"), f"Bearer {key}")
        self.assertNotIn(key, str(captured[0].url))


class ExaProviderMetadataTest(unittest.TestCase):
    """costDollars is surfaced for a future reconciliation pass, but the
    ledger (usage) never billed from it (I4)."""

    def test_cost_dollars_reported_surfaced_while_usage_stays_formula_based(self) -> None:
        provider = ExaProvider(api_key="x")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("q", SearchType.WEB, client=client)

        response = run(go())
        self.assertEqual(
            response.provider_metadata["cost_dollars_reported"],
            WEB_OK["costDollars"],
        )
        self.assertIsNone(response.provider_metadata["reported_remaining"])
        # Default WEB call, numResults=10 -- 8,000 micro-dollars from the
        # formula, never from costDollars above.
        self.assertEqual(response.usage.credits, 8_000)


class ExaRegistryTest(unittest.TestCase):
    def test_registered_under_exa(self) -> None:
        from sertchi.providers.base import REGISTRY
        from sertchi.types import Provider

        self.assertIn(Provider.EXA, REGISTRY)
        self.assertIsInstance(REGISTRY[Provider.EXA], ExaProvider)


if __name__ == "__main__":
    unittest.main()
