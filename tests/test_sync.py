"""Phase 8: sertchi/sync.py -- the plain, non-async front door
(fable-prompt-build-v1.md §5 Phase 8).

CRITICAL SANDBOXING: every test repoints keys.CONFIG_DIR/KEYS_PATH and
usage.CONFIG_DIR/USAGE_PATH/LOCK_PATH at a tempfile.TemporaryDirectory and
restores them in tearDown, mirroring tests/test_orchestrator.py's
_SandboxedOrchestratorTest exactly (keys and usage move together: the
orchestrator that sync.py wraps reads keys during gate 2 and the ledger
during gate 4). tearDown additionally snapshots the real
``~/.config/sertchi/`` directory's existence/listing before every test and
re-checks it afterwards, so a regression that starts writing through the
real config path fails the suite immediately rather than silently.

INJECTION SEAM WITHOUT A ``client`` PARAMETER: sync.py deliberately does not
accept ``client`` -- Phase 8's signature is the async originals' MINUS
``client``, because a non-async caller has no ``httpx.AsyncClient`` of its
own to hand over. That means the usual seam (pass ``client=`` straight
through to ``sertchi.search()``, as test_orchestrator.py's
``_SandboxedOrchestratorTest.client_for()`` does) is not available here. Every
adapter falls back to ``client or httpx.AsyncClient()`` when ``client`` is
``None`` (``providers/ddg.py:324`` and its three siblings all resolve
``httpx.AsyncClient`` via a plain module-attribute lookup at call time), so
this file's success-path tests patch ``httpx.AsyncClient`` itself for the
duration of one call: the patched constructor forwards to the real
``httpx.AsyncClient`` with a ``transport=httpx.MockTransport(router)`` kwarg
injected. That redirects every adapter's self-opened client at the mock
transport without adding a ``client`` parameter back to sync.py's signature,
which the build plan forbids.

I9: no network. Every request is served by a MockTransport handler that
raises on any host it was not told to expect.

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from sertchi import keys, sync, usage
from sertchi.providers.base import REGISTRY
from sertchi.types import InvalidRequest, Provider, SearchStatus, SearchType

# --------------------------------------------------------------------------
# Fixtures -- module-level constants, the house convention (§2.6), kept
# self-contained rather than imported from tests/test_orchestrator.py so this
# file's Phase 8 scope does not reach into another phase's test module.
# --------------------------------------------------------------------------

TAVILY_HOST = "api.tavily.com"
DDG_HOST = "duckduckgo.com"  # matches both lite. and html. subdomains

TAVILY_ONE_ROW = {
    "query": "sync probe",
    "results": [
        {
            "title": "Sync Wrapper Row",
            "url": "https://example.com/sync-row",
            "content": "A row returned through the sync wrapper.",
            "score": 0.5,
        }
    ],
}

DDG_EMPTY_HTML = "<html><body><table></table></body></html>"


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


class _Router:
    """Routes a MockTransport call by host suffix; an unlisted host is an
    AssertionError, not a default response -- the same discipline
    tests/test_orchestrator.py's ``_Router`` uses, kept local to this file."""

    def __init__(self, **routes) -> None:
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        for suffix, responder in self.routes.items():
            if host.endswith(suffix):
                return responder(request)
        raise AssertionError(f"unexpected request to {host!r}")


