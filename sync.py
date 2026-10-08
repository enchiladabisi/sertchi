"""Sertchi -- the plain, non-async front door (fable-prompt-build-v1.md §5
Phase 8; rationale in SKETCH.md §8's "Sync wrapper" bullet, §Q3 resolved).

WHY THIS MODULE EXISTS
-----------------------
``providers/`` and ``sertchi/__init__.py`` stay ``async`` on purpose -- that
is what lets the fallback chain and ``multi_search()``'s fan-out actually
overlap their network calls instead of waiting on them one at a time. But
Sertchi is meant to be one centralized installation called on by many
different projects (SKETCH.md §7), and not all of those callers are
themselves async. A plain script that just wants one search result should
not have to learn ``asyncio.run(...)`` to get it. This module is that: a
thin wrapper, nothing more -- it adds no orchestration logic of its own and
inherits I2/I3/I5/I8 unchanged from the functions it wraps.

SIGNATURES -- IDENTICAL TO THE ASYNC ORIGINALS MINUS ``client``
-----------------------------------------------------------------
``sertchi.search()``/``sertchi.multi_search()`` accept a ``client:
httpx.AsyncClient | None = None`` test-injection seam (``garudagent/
websearch.py:101``'s pattern) for a caller who is already inside an event
loop and wants to reuse a connection pool. A caller reaching for THIS module
is by definition not managing an event loop of their own, so it has no
``httpx.AsyncClient`` to hand over -- if it did, ``await sertchi.search()``
was sitting right there and this module was the wrong door. ``client`` is
therefore dropped, not defaulted; every other keyword argument (``providers``,
``include_attempts``, and the passthrough ``**opts``) is forwarded unchanged.

RUNNING-EVENT-LOOP GUARD -- AN EXPLICIT ERROR, NEVER A HIDDEN THREAD
----------------------------------------------------------------------
``asyncio.run()`` already raises when called from inside a running loop, but
its own message names ``asyncio.run()``, not this module's public function,
and gives a caller no next step. Both entry points below check first, via
``asyncio.get_running_loop()`` inside a ``try``/``except RuntimeError``, and
raise their own ``RuntimeError`` with an actionable message before ever
reaching ``asyncio.run()``. Deliberately NOT handled by silently spawning a
background thread to run the coroutine on a second loop: that would make a
caller's mistake -- reaching for the sync wrapper from inside async code,
when ``await sertchi.search()`` was already available -- invisible instead of
loud, and would hand back a result from a thread whose lifetime the caller
never asked to manage. The explicit error IS the decision (fable-prompt-
build-v1.md §5 Phase 8).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from . import multi_search as _async_multi_search
from . import search as _async_search
from .types import Provider, SearchResult, SearchType

__all__ = ["search", "multi_search"]


def _refuse_if_loop_running(entry_point: str, async_name: str) -> None:
    """Raise the documented ``RuntimeError`` iff a loop is already running.

    ``asyncio.get_running_loop()`` raises ``RuntimeError`` exactly when there
    is NO running loop -- that is the safe case for ``asyncio.run()``, so it
    is caught and swallowed here. If it does *not* raise, a loop is already
    live on this thread and ``asyncio.run()`` would fail; that is the case
    this module surfaces itself, with a message naming the actual functions
    involved rather than asyncio's own generic one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop running -- asyncio.run() below is safe
    raise RuntimeError(
        f"sertchi.sync.{entry_point}() cannot be called from a running "
        f"event loop; await sertchi.{async_name}() instead"
    )


