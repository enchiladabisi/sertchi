"""Phase 7: sertchi/__init__.py -- the orchestrator, the only place a
SearchResult is assembled (fable-prompt-build-v1.md §5 Phase 7).

The six cases the build plan names by number are the first six test methods,
in order. Everything after them guards the two things this file exists to get
right and that a future edit could quietly break: I3's fail-open narrowing in
BOTH directions (operational -> fall through, everything else -> raise), and
Q8's status table (a fallback success is OK, never PARTIAL).

CRITICAL SANDBOXING: every test repoints keys.CONFIG_DIR/KEYS_PATH and
usage.CONFIG_DIR/USAGE_PATH/LOCK_PATH at a tempfile.TemporaryDirectory and
restores them in tearDown. No test in this module may read the user's real
keys or write the real ~/.config/sertchi/ -- an orchestrator test that
touched the real ledger would corrupt the very credit accounting Phase 6
exists to keep honest.

I9: no network. Every request is served by one httpx.MockTransport handler
that routes by host and logs what it was asked for, so "zero requests
issued" is an assertion, not a hope.

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

import httpx

import sertchi
from sertchi import keys, usage
from sertchi.providers.base import REGISTRY, ProviderRateLimited
from sertchi.types import (
    AttemptOutcome,
    CreditCost,
    InvalidRequest,
    Provider,
    ProviderAuthError,
    ProviderResponse,
    SearchStatus,
    SearchType,
)

# --------------------------------------------------------------------------
# Fixtures -- module-level constants, the house convention
# (tributary-cpp-v2/tests/test_ddg.py:20-40, restated in §2.6)
# --------------------------------------------------------------------------

TAVILY_TWO_ROWS = {
    "query": "orchestrator probe",
    "results": [
        {
            "title": "Tavily Row One",
            "url": "https://www.example.org/one?utm_source=test&id=7",
            "content": "First Tavily row.",
            "score": 0.35,
        },
        {
            "title": "Tavily Row Two",
            "url": "https://example.com/two",
            "content": "Second Tavily row.",
            "score": 0.81,
        },
    ],
}

#: Case 5's collision pair: Tavily reports the bare https URL (with a tracking
#: param canonicalize() strips), DDG reports the www. form of the same page.
#: dedupe_key() forces https and strips a leading www., so the two collapse.
TAVILY_SHARED_URL = {
    "query": "orchestrator probe",
    "results": [
        {
            "title": "Shared Story",
            "url": "https://example.com/story?utm_source=newsletter",
            "content": "The same page both providers found.",
            "score": 0.5,
        }
    ],
}

FIRECRAWL_EMPTY = {"success": True, "data": {"web": []}, "creditsUsed": 0}

FIRECRAWL_TWO_ROWS = {
    "success": True,
    "data": {
        "web": [
            {
                "title": "Firecrawl Row One",
                "url": "https://example.net/fc-one",
                "description": "First Firecrawl row.",
            }
        ]
    },
    "creditsUsed": 2,
}

SERPER_TWO_ROWS = {
    "searchParameters": {"q": "orchestrator probe", "type": "search"},
    "organic": [
        {
            "title": "Serper Row One",
            "link": "https://www.example.org/one?utm_source=test&id=7",
            "snippet": "First Serper row.",
            "position": 1,
        },
        {
            "title": "Serper Row Two",
            "link": "https://example.com/two",
            "snippet": "Second Serper row.",
            "position": 2,
        },
    ],
}

SERPER_EMPTY = {"searchParameters": {"q": "x", "type": "search"}, "organic": []}

DDG_SHARED_URL_HTML = """<html><body><table>
<tr><td>1.&nbsp;</td><td><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.com%2Fstory"
 class="result-link">Shared Story (DDG)</a></td></tr>
