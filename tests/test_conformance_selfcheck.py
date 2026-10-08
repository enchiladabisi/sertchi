"""Phase 1a's negative control (fable-prompt-build-v1.md §5 Phase 1a) — the
whole point of this phase: proves ``ProviderConformanceTests`` can actually
FAIL a non-conforming adapter, not just pass a conforming one. A suite that
only ever says OK asserts nothing.

Two in-file fake adapters:

- ``GoodFake`` — conforms to every check; the suite passes against it.
- ``BrokenFake`` — deliberately violates exactly three checks:
    1. ``provider_rank`` is 0-based, not 1-based dense       -> check #4
    2. rows keep a ``utm_source`` param and a ``#fragment``   -> check #3
       (``url != canonicalize(url)``)
    3. raises instead of returning ``results == []`` on empty -> check #6

The two parametrized TestCase classes that pair each fake with
``ProviderConformanceTests`` are defined LOCALLY, inside the test methods
below, rather than at module scope. This is deliberate: a class defined at
module scope in a ``test_*.py`` file is exactly what ``unittest discover``'s
module scan picks up and runs on its own — and ``BrokenFake``'s suite is
*designed* to fail. If discovery ran it directly, the project's overall
"OK" verification (Phase 0's command) would break. Defining the classes
inside a function keeps them out of ``dir(module)`` entirely, so discovery
never sees them; this file's own two ``test_*`` methods (in a plain,
module-level ``unittest.TestCase``) are the only things discovery runs, and
they drive each parametrized suite explicitly through a real
``unittest.TestResult``.

Run from ~/Documents/CODE via:
    python3 -m unittest sertchi.tests.test_conformance_selfcheck -v
"""

from __future__ import annotations

import dataclasses
import unittest

import httpx

from sertchi.dedupe import canonicalize
from sertchi.providers.base import (
    ProviderBlocked,
    ProviderMalformedResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from sertchi.tests.conformance import ConformanceFixtures, ProviderConformanceTests
from sertchi.types import (
    CreditCost,
    DateConfidence,
    InvalidRequest,
    ProviderResponse,
    ResultItem,
    SearchType,
)

# ---------------------------------------------------------------------------
# Fixture bodies. GoodFake's URLs are already canonical (no tracking params,
# no fragment); BrokenFake's carry a utm_source + fragment on purpose so its
# pass-through-unchanged behavior (violation #2) is actually exercised.
# ---------------------------------------------------------------------------

_GOOD_SUCCESS_BODY = {
    "results": [
        {"title": "Alpha", "url": "https://example.com/a",
         "domain": "example.com", "registrable_domain": "example.com"},
        {"title": "Bravo", "url": "https://example.org/b",
         "domain": "example.org", "registrable_domain": "example.org"},
    ]
}
_BROKEN_SUCCESS_BODY = {
    "results": [
        {"title": "Alpha", "url": "https://example.com/a?utm_source=x#frag",
         "domain": "example.com", "registrable_domain": "example.com"},
        {"title": "Bravo", "url": "https://example.org/b?utm_source=x#frag",
         "domain": "example.org", "registrable_domain": "example.org"},
    ]
}
_EMPTY_BODY = {"results": []}


def _json_handler(body: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)
    return handler


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("mock timeout")


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"not-json{{{")


def _blocked_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"blocked": True, "results": []})


def _good_fixtures() -> ConformanceFixtures:
    return ConformanceFixtures(
        success_handler=_json_handler(_GOOD_SUCCESS_BODY),
        empty_handler=_json_handler(_EMPTY_BODY),
        timeout_handler=_timeout_handler,
        rate_limited_handler=_json_handler({"error": "rate limited"}, status=429),
        unavailable_handler=_json_handler({"error": "server error"}, status=500),
        blocked_handler=_blocked_handler,
        malformed_handler=_malformed_handler,
    )


def _broken_fixtures() -> ConformanceFixtures:
    return dataclasses.replace(_good_fixtures(), success_handler=_json_handler(_BROKEN_SUCCESS_BODY))


# ---------------------------------------------------------------------------
# Shared request/error-mapping plumbing, used by both fakes identically —
# only the parts named VIOLATION below differ between them.
# ---------------------------------------------------------------------------


def _validate_search_type(search_type) -> SearchType:
    try:
        return SearchType(search_type)
    except ValueError as exc:
        raise InvalidRequest(f"invalid search_type: {search_type!r}") from exc


