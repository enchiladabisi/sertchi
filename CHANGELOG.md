# Changelog

What changed in Sertchi, newest first.

## 2026-10-08

### Added

- **Brave Search as a seventh provider**, for both web and news searches.
  It runs on Brave's free $5 monthly credit, which is about 1,000
  searches. Sertchi tracks that credit like the others and stops before
  the limit. Set it up with `python3 -m sertchi keys set brave`.
  Brave sits between Exa and Tavily in the fixed provider order.

### Changed

- **Keenable is capped at half of all picks.** `search()` still chooses a
  provider at random, weighted by how many searches each account has
  left. Keenable's 100,000 monthly searches used to win about 96% of
  draws, so the other accounts were almost never used. Now Keenable gets
  at most 50%, and the other five paid providers split the rest by what
  each has left, recalculated on every search. The cap is the same for
  web and news searches. DuckDuckGo is still never part of the draw and
  is only tried last. Serper's one-time allowance still counts at a
  quarter of its size, so its share keeps shrinking as it runs down.

  With the balances at the time of the change, the odds for the first
  provider tried in a web or news search were:

  | Provider | Before | After |
  |---|---|---|
  | Keenable | 95.8% | 50.0% |
  | Exa | 1.2% | 14.3% |
  | Brave | 1.0% | 11.4% |
  | Tavily | 0.9% | 11.4% |
  | Serper | 0.6% | 7.2% |
  | Firecrawl | 0.5% | 5.7% |

  The cap is a ceiling, not a target. If Keenable's share drops below
  half because its monthly pool runs low, that lower share stands. If it
  is the only paid provider available, it is not held back.
  `multi_search()` is unchanged.

## 2026-10-07

- First public release.