<tr><td class="result-snippet">The same page, found by DDG too.</td></tr>
</table></body></html>"""

DDG_EMPTY_HTML = "<html><body><table></table></body></html>"


# --------------------------------------------------------------------------
# One MockTransport handler, routing by host and logging every request
# --------------------------------------------------------------------------


def _json_responder(status: int, payload: dict):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return responder


def _html_responder(status: int, body: str):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, text=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    return responder


def _error_responder(status: int):
    return _json_responder(status, {"detail": f"mock http {status}"})


TAVILY_HOST = "api.tavily.com"
FIRECRAWL_HOST = "api.firecrawl.dev"
SERPER_HOST = "google.serper.dev"
DDG_HOST = "duckduckgo.com"  # matches both lite. and html. subdomains


class _Router:
    """Routes by host, records every host it was asked for.

    A host with no rule is an AssertionError rather than a default response:
    a provider this test did not expect to be called must fail the test
    loudly, not be quietly served.
    """

    def __init__(self, **routes) -> None:
        self.routes = routes
        self.log: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        self.log.append(host)
        for suffix, responder in self.routes.items():
            if host.endswith(suffix):
                return responder(request)
        raise AssertionError(f"unexpected request to {host!r}")

    @property
    def hosts_called(self) -> set[str]:
        return set(self.log)

    def called(self, suffix: str) -> bool:
        return any(host.endswith(suffix) for host in self.log)


# --------------------------------------------------------------------------
# Fake adapters used by the I3/I8 cases
# --------------------------------------------------------------------------


class _WebOnlyFake:
    """Case 4's hypothetical provider: it supports `web` and nothing else.

    Registered under Provider.TAVILY so it occupies a real slot in the
    chain. Its search() is a tripwire -- if the orchestrator ever invokes it
    for a news call, the test fails on the spot rather than on a downstream
    assertion.
    """

    name = "tavily"
    supports = frozenset({SearchType.WEB})
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        return CreditCost(credits=0, unit="none", basis="fake")

    async def search(self, query, search_type, *, client=None, **opts):
        self.calls += 1
        raise AssertionError(
            "I8 violated: a web-only provider was invoked for a news call"
        )


class _BuggyFake:
    """Raises a NON-operational exception -- I3 says this must escape."""

    name = "tavily"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = False

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        return CreditCost(credits=1, unit="api_credit", basis="fake")

    async def search(self, query, search_type, *, client=None, **opts):
        raise KeyError("internal schema violation")


class _OperationalFake:
    """Raises an operational error -- I3 says this must be swallowed."""

    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = False

    def __init__(self, name: str = "tavily") -> None:
        self.name = name

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        return CreditCost(credits=1, unit="api_credit", basis="fake")

    async def search(self, query, search_type, *, client=None, **opts):
        raise ProviderRateLimited("http 429")


class _EmptyFake:
    """Answers 200-with-nothing, the outcome that is NOT an error."""

    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = False

    def __init__(self, name: str) -> None:
        self.name = name

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        return CreditCost(credits=1, unit="api_credit", basis="fake")

    async def search(self, query, search_type, *, client=None, **opts):
        return ProviderResponse(
            provider=self.name,
            results=[],
            provider_metadata={"reported_remaining": None},
            usage=CreditCost(credits=1, unit="api_credit", basis="fake"),
        )


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Sandbox base -- mirrors tests/test_usage.py's _TempUsageDirTestCase
# --------------------------------------------------------------------------


class _SandboxedOrchestratorTest(unittest.TestCase):
    """Repoints BOTH config-backed modules at a temp dir, and restores the
    REGISTRY, for every test. keys and usage move together: the orchestrator
    reads keys during gate 2 and the ledger during gate 4, so sandboxing
    only one of them would still touch the user's real ~/.config/sertchi/."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        sandbox = Path(self._tmpdir.name) / "sertchi"

        self._real_keys = (keys.CONFIG_DIR, keys.KEYS_PATH)
        keys.CONFIG_DIR = sandbox
        keys.KEYS_PATH = sandbox / "keys.json"

        self._real_usage = (usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH)
        usage.CONFIG_DIR = sandbox
        usage.USAGE_PATH = sandbox / "usage.json"
        usage.LOCK_PATH = sandbox / "usage.lock"

        self._real_registry = dict(REGISTRY)

        # search()'s try order is a weighted random draw (2026-09-26). These
        # tests are about fallback semantics, not the draw, so pin it to the
        # declaration order the candidates arrive in; OrchestratorDrawOrderTest
        # restores the real draw and tests it directly.
        self._real_draw_order = sertchi._draw_order
        sertchi._draw_order = list

    def tearDown(self) -> None:
        sertchi._draw_order = self._real_draw_order
        keys.CONFIG_DIR, keys.KEYS_PATH = self._real_keys
        usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH = self._real_usage
        REGISTRY.clear()
        REGISTRY.update(self._real_registry)
        self._tmpdir.cleanup()

    # -- helpers ----------------------------------------------------------

    def configure_keys(self, *providers: Provider) -> None:
        for provider in providers:
            keys.set_key(provider.value, f"test-key-for-{provider.value}")

    def client_for(self, router: _Router) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(router))

    def search(self, router: _Router, *args, **kwargs):
        async def go():
            async with self.client_for(router) as client:
                return await sertchi.search(*args, client=client, **kwargs)

        return run(go())

    def multi_search(self, router: _Router, *args, **kwargs):
        async def go():
            async with self.client_for(router) as client:
                return await sertchi.multi_search(*args, client=client, **kwargs)

        return run(go())


# --------------------------------------------------------------------------
# The six cases the build plan names, in order
# --------------------------------------------------------------------------