async def _fetch_json(client: httpx.AsyncClient | None, url: str, query: str) -> dict:
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        try:
            resp = await client.get(url, params={"q": query})
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc
        if resp.status_code == 429:
            raise ProviderRateLimited(f"http {resp.status_code}")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"http {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderMalformedResponse("invalid json body") from exc
        if payload.get("blocked"):
            raise ProviderBlocked("anomaly page")
        return payload
    finally:
        if owns_client:
            await client.aclose()


class GoodFake:
    """Fully-conformant fake adapter — proves the suite doesn't
    false-positive-fail a well-behaved provider."""

    name = "goodfake"
    supports = frozenset({SearchType.WEB})
    requires_key = False

    async def search(self, query, search_type, *, client=None, **opts):
        search_type = _validate_search_type(search_type)
        if search_type not in self.supports:
            raise InvalidRequest(f"{self.name} does not support {search_type.value}")
        payload = await _fetch_json(client, "https://goodfake.example/search", query)
        rows = [
            ResultItem(
                title=item["title"],
                url=canonicalize(item["url"]),
                snippet=item.get("snippet", ""),
                domain=item["domain"],
                registrable_domain=item["registrable_domain"],
                published_date=item.get("published_date"),
                date_confidence=DateConfidence.NONE,
                provider=self.name,
                provider_rank=rank,
                provider_score=None,
                thumbnail_url=None,
            )
            for rank, item in enumerate(payload.get("results", []), start=1)
        ]
        return ProviderResponse(
            provider=self.name, results=rows, provider_metadata={},
            usage=CreditCost(credits=0, unit="none", basis="fake"),
        )


class BrokenFake:
    """Deliberately non-conformant fake adapter — see module docstring for
    the three violations, each annotated VIOLATION below at the exact line
    responsible."""

    name = "brokenfake"
    supports = frozenset({SearchType.WEB})
    requires_key = False

    async def search(self, query, search_type, *, client=None, **opts):
        search_type = _validate_search_type(search_type)
        if search_type not in self.supports:
            raise InvalidRequest(f"{self.name} does not support {search_type.value}")
        payload = await _fetch_json(client, "https://brokenfake.example/search", query)
        items = payload.get("results", [])
        if not items:
            # VIOLATION (intentional, targets check #6): a conformant
            # adapter must return results=[], never raise, on an empty set.
            raise RuntimeError("BrokenFake refuses to report zero results")
        rows = [
            ResultItem(
                title=item["title"],
                # VIOLATION (intentional, targets check #3): passed through
                # unchanged instead of canonicalize(item["url"]).
                url=item["url"],
                snippet=item.get("snippet", ""),
                domain=item["domain"],
                registrable_domain=item["registrable_domain"],
                published_date=item.get("published_date"),
                date_confidence=DateConfidence.NONE,
                provider=self.name,
                provider_rank=rank,
                provider_score=None,
                thumbnail_url=None,
            )
            # VIOLATION (intentional, targets check #4): enumerate(items)
            # starts at 0, not 1 — not 1-based dense.
            for rank, item in enumerate(items)
        ]
        return ProviderResponse(
            provider=self.name, results=rows, provider_metadata={},
            usage=CreditCost(credits=0, unit="none", basis="fake"),
        )


def _run_suite(case_class) -> unittest.TestResult:
    suite = unittest.TestLoader().loadTestsFromTestCase(case_class)
    result = unittest.TestResult()
    suite.run(result)
    return result


class ConformanceSelfCheck(unittest.TestCase):
    def test_good_fake_passes_the_conformance_suite(self) -> None:
        class _GoodConformance(ProviderConformanceTests, unittest.TestCase):
            def make_provider(self):
                return GoodFake()

            def fixtures(self):
                return _good_fixtures()

        result = _run_suite(_GoodConformance)
        self.assertTrue(
            result.wasSuccessful(),
            "GoodFake should pass every conformance check; "
            f"failures={result.failures} errors={result.errors}",
        )

    def test_broken_fake_fails_at_least_three_checks(self) -> None:
        class _BrokenConformance(ProviderConformanceTests, unittest.TestCase):
            def make_provider(self):
                return BrokenFake()

            def fixtures(self):
                return _broken_fixtures()

        result = _run_suite(_BrokenConformance)
        total = len(result.failures) + len(result.errors)
        caught = [t.id().rsplit(".", 1)[-1] for t, _ in result.failures] + \
                 [t.id().rsplit(".", 1)[-1] for t, _ in result.errors]
        self.assertGreaterEqual(
            total, 3,
            f"expected >=3 contract violations caught, got {total}; caught={caught}",
        )
