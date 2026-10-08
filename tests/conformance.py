"""``ProviderConformanceTests`` — SKETCH.md §11's contract, encoded as a
runnable, parametrized suite (fable-prompt-build-v1.md §5 Phase 1a).

This is a plain **mixin**, not a ``unittest.TestCase`` — the stdlib-idiomatic
way to share a battery of test methods across several concrete TestCases
without ``unittest discover`` trying to run the mixin standalone (it isn't a
TestCase subclass at all, so discovery's module scan never picks it up on
its own). A per-adapter test module parametrizes it by multiple inheritance:

    class TestProviderXxx(ProviderConformanceTests, unittest.TestCase):
        def make_provider(self) -> SearchProvider: ...
        def fixtures(self) -> ConformanceFixtures: ...

``make_provider()`` returns a fresh adapter instance (no client bound — the
real ``SearchProvider.search()`` signature takes ``client`` as a per-call
injection seam, Phase 2's ``providers/base.py``). ``fixtures()`` returns a
``ConformanceFixtures`` of ``httpx.MockTransport``-style handlers
(``handler(request: httpx.Request) -> httpx.Response``, the house pattern —
``tributary-cpp-v2/tests/test_ddg.py:109-115``) covering a success case, an
empty-results case, and one case per operational-failure kind. Every check
below runs entirely off these mocked handlers — no network, ever (I9).

The 10 checks below are numbered to match SKETCH.md §11 / the build plan's
Phase 1a list; each is its own ``test_*`` method so a violation is
attributable to the exact check it broke, not a single monolithic assertion.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Callable

import httpx

from sertchi.dedupe import canonicalize
from sertchi.providers.base import ProviderOperationalError
from sertchi.types import (
    CreditCost,
    InvalidRequest,
    ProviderResponse,
    ResultItem,
    SearchResult,
    SearchType,
)


def run(coro):
    """House convention (tributary-cpp-v2/tests/test_ddg.py) for driving one
    coroutine to completion inside a synchronous unittest method."""
    return asyncio.run(coro)


@dataclass(frozen=True)
class ConformanceFixtures:
    """Per-adapter HTTP fixtures the suite drives through
    ``httpx.MockTransport``. Each field is a handler with the house
    signature ``handler(request: httpx.Request) -> httpx.Response``
    (``tributary-cpp-v2/tests/test_ddg.py:109-115``); ``timeout_handler`` is
    the one exception — it *raises* an ``httpx.TimeoutException`` instead of
    returning, which is how a MockTransport simulates a transport-level
    failure rather than an HTTP response.

    - ``success_handler`` — a normal response with >=2 rows, so the
      1-based-dense rank check (#4) can actually distinguish 0-based from
      1-based.
    - ``empty_handler`` — a well-formed response with zero rows.
    - ``timeout_handler`` / ``rate_limited_handler`` (HTTP 429) /
      ``unavailable_handler`` (HTTP 5xx) / ``blocked_handler`` (provider's
      own block/challenge signal) / ``malformed_handler`` (unparseable body)
      — the five operational-failure kinds check #7 asserts are classified,
      not raised raw.
    """

    success_handler: Callable[[httpx.Request], httpx.Response]
    empty_handler: Callable[[httpx.Request], httpx.Response]
    timeout_handler: Callable[[httpx.Request], httpx.Response]
    rate_limited_handler: Callable[[httpx.Request], httpx.Response]
    unavailable_handler: Callable[[httpx.Request], httpx.Response]
    blocked_handler: Callable[[httpx.Request], httpx.Response]
    malformed_handler: Callable[[httpx.Request], httpx.Response]


class ProviderConformanceTests:
    """Mixin encoding SKETCH.md §11's contract. NOT a unittest.TestCase —
    see module docstring. A concrete subclass must also inherit
    unittest.TestCase and implement make_provider()/fixtures()."""

    QUERY = "sertchi conformance probe"

    # -- hooks a concrete subclass must implement ---------------------------

    def make_provider(self):
        raise NotImplementedError("subclass must implement make_provider()")

    def fixtures(self) -> ConformanceFixtures:
        raise NotImplementedError("subclass must implement fixtures()")

    # -- shared plumbing ------------------------------------------------

    async def _search(self, provider, search_type, handler, **opts):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider.search(self.QUERY, search_type, client=client, **opts)

    def _run_success(self):
        provider = self.make_provider()
        response = run(self._search(provider, SearchType.WEB, self.fixtures().success_handler))
        return provider, response

    def _assert_operational(self, handler) -> None:
        provider = self.make_provider()
        with self.assertRaises(ProviderOperationalError):
            run(self._search(provider, SearchType.WEB, handler))

    # -- #1 a provider returns ProviderResponse, never SearchResult (I2) ---

    def test_returns_provider_response_not_search_result(self) -> None:
        _, response = self._run_success()
        self.assertIsInstance(response, ProviderResponse)
        self.assertNotIsInstance(response, SearchResult)

    # -- #2 rows are ResultItems, non-empty title, provider == own identity

    def test_rows_are_result_items_with_own_identity(self) -> None:
        provider, response = self._run_success()
        self.assertTrue(response.results, "success fixture produced zero rows")
        for row in response.results:
            self.assertIsInstance(row, ResultItem)
            self.assertTrue(row.title, "row.title must be non-empty")
            self.assertEqual(row.provider, provider.name)

    # -- #3 url == canonicalize(url) — already canonical at the boundary ---

    def test_urls_are_already_canonical(self) -> None:
        _, response = self._run_success()
        for row in response.results:
            self.assertEqual(
                row.url, canonicalize(row.url),
                f"row.url {row.url!r} is not already canonical",
            )

    # -- #4 provider_rank is 1-based and dense: [1..n], no gaps/repeats ----

    def test_provider_rank_is_1_based_dense(self) -> None:
        _, response = self._run_success()
        ranks = sorted(row.provider_rank for row in response.results)
        self.assertEqual(ranks, list(range(1, len(response.results) + 1)))

    # -- #5 registrable_domain and domain populated for every row ----------

    def test_domain_fields_populated(self) -> None:
        _, response = self._run_success()
        for row in response.results:
            self.assertTrue(row.registrable_domain, "row.registrable_domain is empty")
            self.assertTrue(row.domain, "row.domain is empty")

    # -- #6 a zero-result fixture yields results == [] and does NOT raise --

    def test_empty_fixture_yields_empty_list_not_raise(self) -> None:
        provider = self.make_provider()
        response = run(self._search(provider, SearchType.WEB, self.fixtures().empty_handler))
        self.assertEqual(response.results, [])

    # -- #7 each mocked operational failure -> ProviderOperationalError ----

    def test_timeout_is_classified_operational(self) -> None:
        self._assert_operational(self.fixtures().timeout_handler)

    def test_rate_limit_is_classified_operational(self) -> None:
        self._assert_operational(self.fixtures().rate_limited_handler)

    def test_unavailable_is_classified_operational(self) -> None:
        self._assert_operational(self.fixtures().unavailable_handler)

    def test_blocked_is_classified_operational(self) -> None:
        self._assert_operational(self.fixtures().blocked_handler)

    def test_malformed_is_classified_operational(self) -> None:
        self._assert_operational(self.fixtures().malformed_handler)

    # -- #8 a caller error (bad search_type) DOES raise InvalidRequest -----

    def test_invalid_search_type_raises_invalid_request(self) -> None:
        provider = self.make_provider()
        with self.assertRaises(InvalidRequest):
            run(provider.search(self.QUERY, "not-a-real-search-type"))

    # -- #9 usage is a CreditCost; a keyless provider reports credits == 0 -

    def test_usage_is_credit_cost(self) -> None:
        provider, response = self._run_success()
        self.assertIsInstance(response.usage, CreditCost)
        if not provider.requires_key:
            self.assertEqual(response.usage.credits, 0)

    # -- #10 a news call on a non-news provider never issues a request (I8)

    def test_news_call_on_non_news_provider_issues_no_request(self) -> None:
        provider = self.make_provider()
        if SearchType.NEWS in provider.supports:
            self.skipTest("provider supports news; the I8 zero-request check does not apply")
        calls: list[httpx.Request] = []
        base_handler = self.fixtures().success_handler

        def counting_handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return base_handler(request)

        try:
            run(self._search(provider, SearchType.NEWS, counting_handler))
        except Exception:
            # The contract only requires zero requests; what (if anything)
            # the adapter raises/returns for an unsupported type on a direct
            # call is not this check's concern.
            pass
        self.assertEqual(len(calls), 0, "adapter issued a request for an unsupported search_type")