class OrchestratorNamedCasesTest(_SandboxedOrchestratorTest):
    def test_case1_tavily_429_firecrawl_empty_serper_rows_is_ok_not_partial(self) -> None:
        """Case 1 -- Tavily 429 -> Firecrawl 200-but-empty -> Serper 200 rows.

        Q8: a fallback success is OK. PARTIAL is never assigned, no matter
        how many attempts it took to get here.
        """
        self.configure_keys(Provider.TAVILY, Provider.FIRECRAWL, Provider.SERPER)
        router = _Router(
            **{
                TAVILY_HOST: _error_responder(429),
                FIRECRAWL_HOST: _json_responder(200, FIRECRAWL_EMPTY),
                SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS),
            }
        )

        result = self.search(router, "orchestrator probe", SearchType.WEB)

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertNotEqual(result.status, SearchStatus.PARTIAL)
        self.assertEqual(result.provider_used, "serper")
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(
            [a.outcome for a in result.attempts],
            [AttemptOutcome.ERROR, AttemptOutcome.EMPTY, AttemptOutcome.SUCCESS],
        )
        self.assertEqual(
            [a.provider for a in result.attempts], ["tavily", "firecrawl", "serper"]
        )
        self.assertEqual(len(result.results), 2)
        self.assertTrue(all(row.provider == "serper" for row in result.results))
        # The chain stopped at Serper: DDG was eligible but never called.
        self.assertFalse(router.called(DDG_HOST))

    def test_case2_all_four_fail_is_unavailable_with_no_exception(self) -> None:
        """Case 2 -- every provider fails operationally. The caller's turn
        survives: no exception, empty results, status UNAVAILABLE."""
        self.configure_keys(Provider.TAVILY, Provider.FIRECRAWL, Provider.SERPER)
        router = _Router(
            **{
                TAVILY_HOST: _error_responder(500),
                FIRECRAWL_HOST: _error_responder(500),
                SERPER_HOST: _error_responder(500),
                DDG_HOST: _error_responder(500),
            }
        )

        result = self.search(router, "orchestrator probe", SearchType.WEB)

        self.assertEqual(result.status, SearchStatus.UNAVAILABLE)
        self.assertEqual(result.results, [])
        self.assertIsNone(result.provider_used)
        self.assertEqual(len(result.attempts), 4)
        self.assertTrue(
            all(a.outcome is AttemptOutcome.ERROR for a in result.attempts)
        )
        # Every one of the four was really tried over the wire.
        for host in (TAVILY_HOST, FIRECRAWL_HOST, SERPER_HOST, DDG_HOST):
            self.assertTrue(router.called(host), f"{host} was never attempted")

    def test_case3_bad_search_type_raises_invalid_request_with_zero_requests(self) -> None:
        """Case 3 -- I3's other direction. A bad SearchType is a CALLER
        error: it raises, and it raises before anything is contacted."""
        self.configure_keys(Provider.TAVILY, Provider.FIRECRAWL, Provider.SERPER)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS),
                FIRECRAWL_HOST: _json_responder(200, FIRECRAWL_TWO_ROWS),
                SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS),
                DDG_HOST: _html_responder(200, DDG_EMPTY_HTML),
            }
        )

        with self.assertRaises(InvalidRequest):
            self.search(router, "orchestrator probe", "nonsense")

        self.assertEqual(router.log, [], "a bad search_type issued a request")
        # multi_search validates at the same point, for the same reason.
        with self.assertRaises(InvalidRequest):
            self.multi_search(router, "orchestrator probe", "nonsense")
        self.assertEqual(router.log, [])

    def test_case4_news_call_never_invokes_a_web_only_provider(self) -> None:
        """Case 4 -- I8. A provider whose adapter does not implement `news`
        is not invoked for a news call; it is dropped at gate 3, before any
        request, and never silently served a `web` search instead."""
        fake = _WebOnlyFake()
        REGISTRY[Provider.TAVILY] = fake
        router = _Router()  # any request at all is an AssertionError

        result = self.search(
            router, "orchestrator probe", SearchType.NEWS, providers=[Provider.TAVILY]
        )

        self.assertEqual(fake.calls, 0)
        self.assertEqual(router.log, [])
        self.assertEqual(result.results, [])
        self.assertEqual(result.attempts, [])
        self.assertEqual(result.status, SearchStatus.UNAVAILABLE)
        self.assertIn("tavily skipped: does not support news", result.limitations)

    def test_case5_multi_search_dedupes_shared_url_to_tavily(self) -> None:
        """Case 5 -- the same URL from Tavily and DDG collapses to ONE row,
        attributed to Tavily: the higher-ranked provider wins the tiebreak,
        using Provider's declaration order and no second ranking."""
        self.configure_keys(Provider.TAVILY)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(200, TAVILY_SHARED_URL),
                DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML),
            }
        )

        result = self.multi_search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.DDG],
        )

        # Both really were fanned out to -- this is not a dedupe that only
        # "works" because one provider was never called.
        self.assertTrue(router.called(TAVILY_HOST))
        self.assertTrue(router.called(DDG_HOST))
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].provider, "tavily")
        self.assertEqual(result.results[0].url, "https://example.com/story")
        self.assertEqual(result.status, SearchStatus.OK)

    def test_case6_include_attempts_false_still_moves_usage_json(self) -> None:
        """Case 6 -- include_attempts=False trims the VIEW only. The data is
        still collected and still reported to the ledger."""
        self.configure_keys(Provider.TAVILY)
        router = _Router(**{TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS)})

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY],
            include_attempts=False,
        )

        self.assertIsNone(result.attempts)
        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(len(result.results), 2)

        # ... and the ledger moved anyway: one basic-depth Tavily credit.
        self.assertTrue(usage.USAGE_PATH.exists())
        ledger = json.loads(usage.USAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(ledger["providers"]["tavily"]["local_credits_used"], 1)
        self.assertEqual(usage.get_state("tavily").local_credits_used, 1)
        self.assertEqual(usage.get_state("tavily").reservations, [])


# --------------------------------------------------------------------------
# I3 -- fail-open narrowing, both directions
# --------------------------------------------------------------------------


class OrchestratorFailOpenTest(_SandboxedOrchestratorTest):
    def test_operational_error_falls_through_to_the_next_provider(self) -> None:
        REGISTRY[Provider.TAVILY] = _OperationalFake("tavily")  # occupies tavily's slot
        self.configure_keys(Provider.SERPER)
        router = _Router(**{SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS)})

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.SERPER],
        )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "serper")
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.ERROR)
        self.assertIn("ProviderRateLimited", result.attempts[0].error)

    def test_non_operational_exception_propagates_out_of_search(self) -> None:
        """A KeyError is a bug, not an outage. Swallowing it would hide a
        real defect behind the same surface an outage produces."""
        REGISTRY[Provider.TAVILY] = _BuggyFake()
        router = _Router()

        with self.assertRaises(KeyError):
            self.search(
                router, "orchestrator probe", SearchType.WEB, providers=[Provider.TAVILY]
            )

    def test_non_operational_exception_propagates_out_of_multi_search(self) -> None:
        """asyncio.gather(return_exceptions=True) captures EVERYTHING, so
        multi_search has to re-raise the non-operational ones itself. This is
        the test that fails if that re-raise loop is ever deleted."""
        REGISTRY[Provider.TAVILY] = _BuggyFake()
        self.configure_keys(Provider.SERPER)
        router = _Router(**{SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS)})

        with self.assertRaises(KeyError):
            self.multi_search(
                router,
                "orchestrator probe",
                SearchType.WEB,
                providers=[Provider.TAVILY, Provider.SERPER],
            )

    def test_operational_error_in_multi_search_is_still_swallowed(self) -> None:
        REGISTRY[Provider.TAVILY] = _OperationalFake("tavily")
        self.configure_keys(Provider.SERPER)
        router = _Router(**{SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS)})

        result = self.multi_search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.SERPER],
        )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "serper")
        self.assertEqual(len(result.attempts), 2)

    def test_blank_query_raises_before_any_request(self) -> None:
        self.configure_keys(Provider.TAVILY)
        router = _Router(**{TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS)})
        with self.assertRaises(InvalidRequest):
            self.search(router, "   ", SearchType.WEB)
        self.assertEqual(router.log, [])

    def test_unknown_provider_name_raises_before_any_request(self) -> None:
        router = _Router()
        with self.assertRaises(InvalidRequest):
            self.search(router, "orchestrator probe", SearchType.WEB, providers=["bing"])
        self.assertEqual(router.log, [])


