# Integrating Sertchi into a project

This guide is for anyone wiring Sertchi in as a project's search layer,
whether you're replacing a hand-rolled search module or adding search to a
project from scratch. It assumes you've already installed Sertchi and set up
keys as described in the [README](../README.md). The README covers *using*
Sertchi; this guide covers *integrating* it cleanly: where to draw the line
between Sertchi and your code, how to handle its errors, and how to keep
your test suite from spending real search credits.

If you only read one section, read §6, "Mistakes not to repeat". Both
mistakes there are easy to make and expensive when they land silently.

---

## 1. What Sertchi is, in one paragraph

Sertchi is a library, not a service. There is no server to start and no
daemon to keep alive. You import it and call `sertchi.search(query,
search_type)` or `sertchi.multi_search(...)`. `search()` tries the available
providers in a weighted-random order (weighted by searches left on each
account, with one-time grants discounted and DuckDuckGo always last), stops
at the first provider that returns rows, and hands back one normalized
`SearchResult` (`status`, `provider_used`, `attempts`, `results`,
`limitations`). Provider selection, API-key gating, quota accounting, rate
pacing and fallback are entirely Sertchi's job. Your project should never
see a provider-specific HTTP call, endpoint or rate limit. If you find
yourself writing a per-provider `except` clause or a provider-specific retry
loop, you're re-implementing something Sertchi already does. Route it
through `search()` / `multi_search()` instead.

Everything *downstream* of search is **not** Sertchi's job: fetching a page
a result links to, extracting readable text from it, ranking or selecting
among results, deduplicating your own accumulated evidence. Sertchi returns
search-result rows (title, url, snippet, published date where available)
and never fetches pages. That split is the most important architectural
fact for an integration: draw your seam at "I call `sertchi.search()`, I
get rows back," and own everything on both sides of that call yourself.

## 2. Two ways to place the code

**Linked (recommended).** Keep one copy of Sertchi on the machine and give
each consuming project a relative symlink to it:

```sh
cd path/to/your-project
ln -s ../sertchi sertchi     # adjust the relative path to your layout
```

Your project then imports it like any local package (`import sertchi`, or
`from sertchi import search, Provider, SearchType`) with no `sys.path`
changes. Python follows the symlink transparently.

The benefit is one always-current source with nothing to keep in sync. The
costs:

- A symlink doesn't survive cloning your project onto another machine.
  Anyone else checking out your project needs their own copy of Sertchi at
  the linked path.
- **A write into `your-project/sertchi/` is a write into the one shared
  copy**, which every other linked project also uses. Keep integration work
  strictly on your side of the seam. If you find a bug in Sertchi itself,
  fix it in Sertchi's own folder deliberately and run Sertchi's test suite,
  rather than patching it through the link.

**Copied.** A full copy of the Sertchi source inside your project, for a
project that must be self-contained. This works, but every copy drifts from
the others as Sertchi changes, so prefer the link unless you really need
independence.

Either way, keys and usage tracking stay **shared, whatever code layout you
choose**: `~/.config/sertchi/{keys.json,usage.json}`, one store used by every
copy of Sertchi on the machine (see [LOCAL-FILES.md](../LOCAL-FILES.md)). Your
project never creates its own key store, never reads a provider key from an
environment variable, and never keeps its own usage ledger. That has a sharp
edge for testing; see §5.

## 3. Provider keys

Decide which providers the project should use, then set each key once, from
the folder *above* `sertchi/`:

```sh
python3 -m sertchi keys set keenable
python3 -m sertchi keys set exa
python3 -m sertchi keys set tavily
python3 -m sertchi keys list        # masked output only
```

The available providers are `keenable`, `exa`, `tavily`, `firecrawl`,
`serper` and `ddg`. DuckDuckGo needs no key. Whatever is configured takes
effect automatically: Sertchi silently drops any provider with no key, so a
partly configured machine degrades gracefully to the configured subset, down
to DuckDuckGo alone if nothing else is set up. **Adding a provider later
needs no code change.** Running `keys set` for a new provider activates it
for every consumer immediately. So don't hard-code assumptions about which
providers are live; let Sertchi decide.

Cost consequence: once any key is configured, **every call that reaches that
provider draws down that account's allowance.** That includes calls made by
your test suite. See §5 and §6.

## 4. Writing the seam

### 4.1 Minimum viable call

