"""Phase 1: types.py is pure schema — field names/defaults, Provider's
declaration order (the fallback-chain/dedupe-tiebreak source of truth), and
UsageState.best_available_remaining's fallback rule. No network, no I/O.

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import dataclasses
import unittest

from sertchi.types import (
    AttemptOutcome,
    AttemptRecord,
    CreditCost,
    DateConfidence,
    InvalidRequest,
    Provider,
    ProviderResponse,
    QuotaKind,
    ResultItem,
    SearchResult,
    SearchStatus,
    SearchType,
    SertchiError,
    UsageState,
)


class TypesTest(unittest.TestCase):
    def test_provider_declaration_order_is_the_quality_order(self) -> None:
        # This order is the single source of truth for the fallback chain
        # (§5) and the dedupe tiebreak (§6) — nothing else may re-declare it.
        # Updated 2026-09-13 (fable-prompt-keenable-exa-reorder.md Phase 2):
        # KEENABLE prepended ahead of the original four, a pure prepend with
        # no reshuffling among tavily/firecrawl/serper/ddg. Updated again in
        # the same pass's Phase 3: EXA inserted immediately after KEENABLE,
        # giving the final agreed order below. Evidence for the ordering
        # itself lives in etc/planning/NEW-PROVIDERS.md §5.3, not here.
        self.assertEqual(
            [p.value for p in Provider],
            ["keenable", "exa", "tavily", "firecrawl", "serper", "ddg"],
        )

    def test_search_type_has_only_web_and_news(self) -> None:
        self.assertEqual([t.value for t in SearchType], ["web", "news"])

    def test_enum_values(self) -> None:
        self.assertEqual(
            [c.value for c in DateConfidence], ["reported", "inferred", "none"]
        )
        self.assertEqual(
            [o.value for o in AttemptOutcome],
            [
                "success",
                "error",
                "empty",
                "skipped_quota",
                "skipped_backoff",
                "throttled",
            ],
        )
        # THROTTLED is a distinct fact from either SKIPPED_* member (a slot
        # wasn't free vs. the provider being unhealthy/out of quota) — future
        # code must not fold them together.
        self.assertNotIn(
            AttemptOutcome.THROTTLED,
            (AttemptOutcome.SKIPPED_QUOTA, AttemptOutcome.SKIPPED_BACKOFF),
        )
        # PARTIAL is declared (fable-prompt-build-v1.md §6 Q8) but this
        # module makes no claim about who assigns it — that's Phase 7's job.
        self.assertEqual(
            [s.value for s in SearchStatus], ["ok", "partial", "unavailable", "empty"]
        )
        self.assertEqual(
            [k.value for k in QuotaKind], ["monthly", "total", "expiring"]
        )

    def test_result_item_fields_and_defaults(self) -> None:
        names = [f.name for f in dataclasses.fields(ResultItem)]
        self.assertEqual(
            names,
            [
                "title",
                "url",
                "snippet",
                "domain",
                "registrable_domain",
                "published_date",
                "date_confidence",
                "provider",
                "provider_rank",
                "provider_score",
                "thumbnail_url",
            ],
        )
        # #4 — bare rank/score/normalized_rank must NOT appear; reserved for
        # a future cross-provider merged rank.
        self.assertNotIn("rank", names)
        self.assertNotIn("score", names)
        self.assertNotIn("normalized_rank", names)

        item = ResultItem(
            title="t",
            url="https://example.com/a",
            snippet="s",
            domain="example.com",
            registrable_domain="example.com",
            published_date=None,
            date_confidence=DateConfidence.NONE,
            provider="ddg",
            provider_rank=1,
        )
        self.assertIsNone(item.provider_score)
        self.assertIsNone(item.thumbnail_url)

    def test_credit_cost_is_frozen(self) -> None:
        cost = CreditCost(credits=1, unit="api_credit", basis="basic depth")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cost.credits = 2  # type: ignore[misc]

    def test_attempt_record_fields_and_skipped_latency(self) -> None:
        names = [f.name for f in dataclasses.fields(AttemptRecord)]
        self.assertEqual(names, ["provider", "outcome", "latency_ms", "error"])
        rec = AttemptRecord(
            provider="serper",
            outcome=AttemptOutcome.SKIPPED_QUOTA,
            latency_ms=None,
            error="quota reserve reached",
        )
        self.assertIsNone(rec.latency_ms)

    def test_provider_response_and_search_result_are_distinct_shapes(self) -> None:
        # I2 — a provider returns ProviderResponse, never SearchResult.
        self.assertEqual(
            [f.name for f in dataclasses.fields(ProviderResponse)],
            ["provider", "results", "provider_metadata", "usage"],
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(SearchResult)],
            [
                "query",
                "search_type",
                "status",
                "provider_used",
                "attempts",
                "results",
                "limitations",
            ],
        )
        response = ProviderResponse(
            provider="tavily",
            results=[],
            provider_metadata={},
            usage=CreditCost(credits=1, unit="api_credit", basis="basic depth"),
        )
        self.assertNotIsInstance(response, SearchResult)

    def test_usage_state_fields_and_best_available_remaining(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(UsageState)],
            [
                "provider",
                "quota",
                "quota_kind",
                "quota_period",
                "plan",
                "local_credits_used",
                "local_credits_remaining_estimate",
                "provider_reported_remaining",
                "last_reconciled",
                "consecutive_failures",
                "cooldown_until",
                "last_request_at",
                "latency_ms_recent",
                "reservations",
                "expires_at",
                "next_slot_at",
            ],
        )

        def make(*, provider_reported_remaining, local_estimate) -> UsageState:
            return UsageState(
                provider="tavily",
                quota=1000,
                quota_kind=QuotaKind.MONTHLY,
                quota_period="2026-08",
                plan="free",
                local_credits_used=50,
                local_credits_remaining_estimate=local_estimate,
                provider_reported_remaining=provider_reported_remaining,
                last_reconciled=None,
                consecutive_failures=0,
                cooldown_until=None,
                last_request_at=None,
                latency_ms_recent=[],
                reservations=[],
                expires_at=None,
                next_slot_at=None,
            )

        # Prefers the provider-reported figure when present...
        self.assertEqual(
            make(provider_reported_remaining=17, local_estimate=950).best_available_remaining,
            17,
        )
        # ...else falls back to the local reconciled estimate.
        self.assertEqual(
            make(
                provider_reported_remaining=None, local_estimate=950
            ).best_available_remaining,
            950,
        )

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(InvalidRequest, SertchiError))
        self.assertTrue(issubclass(InvalidRequest, ValueError))
        with self.assertRaises(InvalidRequest):
            raise InvalidRequest("bad search_type")


if __name__ == "__main__":
    unittest.main()