# --------------------------------------------------------------------------
# Q8 -- the status table, and PARTIAL's permanent absence
# --------------------------------------------------------------------------


class OrchestratorStatusTest(_SandboxedOrchestratorTest):
    def test_all_empty_attempts_is_empty_not_unavailable(self) -> None:
        REGISTRY[Provider.TAVILY] = _EmptyFake("tavily")
        REGISTRY[Provider.SERPER] = _EmptyFake("serper")
        router = _Router()

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.SERPER],
        )

        self.assertEqual(result.status, SearchStatus.EMPTY)
        self.assertEqual(
            [a.outcome for a in result.attempts],
            [AttemptOutcome.EMPTY, AttemptOutcome.EMPTY],
        )
        self.assertIsNone(result.provider_used)

    def test_one_empty_one_error_with_no_results_is_unavailable(self) -> None:
        REGISTRY[Provider.TAVILY] = _OperationalFake("tavily")
        REGISTRY[Provider.SERPER] = _EmptyFake("serper")
        router = _Router()

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.SERPER],
        )

        self.assertEqual(result.status, SearchStatus.UNAVAILABLE)

    def test_partial_is_never_assigned_anywhere_in_the_orchestrator(self) -> None:
        """The static half of the Q8 guarantee: the string PARTIAL does not
        appear in __init__.py's code at all, so no future edit can reach it
        by accident without deleting this test first."""
        source = Path(sertchi.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SearchStatus.PARTIAL", source)
        # Every remaining mention of PARTIAL in the module is prose inside the
        # module docstring, written as ``PARTIAL``. Strip those and no bare
        # PARTIAL token may survive anywhere in the file.
        self.assertNotIn("PARTIAL", source.replace("``PARTIAL``", ""))

    def test_no_bare_except_exception_around_the_chain(self) -> None:
        """I3's structural guard: the only exception type this module catches
        is ProviderOperationalError."""
        source = Path(sertchi.__file__).read_text(encoding="utf-8")
        # Only CODE lines are examined: the module docstring says the words
        # "except Exception" while promising not to contain one, and a raw
        # substring search over the whole file would match its own prose.
        caught = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("except ")
        ]
        self.assertTrue(caught, "no except clause found at all -- test is stale")
        # Three narrow forms are allowed, and nothing else:
        #   ProviderOperationalError -- I3's closed set, the only thing the
        #     chain is permitted to swallow;
        #   ProviderAuthError -- a rejected key, caught ONLY to settle the
        #     ledger (zero charge, breaker untouched) and then RE-RAISED as
        #     the configuration error it is;
        #   ValueError -- the enum-coercion catch in _resolve_search_type /
        #     _resolve_providers, which RE-RAISES as InvalidRequest rather
        #     than swallowing anything.
        allowed = {
            "except ProviderOperationalError as exc:",
            "except ProviderAuthError:",
            "except ValueError as exc:",
        }
        for line in caught:
            self.assertIn(line, allowed, f"unexpectedly broad except clause: {line}")
        self.assertIn("except ProviderOperationalError as exc:", caught)
        # Every ValueError catch re-raises rather than swallowing: each one is
        # followed by a `raise ... from exc` within its own block.
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "except ValueError as exc:":
                continue
            block = "\n".join(lines[index : index + 6])
            self.assertIn("raise InvalidRequest(", block)
            self.assertIn("from exc", block)
        # The ProviderAuthError catch exists ONLY to settle the ledger before
        # the error escapes. If a future edit ever drops its bare `raise`, a
        # bad key would silently fall through to the next provider again --
        # exactly the behaviour this classification was introduced to end.
        auth_catches = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "except ProviderAuthError:"
        ]
        self.assertEqual(len(auth_catches), 1, "expected exactly one auth catch")
        for index in auth_catches:
            block = lines[index : index + 20]
            body = [line.strip() for line in block[1:] if line.strip()]
            self.assertIn("raise", body, "auth catch must re-raise, not swallow")


