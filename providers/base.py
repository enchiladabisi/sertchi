"""Provider protocol and the operational-error hierarchy (SKETCH.md §4).

Every adapter (``providers/ddg.py``, and later ``tavily.py``/``firecrawl.py``/
``serper.py``) implements ``SearchProvider`` and registers an instance in
``REGISTRY``. The protocol's name is ``SearchProvider``, not ``Provider`` --
``Provider`` stays the public enum in ``types.py`` (SKETCH.md §2.7 decision 1).
"""

from __future__ import annotations

from typing import Protocol

import httpx

from ..types import CreditCost, Provider, ProviderResponse, SearchType, SertchiError


class ProviderOperationalError(SertchiError):
    """Base of every *operational* provider failure.

    This closed set — this class plus its five subclasses below — IS I3's
    definition of "operational" in code: an adapter catches only these and
    lets everything else (bad arguments, internal schema violations, an
    outright bug) propagate uncaught. The orchestrator (``__init__.py``,
    Phase 7) catches this base class to fall through to the next provider in
    the chain; it never needs to catch the subclasses individually.

    Deliberately NOT in this set: a rejected API key. That is
    ``types.ProviderAuthError``, a configuration error that escapes past
    ``search()`` instead of falling through (user decision, 2026-08-20) —
    a bad key is something the user must fix, not something a different
    provider can succeed at. Being out of credit or region-blocked IS
    operational and stays here as ``ProviderBlocked``.
    """


class ProviderTimeout(ProviderOperationalError):
    """The request timed out (connect, read, write, or pool)."""


class ProviderRateLimited(ProviderOperationalError):
    """HTTP 429, or a provider-specific equivalent."""


class ProviderUnavailable(ProviderOperationalError):
    """HTTP 5xx, or a connection/DNS failure reaching the provider."""


class ProviderBlocked(ProviderOperationalError):
    """A provider-specific block/challenge signal (e.g. DDG's anomaly page:
    HTTP 200 with no real data)."""


class ProviderMalformedResponse(ProviderOperationalError):
    """The provider responded, but the body could not be parsed into the
    shape the adapter expects (e.g. invalid JSON, a missing required field)."""


class SearchProvider(Protocol):
    """One provider adapter's contract (SKETCH.md §4; §11's conformance
    suite, ``tests/conformance.py``, enforces it in a runnable form). An
    adapter never learns its position in a fallback chain (I2) -- it only
    describes its own call: its rows, its own identity, its raw metadata,
    and what the call cost.
    """

    name: str
    supports: "frozenset[SearchType]"
    requires_key: bool

    def estimate_cost(self, search_type: SearchType, **opts) -> CreditCost:
        """Pure, network-free pre-call cost estimate -- the Phase 6 usage
        gate's ``can_afford()`` needs this before any request is made."""
        ...

    async def search(
        self,
        query: str,
        search_type: SearchType,
        *,
        client: httpx.AsyncClient | None = None,
        **opts,
    ) -> ProviderResponse:
        """Run one search. ``client`` is a test-injection seam
        (``garudagent/websearch.py:101``'s pattern) -- production callers
        omit it and the adapter opens/closes its own."""
        ...


REGISTRY: dict[Provider, SearchProvider] = {}