```python
from sertchi import search as sertchi_search, SearchType, ProviderAuthError

result = await sertchi_search(
    query, SearchType.WEB,
    providers=None,          # None = every configured provider; or a Provider subset
    client=web_client,       # reuse your own httpx.AsyncClient rather than
                             # letting Sertchi open a fresh connection per call
    max_results=5,           # -> Keenable
    numResults=5,            # -> Exa
    limit=5,                 # -> Firecrawl and DDG
    num=5,                   # -> Serper; Tavily has no count setting at all
)
```

Extra keyword arguments are passed to every provider, and each provider
reads only the name it knows. That's why "results per query" takes four
spellings. Code that isn't async can use `sertchi.sync.search()` instead,
with the same arguments minus `client`.

`SearchResult` fields worth knowing before you write your mapping layer:

- `result.status`, a `SearchStatus`: `OK`, `EMPTY` or `UNAVAILABLE`.
  `PARTIAL` is declared but never returned today, so don't write a branch
  for it.
- `result.provider_used`: which provider answered (`search()`), or a
  comma-joined list of contributors (`multi_search()`). `None` when nothing
  came back.
- `result.results`: a list of `ResultItem` (`title`, `url`, `snippet`,
  `domain`, `published_date`, `date_confidence`). `published_date` is `None`
  when the provider didn't report one. DuckDuckGo never does.
- `result.limitations`: human-readable strings explaining any degradation
  (no key, unsupported search type, quota reserve reached, throttled).
  Surface these in your own degraded-result reporting rather than writing
  your own messages from `attempts`.
- `result.attempts`: the full per-provider record (`AttemptOutcome`:
  `SUCCESS`, `ERROR`, `EMPTY`, `SKIPPED_QUOTA`, `SKIPPED_BACKOFF`,
  `THROTTLED`). Log it at debug level, but don't build user-facing behavior
  on individual outcomes. That's exactly the provider-awareness your project
  shouldn't have.

### 4.2 What raises, and what doesn't

`sertchi.search()` raises two exceptions that your seam must let surface as
real errors, not swallow into "no results":

- `InvalidRequest`: a blank or non-string query, a bad `search_type`, or an
  unknown provider name.
- `ProviderAuthError`: **a rejected API key is a configuration error, not a
  search miss.** Sertchi deliberately does not fall through to the next
  provider on a bad key, because then a typo'd key could hide forever
  behind a working fallback while you quietly got worse results. Catch it
  separately from operational failures, so a broken key shows up as a real
  problem.

Everything else (timeouts, rate limits, blocks, malformed responses) Sertchi
has *already* handled: it caught the failure, recorded the attempt and moved
on to the next provider. By the time `search()` returns, an operational
failure is just an `UNAVAILABLE` or `EMPTY` result, with `limitations` and
`attempts` explaining why. Your seam doesn't need retry logic of its own.

A typical seam looks like this:

```python
from dataclasses import dataclass

from sertchi import search as sertchi_search, SearchType, ProviderAuthError
from sertchi.types import SearchStatus

MAX_RESULTS = 5


@dataclass
class SearchOutcome:
    query: str
    rows: tuple
    provider: str
    unavailable: bool
    error: str = ""


async def search_one(query: str, *, client=None) -> SearchOutcome:
    try:
        result = await sertchi_search(
            query, SearchType.WEB,
            client=client,
            max_results=MAX_RESULTS, numResults=MAX_RESULTS,
            limit=MAX_RESULTS, num=MAX_RESULTS,
        )
    except ProviderAuthError as exc:
        # surfaced distinctly -- not folded into a routine search miss
        return SearchOutcome(query, (), "none", unavailable=True,
                             error=f"configuration: {exc}")
    except Exception as exc:                      # incl. InvalidRequest
        return SearchOutcome(query, (), "none", unavailable=True, error=str(exc))
    rows = tuple({"title": r.title, "url": r.url, "snippet": r.snippet,
                  "published_date": r.published_date or ""}
                 for r in result.results)
    return SearchOutcome(query, rows, result.provider_used or "none",
                         unavailable=(result.status is SearchStatus.UNAVAILABLE),
                         error="; ".join(result.limitations))
```

If your project has a "never raise, always degrade" rule for its pipeline,
this function is where that rule meets Sertchi's "raise on real problems"
rule. Convert Sertchi's exceptions into your own degraded-result shape here
and nowhere deeper: one seam function, one place that knows Sertchi can
raise.