# --------------------------------------------------------------------------
# Gates 2 and 4 -- key, quota, backoff. None of them issue a request.
# --------------------------------------------------------------------------


class OrchestratorGateTest(_SandboxedOrchestratorTest):
    def test_provider_without_a_key_is_dropped_not_tried(self) -> None:
        """'No key -> not an option, not try-and-fail' (§5)."""
        router = _Router(**{DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML)})

        result = self.search(router, "orchestrator probe", SearchType.WEB)

        self.assertFalse(router.called(TAVILY_HOST))
        self.assertFalse(router.called(FIRECRAWL_HOST))
        self.assertFalse(router.called(SERPER_HOST))
        self.assertEqual(result.provider_used, "ddg")
        for name in ("tavily", "firecrawl", "serper"):
            self.assertIn(f"{name} skipped: no API key configured", result.limitations)
        # No AttemptRecord for a keyless drop -- AttemptOutcome has no member
        # for it, and this phase does not invent one.
        self.assertEqual([a.provider for a in result.attempts], ["ddg"])

    def test_quota_exhaustion_records_skipped_quota_without_a_request(self) -> None:
        self.configure_keys(Provider.TAVILY)
        usage.configure(Provider.TAVILY, quota=5, reserve_credits=10)
        router = _Router()  # any request would be an AssertionError

        result = self.search(
            router, "orchestrator probe", SearchType.WEB, providers=[Provider.TAVILY]
        )

        self.assertEqual(router.log, [])
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.SKIPPED_QUOTA)
        self.assertIsNone(result.attempts[0].latency_ms)
        self.assertIn("tavily skipped: quota reserve reached", result.limitations)
        self.assertEqual(result.status, SearchStatus.UNAVAILABLE)
        # A skip costs nothing: the ledger must not have moved.
        self.assertEqual(usage.get_state("tavily").local_credits_used, 0)

    def test_open_circuit_breaker_records_skipped_backoff_without_a_request(self) -> None:
        self.configure_keys(Provider.TAVILY)
        cost = CreditCost(credits=1, unit="api_credit", basis="test")
        reservation = usage.reserve(Provider.TAVILY, cost)
        usage.reconcile(
            Provider.TAVILY,
            reservation,
            actual=cost,
            outcome=AttemptOutcome.ERROR,
            latency_ms=1.0,
        )
        router = _Router()

        result = self.search(
            router, "orchestrator probe", SearchType.WEB, providers=[Provider.TAVILY]
        )

        self.assertEqual(router.log, [])
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.SKIPPED_BACKOFF)
        self.assertIsNone(result.attempts[0].latency_ms)
        self.assertIn("tavily skipped: in backoff cooldown", result.limitations)

    def test_caller_subset_is_resorted_into_declaration_order(self) -> None:
        """A caller's subset filters; it does not re-rank. Passing
        [DDG, TAVILY] still tries Tavily first."""
        self.configure_keys(Provider.TAVILY)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS),
                DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML),
            }
        )

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.DDG, Provider.TAVILY],
        )

        self.assertEqual(result.provider_used, "tavily")
        self.assertFalse(router.called(DDG_HOST))

    def test_missing_adapter_raises_rather_than_being_dropped(self) -> None:
        """A declared provider with no registered adapter is a missing
        internal dependency -- I3 says that raises, never a quiet skip."""
        del REGISTRY[Provider.TAVILY]
        router = _Router()
        with self.assertRaises(InvalidRequest):
            self.search(
                router, "orchestrator probe", SearchType.WEB, providers=[Provider.TAVILY]
            )


# --------------------------------------------------------------------------
# limitations + I8's sanctioned exception
# --------------------------------------------------------------------------


class OrchestratorLimitationsTest(_SandboxedOrchestratorTest):
    def test_ddg_news_emulation_is_surfaced_in_limitations(self) -> None:
        """I8's one exception: DDG's `news` is a recency-biased web query.
        Its rows are marked INFERRED by the adapter, and the orchestrator
        says so out loud rather than letting them pass as dated news."""
        router = _Router(**{DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML)})

        result = self.search(
            router, "orchestrator probe", SearchType.NEWS, providers=[Provider.DDG]
        )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "ddg")
        self.assertIn("ddg emulates news; dates are inferred", result.limitations)
        self.assertIn(
            "date unavailable for 1 of 1 results", result.limitations
        )

    def test_undated_web_results_are_reported_in_limitations(self) -> None:
        self.configure_keys(Provider.TAVILY)
        router = _Router(**{TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS)})

        result = self.search(
            router, "orchestrator probe", SearchType.WEB, providers=[Provider.TAVILY]
        )

        self.assertIn("date unavailable for 2 of 2 results", result.limitations)


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------