def search(
    query: str,
    search_type: SearchType | str = SearchType.WEB,
    *,
    providers: Sequence[Provider] | None = None,
    include_attempts: bool = True,
    max_wait_s: float | None = None,
    **opts,
) -> SearchResult:
    """Single-search with automatic fallback -- the synchronous front door.

    A blocking wrapper around ``await sertchi.search(...)`` (§5): walks the
    candidate chain in ``Provider`` declaration order and returns as soon as
    the first provider answers with at least one row. Every argument below
    means exactly what it means on the async original; see ``sertchi.search``
    for the full contract (candidate gates, fail-open behavior, the status
    table). The only difference is that this function returns the finished
    ``SearchResult`` directly instead of a coroutine you would otherwise
    ``await``, by opening and closing its own event loop via
    ``asyncio.run()``.

    Use ``await sertchi.search(...)`` instead, not this function, when your
    own code is already ``async def`` and running inside an event loop --
    calling this one from there raises ``RuntimeError`` rather than silently
    nesting loops (see the module docstring).

    Args:
        query: what to search for. Blank or non-string raises
            ``InvalidRequest`` (unchanged from the async original).
        search_type: ``SearchType.WEB`` (default) or ``SearchType.NEWS``, or
            either one's string value.
        providers: optional subset, still subject to every candidate gate;
            filters the chain, does not reorder it.
        include_attempts: when ``False``, ``SearchResult.attempts`` is
            ``None`` in the object you get back (the data is still collected
            and still reported to the usage ledger either way).
        max_wait_s: seconds to wait for a paced provider's next free slot
            before falling through; see ``sertchi.search`` for the full
            contract.
        **opts: forwarded to the winning adapter and to its
            ``estimate_cost()`` unchanged (e.g. ``search_depth`` for Tavily).

    Returns:
        A ``SearchResult``, exactly as ``sertchi.search()`` would produce.

    Raises:
        RuntimeError: called from inside an already-running event loop.
        InvalidRequest: bad ``search_type``, blank ``query``, or an unknown
            provider name -- propagates out of this wrapper unchanged, not
            repackaged (I3 still applies through the wrapper).
        Exception: anything the async original would raise that is not a
            ``ProviderOperationalError`` propagates unchanged.
    """
    _refuse_if_loop_running("search", "search")
    return asyncio.run(
        _async_search(
            query,
            search_type,
            providers=providers,
            include_attempts=include_attempts,
            max_wait_s=max_wait_s,
            **opts,
        )
    )


def multi_search(
    query: str,
    search_type: SearchType | str = SearchType.WEB,
    *,
    providers: Sequence[Provider] | None = None,
    include_attempts: bool = True,
    max_wait_s: float | None = None,
    **opts,
) -> SearchResult:
    """Concurrent fan-out across every eligible provider -- the synchronous
    front door for ``sertchi.multi_search()`` (§6).

    A blocking wrapper around ``await sertchi.multi_search(...)``: every
    surviving candidate is called concurrently (one request each) and the
    rows are merged with ``dedupe.merge()``. Same four candidate gates, same
    ``SearchResult`` shape, same status table as ``search()`` above -- see
    ``sertchi.multi_search`` for the full contract. This spends every
    eligible provider's quota on one call, so use it deliberately, not as a
    default.

    Use ``await sertchi.multi_search(...)`` instead, not this function, from
    inside code that is already ``async def`` and running its own event
    loop -- calling this one there raises ``RuntimeError`` rather than
    silently nesting loops (see the module docstring).

    Args:
        query, search_type, providers, include_attempts, **opts: identical in
            meaning to ``search()`` above and to ``sertchi.multi_search()``.
        max_wait_s: seconds to wait for a paced provider's next free slot
            before falling through; see ``sertchi.search`` for the full
            contract.

    Returns:
        A ``SearchResult`` with ``provider_used`` set to a comma-joined list
        of every provider that contributed a surviving row, exactly as
        ``sertchi.multi_search()`` would produce.

    Raises:
        RuntimeError: called from inside an already-running event loop.
        InvalidRequest: bad ``search_type``, blank ``query``, or an unknown
            provider name -- propagates out of this wrapper unchanged, not
            repackaged (I3 still applies through the wrapper).
        Exception: anything the async original would raise that is not a
            ``ProviderOperationalError`` propagates unchanged.
    """
    _refuse_if_loop_running("multi_search", "multi_search")
    return asyncio.run(
        _async_multi_search(
            query,
            search_type,
            providers=providers,
            include_attempts=include_attempts,
            max_wait_s=max_wait_s,
            **opts,
        )
    )