### 4.3 What to delete from an existing search module

If you're replacing an existing single-provider search module rather than
starting fresh, delete these instead of keeping them "just in case":

- **Provider-specific rate limiting**: a "1.1 s between DuckDuckGo
  requests" floor, a retry-with-backoff loop, a 202/403/429 status handler.
  Sertchi already paces every provider that needs it, and a copy in your
  project will drift out of sync the moment Sertchi's pacing changes.
- **Delay settings between queries** in your own config (`query_delay_ms`
  or similar). Pacing is Sertchi's job now. A leftover delay is dead weight
  that fights Sertchi's own pacing.
- **Fine-grained per-provider error codes.** Keep at most "search
  unavailable", "search returned nothing" and "search disabled". Sertchi's
  `attempts` already carries the per-provider detail, and a parallel
  taxonomy in your project becomes a second source of truth that falls
  behind as providers are added.
- **Direct provider calls.** A URL literal like
  `https://html.duckduckgo.com/html/` or an API-key header assembled by hand
  gets replaced, not kept alongside Sertchi as a "backup path".

Keep, unchanged, anything genuinely downstream of search: page fetching,
HTML-to-text extraction, source scoring, pooling and deduplicating your own
results across queries, and safety checks on page fetches. None of that is
Sertchi's concern.

## 5. Testing: inject the search function

Sertchi's key store and usage ledger have **no environment-variable
override**. They always live at `~/.config/sertchi/`. One consequence is
unavoidable: on a machine with provider keys configured, a test that calls
your search seam unmodified **will** reach real providers and spend real
credits.

**The pattern to use:** your search-facing function takes an optional
search function, which defaults to the real one:

```python
from collections.abc import Awaitable, Callable

SearchFn = Callable[..., Awaitable[SearchOutcome]]

async def retrieve(spec, *, search: SearchFn | None = None):
    run_search = search or search_one
    ...
```

Every unit test of the pipeline above `search_one` passes a fake `search=`
and never touches Sertchi. That's the normal test path: cheap, hermetic and
fast.

In the narrow case where a test wants the real `sertchi.search()` code path
(real provider selection, a real `ProviderAuthError`, the real adapter
code) without touching your real config or spending a credit, point
Sertchi's config paths at a temporary directory, the same way Sertchi's own
suite does (`tests/test_keys.py`, `tests/test_orchestrator.py`):

```python
import tempfile
from pathlib import Path

from sertchi import keys, usage


def setUp(self):
    self._tmpdir = tempfile.TemporaryDirectory()
    sandbox = Path(self._tmpdir.name) / "sertchi"
    self._real_keys = (keys.CONFIG_DIR, keys.KEYS_PATH)
    keys.CONFIG_DIR = sandbox
    keys.KEYS_PATH = sandbox / "keys.json"
    self._real_usage = (usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH)
    usage.CONFIG_DIR = sandbox
    usage.USAGE_PATH = sandbox / "usage.json"
    usage.LOCK_PATH = sandbox / "usage.lock"

def tearDown(self):
    keys.CONFIG_DIR, keys.KEYS_PATH = self._real_keys
    usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH = self._real_usage
    self._tmpdir.cleanup()
```

Repoint **all** the path constants together. Changing `CONFIG_DIR` alone
does nothing, because `KEYS_PATH`, `USAGE_PATH` and `LOCK_PATH` are computed
once at import time. A test using this pattern must still supply its own
`httpx.MockTransport` (or equivalent): sandboxing the config stops a credit
spend, but it doesn't stop a real network connection.

If your project has a "dependency injection, never monkeypatching" rule,
document this pattern as the deliberate exception wherever you use it, so
it doesn't read as a silent violation.

## 6. Mistakes not to repeat

Both of these happened in a real integration.

**Mistake 1: adding the setting that enables real calls before the test
seam that prevents them.** The integration was built in phases. First it
wrote a seam function that read a config key which didn't exist yet. Then a
later phase added that config key, and a phase after *that* added the
`search=` injection parameter. In the gap, the missing config key raised a
`KeyError` that the seam's own catch-all quietly swallowed. So the test
suite *looked* hermetic, but only by accident: every call was failing
before it reached the network. When the config key was added, every test on
the default path went live at once. One test run spent about 158 real
search credits (roughly 16% of a free monthly allowance) in about 44
seconds. The only symptom was the suite's runtime jumping from 5.5 s to
44 s, and it went unnoticed until someone looked at the ledger.