class OrchestratorPublicApiTest(unittest.TestCase):
    def test_public_reexports_are_present(self) -> None:
        for name in (
            "search",
            "multi_search",
            "SearchType",
            "Provider",
            "ResultItem",
            "SearchResult",
            "AttemptRecord",
            "SertchiError",
            "InvalidRequest",
        ):
            self.assertTrue(hasattr(sertchi, name), f"sertchi.{name} is missing")
            self.assertIn(name, sertchi.__all__)

    def test_importing_sertchi_populates_the_registry(self) -> None:
        for provider in Provider:
            self.assertIn(provider, REGISTRY)

# --------------------------------------------------------------------------
# search()'s weighted-random draw (user decision 2026-09-26)
# --------------------------------------------------------------------------


class OrchestratorDrawOrderTest(_SandboxedOrchestratorTest):
    """The draw: weighted by searches left, one-time grants discounted, DDG
    last resort only; multi_search() never draws."""

    DRAWS = 4000

    def setUp(self) -> None:
        super().setUp()
        sertchi._draw_order = self._real_draw_order
        self._real_rng = sertchi._rng
        sertchi._rng = __import__("random").Random(20260926)

    def tearDown(self) -> None:
        sertchi._rng = self._real_rng
        super().tearDown()

    def candidate(self, provider: Provider):
        adapter = REGISTRY[provider]
        return sertchi._Candidate(
            provider=provider,
            adapter=adapter,
            cost=adapter.estimate_cost(SearchType.WEB),
        )

    def first_pick_share(self, providers, target: Provider) -> float:
        candidates = [self.candidate(p) for p in providers]
        hits = sum(
            1
            for _ in range(self.DRAWS)
            if sertchi._draw_order(candidates)[0].provider is target
        )
        return hits / self.DRAWS

    def test_first_pick_is_proportional_to_searches_left(self) -> None:
        # 3000 vs 1000 searches above a 10-credit reserve, both 1 credit/call.
        usage.configure(Provider.TAVILY, quota=3010, reserve_credits=10)
        usage.configure(Provider.KEENABLE, quota=1010, reserve_credits=10)
        share = self.first_pick_share(
            [Provider.KEENABLE, Provider.TAVILY], Provider.TAVILY
        )
        self.assertAlmostEqual(share, 0.75, delta=0.03)

    def test_one_time_grant_is_discounted(self) -> None:
        """Equal headroom, but Serper's is TOTAL: weight 1000 vs 250."""
        usage.configure(Provider.TAVILY, quota=1010, reserve_credits=10)
        usage.configure(Provider.SERPER, quota=1010, reserve_credits=10)
        self.assertEqual(
            usage.get_state(Provider.SERPER).quota_kind.value, "total"
        )
        share = self.first_pick_share(
            [Provider.TAVILY, Provider.SERPER], Provider.SERPER
        )
        # Literal, not derived from the constant -- a test that recomputed
        # 0.25/1.25 would pass with the discount deleted (negative control).
        self.assertAlmostEqual(share, 0.20, delta=0.03)

    def test_ddg_is_always_last(self) -> None:
        """DDG has no quota: never drawn, always tried after every metered
        provider -- even one with almost nothing left."""
        usage.configure(Provider.TAVILY, quota=11, reserve_credits=10)
        candidates = [self.candidate(p) for p in (Provider.TAVILY, Provider.DDG)]
        for _ in range(500):
            order = sertchi._draw_order(candidates)
            self.assertEqual(
                [c.provider for c in order], [Provider.TAVILY, Provider.DDG]
            )

    def test_units_are_normalised_by_call_cost(self) -> None:
        """Exa's pool is in micro-dollars: a raw-balance weight would let its
        ~10M swamp everything. Per call it is 1,000 searches, same as Tavily."""
        exa = self.candidate(Provider.EXA)
        usage.configure(
            Provider.EXA, quota=1000 * exa.cost.credits + 50_000, reserve_credits=50_000
        )
        usage.configure(Provider.TAVILY, quota=1010, reserve_credits=10)
        share = self.first_pick_share([Provider.EXA, Provider.TAVILY], Provider.EXA)
        self.assertAlmostEqual(share, 0.5, delta=0.03)

    def test_search_attempts_follow_the_draw_and_still_fall_through(self) -> None:
        """Whichever of the two is drawn first, a failing Tavily still falls
        through to Serper, and `attempts` lists calls in the order made."""
        REGISTRY[Provider.TAVILY] = _OperationalFake("tavily")
        self.configure_keys(Provider.SERPER)
        router = _Router(**{SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS)})
        seen_orders = set()
        for _ in range(40):
            # Fresh sandboxed ledger each round, so Tavily's repeated failure
            # never trips its circuit breaker and turns into a skip.
            usage.USAGE_PATH.unlink(missing_ok=True)
            result = self.search(
                router,
                "orchestrator probe",
                SearchType.WEB,
                providers=[Provider.TAVILY, Provider.SERPER],
            )
            self.assertEqual(result.status, SearchStatus.OK)
            self.assertEqual(result.provider_used, "serper")
            self.assertEqual(result.attempts[-1].provider, "serper")
            seen_orders.add(tuple(a.provider for a in result.attempts))
        # Serper-first (one call) and Tavily-first (error, then Serper) both
        # happen: the draw really is live inside search().
        self.assertEqual(seen_orders, {("serper",), ("tavily", "serper")})

    def test_multi_search_never_draws(self) -> None:
        def boom(_candidates):
            raise AssertionError("multi_search() must not use the draw")

        sertchi._draw_order = boom
        self.configure_keys(Provider.TAVILY)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(200, TAVILY_SHARED_URL),
                DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML),
            }
        )
        result = self.multi_search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.DDG],
        )
        self.assertEqual(result.results[0].provider, "tavily")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# A rejected key is a CONFIGURATION error and escapes the chain