def _patched_mock_transport(router: _Router):
    """Context manager: while active, every ``httpx.AsyncClient()`` call made
    with no explicit ``transport`` gets ``router`` wired in via
    ``httpx.MockTransport``. This is the seam described in the module
    docstring -- it stands in for the ``client=`` parameter sync.py does not
    have."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(router))
        return real_async_client(*args, **kwargs)

    return patch("httpx.AsyncClient", side_effect=factory)


# --------------------------------------------------------------------------
# Sandbox base -- mirrors tests/test_orchestrator.py's
# _SandboxedOrchestratorTest, plus an explicit real-config-untouched check.
# --------------------------------------------------------------------------


class _SandboxedSyncTest(unittest.TestCase):
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

        # Snapshot the REAL ~/.config/sertchi/ (module-level constant,
        # deliberately read via Path.home() again rather than via
        # keys.CONFIG_DIR, which is already repointed above) so tearDown can
        # prove this test module never touched it.
        self._real_config_dir = Path.home() / ".config" / "sertchi"
        self._real_config_existed = self._real_config_dir.exists()
        self._real_config_listing = (
            sorted(p.name for p in self._real_config_dir.iterdir())
            if self._real_config_existed
            else None
        )

    def tearDown(self) -> None:
        keys.CONFIG_DIR, keys.KEYS_PATH = self._real_keys
        usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH = self._real_usage
        REGISTRY.clear()
        REGISTRY.update(self._real_registry)

        now_existed = self._real_config_dir.exists()
        self.assertEqual(
            now_existed,
            self._real_config_existed,
            "the real ~/.config/sertchi/ existence changed -- a test wrote "
            "through the real config path",
        )
        if now_existed:
            now_listing = sorted(p.name for p in self._real_config_dir.iterdir())
            self.assertEqual(
                now_listing,
                self._real_config_listing,
                "the real ~/.config/sertchi/ contents changed -- a test "
                "wrote through the real config path",
            )

        self._tmpdir.cleanup()

    def configure_keys(self, *providers: Provider) -> None:
        for provider in providers:
            keys.set_key(provider.value, f"test-key-for-{provider.value}")


# --------------------------------------------------------------------------
# Success path -- SearchResult off a mocked transport, no client parameter
# --------------------------------------------------------------------------


class SyncSuccessPathTest(_SandboxedSyncTest):
    def test_search_returns_search_result_via_mock_transport(self) -> None:
        """sync.search() has no ``client`` kwarg to inject through, so this
        proves the httpx.AsyncClient patch seam actually reaches the
        adapter: a real SearchResult comes back with the mocked row, and
        nothing hit the real network (MockTransport would raise otherwise)."""
        self.configure_keys(Provider.TAVILY)
        router = _Router(**{TAVILY_HOST: _json_responder(200, TAVILY_ONE_ROW)})

        with _patched_mock_transport(router):
            result = sync.search(
                "sync probe", SearchType.WEB, providers=[Provider.TAVILY]
            )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "tavily")
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].title, "Sync Wrapper Row")

    def test_multi_search_returns_search_result_via_mock_transport(self) -> None:
        """Same seam, exercising the fan-out front door: Tavily answers,
        DDG (keyless, always a candidate) answers empty, and the merged
        result still comes back through sync.multi_search() with no
        ``client`` kwarg anywhere in the call."""
        self.configure_keys(Provider.TAVILY)
        router = _Router(
            **{
                TAVILY_HOST: _json_responder(200, TAVILY_ONE_ROW),
                DDG_HOST: _html_responder(200, DDG_EMPTY_HTML),
            }
        )

        with _patched_mock_transport(router):
            result = sync.multi_search(
                "sync probe",
                SearchType.WEB,
                providers=[Provider.TAVILY, Provider.DDG],
            )

        self.assertEqual(result.status, SearchStatus.OK)
        self.assertEqual(result.provider_used, "tavily")
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].title, "Sync Wrapper Row")


# --------------------------------------------------------------------------
# Running-event-loop guard -- the documented RuntimeError, both entry points
# --------------------------------------------------------------------------


class SyncRunningLoopGuardTest(_SandboxedSyncTest):
    def test_search_raises_documented_runtime_error_from_running_loop(self) -> None:
        async def call_from_inside_a_loop() -> None:
            sync.search("q", SearchType.WEB, providers=[Provider.TAVILY])

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(call_from_inside_a_loop())

        self.assertEqual(
            str(ctx.exception),
            "sertchi.sync.search() cannot be called from a running event "
            "loop; await sertchi.search() instead",
        )

    def test_multi_search_raises_documented_runtime_error_from_running_loop(
        self,
    ) -> None:
        async def call_from_inside_a_loop() -> None:
            sync.multi_search("q", SearchType.WEB, providers=[Provider.TAVILY])

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(call_from_inside_a_loop())

        self.assertEqual(
            str(ctx.exception),
            "sertchi.sync.multi_search() cannot be called from a running "
            "event loop; await sertchi.multi_search() instead",
        )

    def test_search_does_not_raise_when_no_loop_is_running(self) -> None:
        """The negative case for the guard itself: called the ordinary way
        (no loop running on this thread), the guard must NOT fire -- only
        the real work (here, a bad search_type) should raise."""
        with self.assertRaises(InvalidRequest):
            sync.search("q", "nonsense")


# --------------------------------------------------------------------------
# I3 through the wrapper -- InvalidRequest propagates unchanged, not
# repackaged as a RuntimeError or a bare SertchiError
# --------------------------------------------------------------------------


class SyncInvalidRequestPropagationTest(_SandboxedSyncTest):
    def test_search_invalid_request_propagates_unchanged(self) -> None:
        with self.assertRaises(InvalidRequest) as ctx:
            sync.search("q", "nonsense-search-type")
        # Not merely "some exception" -- the exact type I3 names, and its
        # message intact (proves the wrapper did not catch-and-rewrap it).
        self.assertIn("nonsense-search-type", str(ctx.exception))

    def test_multi_search_invalid_request_propagates_unchanged(self) -> None:
        with self.assertRaises(InvalidRequest) as ctx:
            sync.multi_search("q", "nonsense-search-type")
        self.assertIn("nonsense-search-type", str(ctx.exception))

    def test_search_blank_query_raises_invalid_request(self) -> None:
        with self.assertRaises(InvalidRequest):
            sync.search("   ", SearchType.WEB)


# --------------------------------------------------------------------------
# max_wait_s pass-through -- N2: explicit keyword, never in **opts
# --------------------------------------------------------------------------


class SyncMaxWaitSTest(_SandboxedSyncTest):
    def test_search_forwards_max_wait_s_to_async_original(self) -> None:
        """sync.search() must forward max_wait_s as a named keyword to the
        async sertchi.search() it wraps -- not let it fall into **opts."""

        async def fake_async_search(*args, **kwargs):
            fake_async_search.captured_kwargs = kwargs
            from sertchi.types import SearchResult, SearchStatus

            return SearchResult(
                status=SearchStatus.EMPTY,
                query=args[0] if args else kwargs.get("query"),
                search_type=SearchType.WEB,
                results=[],
                provider_used=None,
                attempts=None,
                limitations=[],
            )

        with patch("sertchi.sync._async_search", fake_async_search):
            sync.search("q", SearchType.WEB, providers=[Provider.TAVILY], max_wait_s=1.5)

        self.assertIn("max_wait_s", fake_async_search.captured_kwargs)
        self.assertEqual(fake_async_search.captured_kwargs["max_wait_s"], 1.5)
        self.assertNotIn("max_wait_s", fake_async_search.captured_kwargs.get("opts", {}))

    def test_multi_search_forwards_max_wait_s_to_async_original(self) -> None:
        """Same contract for sync.multi_search() -- max_wait_s reaches the
        async sertchi.multi_search() it wraps as a named keyword."""

        async def fake_async_multi_search(*args, **kwargs):
            fake_async_multi_search.captured_kwargs = kwargs
            from sertchi.types import SearchResult, SearchStatus

            return SearchResult(
                status=SearchStatus.EMPTY,
                query=args[0] if args else kwargs.get("query"),
                search_type=SearchType.WEB,
                results=[],
                provider_used=None,
                attempts=None,
                limitations=[],
            )

        with patch("sertchi.sync._async_multi_search", fake_async_multi_search):
            sync.multi_search(
                "q", SearchType.WEB, providers=[Provider.TAVILY], max_wait_s=2.5
            )

        self.assertIn("max_wait_s", fake_async_multi_search.captured_kwargs)
        self.assertEqual(fake_async_multi_search.captured_kwargs["max_wait_s"], 2.5)
        self.assertNotIn(
            "max_wait_s", fake_async_multi_search.captured_kwargs.get("opts", {})
        )


if __name__ == "__main__":
    unittest.main()
