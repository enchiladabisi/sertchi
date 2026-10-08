"""Phase 2: providers/ddg.py -- the keyless DDG adapter, adapted from
tributary-cpp-v2/ddg.py (I10: that file is read-only, never edited/imported).
Subclasses ProviderConformanceTests (Phase 1a) so DDG is held to the same
contract every other adapter will be (SKETCH.md §11).

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import httpx

from sertchi.providers.ddg import DDGProvider, looks_like_anomaly, parse_results
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import DateConfidence, SearchType

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "ddg"


def _load(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


LITE_OK = _load("lite_ok.html")
HTML_OK = _load("html_ok.html")
ANOMALY = _load("anomaly.html")
EMPTY = _load("empty.html")


def _html_response(status: int, body: str) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"})


def _success_handler(request: httpx.Request) -> httpx.Response:
    return _html_response(200, LITE_OK)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return _html_response(200, EMPTY)


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("mock ddg timeout")


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    return _html_response(429, "<html>rate limited</html>")


def _unavailable_handler(request: httpx.Request) -> httpx.Response:
    return _html_response(500, "<html>server error</html>")


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    return _html_response(200, ANOMALY)


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    # A well-formed HTTP 200 whose body isn't the HTML page the adapter
    # expects -- DDG has no JSON layer to fail-to-parse the way a REST
    # provider does, so an unexpected content-type stands in for "the
    # response could not be parsed into the shape the adapter expects".
    return httpx.Response(200, json={"unexpected": "shape"})


def run(coro):
    return asyncio.run(coro)


class TestProviderDDG(ProviderConformanceTests, unittest.TestCase):
    def make_provider(self) -> DDGProvider:
        return DDGProvider()

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


class DDGParsingTest(unittest.TestCase):
    """Direct checks on the ported parsers/anomaly-detection, beyond what
    the generic conformance suite exercises."""

    def test_lite_surface_unwraps_redirects_and_drops_sponsored(self) -> None:
        results, anomaly = parse_results(LITE_OK, surface="lite", limit=5)
        self.assertFalse(anomaly)
        self.assertEqual(len(results), 2)
        self.assertTrue(all("y.js" not in r["url"] for r in results))
        self.assertTrue(all(r["url"].startswith("https://") for r in results))

    def test_html_surface_drops_ad_rows(self) -> None:
        results, anomaly = parse_results(HTML_OK, surface="html", limit=5)
        self.assertFalse(anomaly)
        self.assertEqual(len(results), 2)
        self.assertTrue(all("ads.example" not in r["url"] for r in results))

    def test_anomaly_page_is_a_clean_block_not_data(self) -> None:
        self.assertTrue(looks_like_anomaly(ANOMALY))
        results, anomaly = parse_results(ANOMALY, surface="lite", limit=5)
        self.assertTrue(anomaly)
        self.assertEqual(results, [])

    def test_empty_fixture_has_no_anomaly_markers(self) -> None:
        # Guards against the empty fixture accidentally looking blocked.
        self.assertFalse(looks_like_anomaly(EMPTY))


class DDGNewsEmulationTest(unittest.TestCase):
    """I8 -- DDG's `news` is an emulation, never a real dated endpoint."""

    def test_news_rows_are_inferred_confidence_and_undated(self) -> None:
        provider = DDGProvider()

        async def go() -> object:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("query", SearchType.NEWS, client=client)

        response = run(go())
        self.assertTrue(response.results, "success fixture produced zero rows")
        for row in response.results:
            self.assertEqual(row.date_confidence, DateConfidence.INFERRED)
            self.assertIsNone(row.published_date)

    def test_web_rows_are_undated_with_no_confidence(self) -> None:
        provider = DDGProvider()

        async def go() -> object:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_success_handler)) as client:
                return await provider.search("query", SearchType.WEB, client=client)

        response = run(go())
        self.assertTrue(response.results)
        for row in response.results:
            self.assertEqual(row.date_confidence, DateConfidence.NONE)
            self.assertIsNone(row.published_date)


class DDGCostTest(unittest.TestCase):
    def test_estimate_cost_is_zero_and_pure(self) -> None:
        provider = DDGProvider()
        cost = provider.estimate_cost(SearchType.WEB)
        self.assertEqual(cost.credits, 0)
        self.assertEqual(cost.unit, "none")
        self.assertEqual(cost.basis, "keyless")


if __name__ == "__main__":
    unittest.main()