# (user decision, 2026-08-20)
# --------------------------------------------------------------------------


class OrchestratorAuthErrorTest(_SandboxedOrchestratorTest):
    """The behaviour this classification was introduced to produce.

    Before the change, a 401 was a ProviderBlocked -- an operational failure
    -- so the chain fell through and the caller got a quietly degraded answer
    from a lower-ranked provider while their broken key stayed invisible.
    Now it escapes, and the chain stops where it failed.
    """

    def _auth_router(self) -> _Router:
        """Tavily rejects the key. Every other provider is wired to a
        response that WOULD succeed -- so if any of them gets called, the
        fall-through bug is back and the assertions below catch it."""
        return _Router(
            **{
                TAVILY_HOST: _json_responder(401, {"detail": "unauthorized"}),
                FIRECRAWL_HOST: _json_responder(200, FIRECRAWL_TWO_ROWS),
                SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS),
                DDG_HOST: _html_responder(200, DDG_SHARED_URL_HTML),
            }
        )

    def test_search_raises_and_stops_the_chain(self) -> None:
        self.configure_keys(Provider.TAVILY, Provider.FIRECRAWL, Provider.SERPER)
        router = self._auth_router()

        with self.assertRaises(ProviderAuthError) as caught:
            self.search(router, "auth probe", SearchType.WEB)

        self.assertEqual(caught.exception.provider, "tavily")
        # The whole point: no silent fallback to a lower-ranked provider.
        self.assertFalse(router.called(FIRECRAWL_HOST))
        self.assertFalse(router.called(SERPER_HOST))
        self.assertFalse(router.called(DDG_HOST))

    def test_multi_search_re_raises_it_too(self) -> None:
        """multi_search gathers with return_exceptions=True, which captures
        everything -- including the errors I3 says must escape. This proves
        the re-raise scan covers ProviderAuthError, not just bugs."""
        self.configure_keys(Provider.TAVILY, Provider.FIRECRAWL, Provider.SERPER)

        with self.assertRaises(ProviderAuthError):
            self.multi_search(self._auth_router(), "auth probe", SearchType.WEB)

    def test_ledger_charges_nothing_and_leaves_the_breaker_alone(self) -> None:
        """A refused call did no work, so it must not bill -- and the
        provider is not unhealthy, so it must not enter cooldown. Advancing
        the breaker here would leave tavily in a cooldown of up to
        BACKOFF_CAP_S seconds right after the user fixes their key."""
        self.configure_keys(Provider.TAVILY)
        before = usage.get_state("tavily")

        with self.assertRaises(ProviderAuthError):
            self.search(self._auth_router(), "auth probe", SearchType.WEB)

        after = usage.get_state("tavily")
        self.assertEqual(after.local_credits_used, before.local_credits_used)
        self.assertEqual(after.consecutive_failures, 0)
        self.assertIsNone(after.cooldown_until)
        # And the reservation was released rather than left hanging.
        self.assertEqual(after.reservations, [])

    def test_contrast_an_operational_failure_still_falls_through(self) -> None:
        """The other direction, so this test file proves the narrowing rather
        than just the raising: a 429 from Tavily still falls through."""
        self.configure_keys(Provider.TAVILY, Provider.SERPER)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(429, {"detail": "rate limited"}),
                SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS),
            }
        )

        result = self.search(router, "auth probe", SearchType.WEB)

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "serper")


# --------------------------------------------------------------------------
# Gate 5 -- slot reservation pacing (fable-prompt-throttling-v1.md §5 Phase 4)
# --------------------------------------------------------------------------


def _poke_next_slot(provider: str, seconds_ahead: float) -> None:
    """Force ``provider``'s ledger record to look like it already has a
    reserved slot ``seconds_ahead`` in the future, so the next
    ``reserve_slot()`` call has something to pace or refuse against.

    Uses ``usage``'s own private ``_now``/``_iso_precise`` -- the exact
    functions ``reserve_slot()`` itself uses for slot arithmetic (N3) --
    rather than reimplementing the ISO formatting, so a poked value is
    indistinguishable from one ``reserve_slot()`` would have written.
    """
    usage.get_state(provider)  # ensure the record exists on disk
    data = json.loads(usage.USAGE_PATH.read_text(encoding="utf-8"))
    slot = usage._now() + timedelta(seconds=seconds_ahead)
    data["providers"][provider]["next_slot_at"] = usage._iso_precise(slot)
    usage.USAGE_PATH.write_text(json.dumps(data), encoding="utf-8")


class _KwargGuardFake:
    """Occupies Tavily's slot. Blows up if ``max_wait_s`` (or any other
    orchestrator-only kwarg) ever reaches an adapter -- the N2 tripwire."""

    name = "tavily"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        if "max_wait_s" in opts:
            raise AssertionError("N2 violated: max_wait_s leaked into estimate_cost")
        return CreditCost(credits=1, unit="api_credit", basis="fake")

    async def search(self, query, search_type, *, client=None, **opts):
        if "max_wait_s" in opts:
            raise AssertionError("N2 violated: max_wait_s leaked into search")
        self.calls += 1
        return ProviderResponse(
            provider=self.name,
            results=[],
            provider_metadata={"reported_remaining": None},
            usage=CreditCost(credits=1, unit="api_credit", basis="fake"),
        )


