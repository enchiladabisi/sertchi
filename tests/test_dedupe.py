"""Phase 2: dedupe.py owns URL identity -- clean_url/canonicalize/
registrable_domain/dedupe_key, plus the cross-provider merge for
multi_search() (SKETCH.md §2.7 decision 2, §6).

Run from ~/Documents/CODE via:
    python3 -m unittest discover -s sertchi/tests -t . < /dev/null
"""

from __future__ import annotations

import unittest

from sertchi.dedupe import canonicalize, clean_url, dedupe_key, merge, registrable_domain
from sertchi.types import CreditCost, DateConfidence, Provider, ProviderResponse, ResultItem


def _row(provider: str, rank: int, url: str, title: str = "t") -> ResultItem:
    return ResultItem(
        title=title, url=url, snippet="s", domain="example.com",
        registrable_domain="example.com", published_date=None,
        date_confidence=DateConfidence.NONE, provider=provider, provider_rank=rank,
    )


class CleanUrlTest(unittest.TestCase):
    def test_unwraps_ddg_redirect(self) -> None:
        self.assertEqual(
            clean_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x"),
            "https://example.com/a",
        )

    def test_prefixes_https_on_protocol_relative(self) -> None:
        self.assertEqual(clean_url("//example.com/a"), "https://example.com/a")

    def test_passes_through_plain_url_unchanged(self) -> None:
        self.assertEqual(clean_url("https://example.com/a"), "https://example.com/a")

    def test_empty_href_returns_empty_string(self) -> None:
        self.assertEqual(clean_url(""), "")


class CanonicalizeTest(unittest.TestCase):
    def test_strips_tracking_but_keeps_identity(self) -> None:
        self.assertEqual(
            canonicalize("HTTP://Example.COM:80/a?utm_source=x&id=7#frag"),
            "http://example.com/a?id=7",
        )

    def test_drops_https_default_port(self) -> None:
        self.assertEqual(canonicalize("https://example.com:443/a"), "https://example.com/a")

    def test_unrecognised_param_is_kept(self) -> None:
        # A stripped ?id= can change what page is returned -- canonicalize
        # must never touch a parameter it doesn't recognise.
        self.assertIn("id=7", canonicalize("https://example.com/a?id=7"))


class RegistrableDomainTest(unittest.TestCase):
    def test_two_char_special_tld(self) -> None:
        self.assertEqual(registrable_domain("https://news.bbc.co.uk/x"), "bbc.co.uk")

    def test_ordinary_tld_collapses_subdomains(self) -> None:
        self.assertEqual(registrable_domain("https://a.b.example.com/"), "example.com")

    def test_two_label_host_returned_as_is(self) -> None:
        self.assertEqual(registrable_domain("https://example.com/"), "example.com")


class DedupeKeyTest(unittest.TestCase):
    def test_www_and_scheme_collapse(self) -> None:
        self.assertEqual(
            dedupe_key("http://www.x.com/a"),
            dedupe_key("https://x.com/a"),
        )

    def test_query_params_still_distinguish(self) -> None:
        self.assertNotEqual(
            dedupe_key("https://x.com/a?id=1"),
            dedupe_key("https://x.com/a?id=2"),
        )

    def test_trailing_slash_collapses(self) -> None:
        # Regression: a live multi_search (2026-08-20) returned
        # superfastpython.com/asyncio-timeout from one provider and
        # .../asyncio-timeout/ from another; both survived the merge.
        self.assertEqual(
            dedupe_key("https://superfastpython.com/asyncio-timeout"),
            dedupe_key("https://superfastpython.com/asyncio-timeout/"),
        )

    def test_trailing_slash_collapses_with_www_and_scheme(self) -> None:
        self.assertEqual(
            dedupe_key("http://www.x.com/a/"),
            dedupe_key("https://x.com/a"),
        )

    def test_bare_root_slash_is_preserved(self) -> None:
        # The trailing-slash strip must not reduce the path below "/".
        self.assertEqual(dedupe_key("https://x.com/"), dedupe_key("https://x.com"))
        self.assertTrue(dedupe_key("https://x.com/").endswith("x.com/"))

    def test_trailing_slash_does_not_conflate_distinct_paths(self) -> None:
        self.assertNotEqual(
            dedupe_key("https://x.com/a/"),
            dedupe_key("https://x.com/b/"),
        )


class MergeTest(unittest.TestCase):
    def test_round_robins_by_rank_across_providers_in_quality_order(self) -> None:
        tavily = ProviderResponse(
            provider="tavily",
            results=[_row("tavily", 1, "https://a.example/1"),
                     _row("tavily", 2, "https://a.example/2")],
            provider_metadata={}, usage=CreditCost(1, "api_credit", "basic"),
        )
        ddg = ProviderResponse(
            provider="ddg",
            results=[_row("ddg", 1, "https://b.example/1")],
            provider_metadata={}, usage=CreditCost(0, "none", "keyless"),
        )
        merged = merge([tavily, ddg], order=list(Provider))
        self.assertEqual(
            [(r.provider, r.provider_rank) for r in merged],
            [("tavily", 1), ("ddg", 1), ("tavily", 2)],
        )

    def test_collision_keeps_higher_ranked_provider_row(self) -> None:
        tavily = ProviderResponse(
            provider="tavily", results=[_row("tavily", 1, "https://www.x.com/a")],
            provider_metadata={}, usage=CreditCost(1, "api_credit", "basic"),
        )
        ddg = ProviderResponse(
            provider="ddg", results=[_row("ddg", 1, "https://x.com/a")],
            provider_metadata={}, usage=CreditCost(0, "none", "keyless"),
        )
        merged = merge([tavily, ddg], order=list(Provider))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].provider, "tavily")

    def test_empty_responses_yield_empty_merge(self) -> None:
        self.assertEqual(merge([], order=list(Provider)), [])


if __name__ == "__main__":
    unittest.main()