**The general lesson:** when a plan includes both "add the seam that
prevents live calls" and "add the config that makes live calls possible",
**the seam goes first, always**, even if that breaks an otherwise sensible
order of phases. Order the work by *when money can start moving*, not by
which file naturally comes first. A catch-all error handler is exactly where
this kind of bug hides: it's designed to make failures invisible, so it also
hides the moment an accidental failure *stops* happening. Before and after
any change that could newly reach a live provider, check the ledger:

```sh
python3 -c "import json, pathlib; print(json.loads((pathlib.Path.home() / '.config/sertchi/usage.json').read_text()))"
```

Or just watch the suite's runtime. A jump from single-digit seconds to tens
of seconds, in a suite that shouldn't touch the network, is the cheap,
immediate tell.

**Mistake 2: assuming a test machine without keys only ever uses
DuckDuckGo.** It's tempting to reason that because DuckDuckGo needs no key,
tests will harmlessly fall through to it and never spend anything. That's
only true while *no* provider key is configured at all. The moment one key
exists in `~/.config/sertchi/keys.json`, the assumption is false for every
consumer on that machine, including tests written before any key existed.
Don't reason about test safety from "DuckDuckGo needs no key". Ask instead:
"does this test path ever reach `sertchi.search()` without an injected fake
or a sandboxed config?"

## 7. Sharp edges worth knowing before you start

- **The `types.py` shadow.** Run Sertchi's commands and tests from the
  folder *above* `sertchi/`, never from inside it. Inside it, Sertchi's own
  `types.py` hides Python's built-in `types` module and imports break in
  confusing ways. When launching Sertchi as a subprocess, `python3 -P`
  keeps the current directory off the import path.
- **`SearchStatus` isn't exported from the top-level package.** Use `from
  sertchi.types import SearchStatus`. `from sertchi import SearchStatus`
  fails, even though `SearchType` and `Provider` import fine from
  `sertchi`.
- **Not every provider takes a result count.** Tavily has no count setting
  and always returns its own default number. Don't assume every provider
  obeys a uniform "results per query" cap. Make sure your own selection or
  truncation still behaves when a provider ignores it.
- **DuckDuckGo results never carry `published_date`.** If your project
  filters or displays by date, treat a missing date as normal, not a bug.
- **A rejected key doesn't fall through, and Serper's version of that is
  blunt.** Serper answers a bad *or missing* key with HTTP 403, the same
  code it uses for account and quota problems. So an exhausted Serper
  account can surface as `ProviderAuthError` even when the key itself is
  fine. If you show users a message for `ProviderAuthError`, phrase it as
  "check your Serper key or account status", not "your key is wrong".
- **`multi_search()` spends a search on every eligible provider in one
  call.** It's never the right default. Use it only when you really want
  several providers' answers to one query, and expect the cost multiplier.

## 8. Checklist for a new integration

1. Decide which providers the project should use, and run `python3 -m
   sertchi keys set <provider>` for each, from the folder above `sertchi/`.
2. Link Sertchi into the project (`ln -s ../sertchi sertchi`), or copy it if
   the project must be self-contained (§2).
3. Write one seam function that calls `sertchi.search()` or
   `multi_search()`, catches `ProviderAuthError` separately from everything
   else, maps `ResultItem` rows into your project's own shape, and, if your
   project requires it, never raises past its own boundary.
4. Give that function an optional, injectable `search=` parameter **before**
   adding any config that could make it reach a real provider.
5. Delete the provider-specific machinery listed in §4.3 from any code
   being replaced: rate floors, retry loops, per-provider error codes,
   direct endpoint URLs.
6. Write tests against the injected fake by default. Add the sandboxed-config
   pattern (§5) only for the one or two tests that deliberately want the
   real `sertchi.search()` path.
7. Before the first full test run on a machine with real keys, snapshot
   `~/.config/sertchi/usage.json`, run the suite and compare. Confirm zero
   unexpected spend before you trust the suite as hermetic.
8. Document the boundary in your project's own docs: where the link or copy
   lives, where the seam function is, and the rule that search is Sertchi's
   problem and everything downstream is yours.