class OrchestratorSlotReservationTest(_SandboxedOrchestratorTest):
    """Gate 5: pace at attempt time, fall through on refusal, report it
    honestly. Uses the real Tavily adapter's slot (via ``REGISTRY``'s
    unmodified default) with ``usage.MIN_REQUEST_INTERVAL_S`` monkeypatched
    to give it an interval -- DDG is last in declaration order, so a
    throttled DDG has nobody to fall through to; Tavily does.
    """

    def setUp(self) -> None:
        super().setUp()
        self._real_interval = dict(usage.MIN_REQUEST_INTERVAL_S)
        usage.MIN_REQUEST_INTERVAL_S[Provider.TAVILY.value] = 1.1

    def tearDown(self) -> None:
        usage.MIN_REQUEST_INTERVAL_S.clear()
        usage.MIN_REQUEST_INTERVAL_S.update(self._real_interval)
        super().tearDown()

    def test_throttled_provider_falls_through_and_note_is_suppressed(self) -> None:
        """Tavily's slot is an hour out -- default max_wait_s=3.0 refuses.
        Serper, ranked below it, still answers -- status is OK, so the
        throttled note is suppressed even though the AttemptRecord is not."""
        self.configure_keys(Provider.TAVILY, Provider.SERPER)
        _poke_next_slot(Provider.TAVILY.value, 3600.0)
        router = _Router(**{SERPER_HOST: _json_responder(200, SERPER_TWO_ROWS)})

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY, Provider.SERPER],
        )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "serper")
        self.assertFalse(router.called(TAVILY_HOST))
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[0].provider, "tavily")
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.THROTTLED)
        self.assertIsNone(result.attempts[0].latency_ms)
        self.assertFalse(
            any("throttled" in note for note in result.limitations),
            "a throttled note leaked into limitations despite an OK status",
        )

    def test_throttled_provider_with_no_fallback_is_unavailable_and_reported(
        self,
    ) -> None:
        """Same throttle, but Tavily is the only candidate: status is
        UNAVAILABLE, so this time the note IS included."""
        self.configure_keys(Provider.TAVILY)
        _poke_next_slot(Provider.TAVILY.value, 3600.0)
        router = _Router()  # any request at all is an AssertionError

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY],
        )

        self.assertEqual(result.status, SearchStatus.UNAVAILABLE)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.THROTTLED)
        self.assertIsNone(result.attempts[0].latency_ms)
        self.assertTrue(
            any("throttled" in note for note in result.limitations),
            "an UNAVAILABLE search must surface its throttled note",
        )

    def test_throttled_provider_issues_no_request_at_all(self) -> None:
        """The zero-request proof (mirrors the I8 pattern): an empty router
        with no routes wired for Tavily raises on any request to it, so a
        clean run without exception proves the network was never touched."""
        self.configure_keys(Provider.TAVILY)
        _poke_next_slot(Provider.TAVILY.value, 3600.0)
        router = _Router()  # TAVILY_HOST deliberately unwired

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY],
        )

        self.assertEqual(router.log, [])
        self.assertEqual(result.attempts[0].outcome, AttemptOutcome.THROTTLED)

    def test_a_granted_wait_actually_sleeps(self) -> None:
        """A slot 0.3s out (well inside the default 3.0s ceiling) is granted,
        not refused -- and the orchestrator really awaits that wait rather
        than firing immediately."""
        self.configure_keys(Provider.TAVILY)
        _poke_next_slot(Provider.TAVILY.value, 0.3)
        router = _Router(**{TAVILY_HOST: _json_responder(200, TAVILY_TWO_ROWS)})

        started = time.monotonic()
        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY],
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertTrue(router.called(TAVILY_HOST))
        self.assertGreaterEqual(elapsed, 0.2, "granted wait was not actually slept")

    def test_max_wait_s_never_reaches_an_adapter(self) -> None:
        """N2: max_wait_s is keyword-only on search(), forwarded only to
        _attempt() -- never left riding in **opts, where it would reach
        every adapter's estimate_cost() and search()."""
        fake = _KwargGuardFake()
        REGISTRY[Provider.TAVILY] = fake
        router = _Router()

        result = self.search(
            router,
            "orchestrator probe",
            SearchType.WEB,
            providers=[Provider.TAVILY],
            max_wait_s=1.0,
        )

        self.assertEqual(fake.calls, 1)
        self.assertEqual(result.status, SearchStatus.EMPTY)

    def test_gate5_uses_asyncio_sleep_and_claims_before_metering(self) -> None:
        """Structural guard, extending the existing source-parsing style:
        asyncio.sleep is used (never the blocking time.sleep), and inside
        _attempt() the slot claim happens before metered_call() is entered."""
        source = Path(sertchi.__file__).read_text(encoding="utf-8")
        self.assertIn("await asyncio.sleep(", source)
        self.assertNotIn("time.sleep(", source)

        start = source.index("async def _attempt(")
        end = source.index("\ndef _ordered_attempts(")
        self.assertGreater(end, start, "could not isolate _attempt()'s body")
        body = source[start:end]

        reserve_idx = body.index("usage.reserve_slot(")
        metered_idx = body.index("with usage.metered_call(")
        self.assertLess(
            reserve_idx,
            metered_idx,
            "reserve_slot() must be claimed before metered_call() is entered",
        )
