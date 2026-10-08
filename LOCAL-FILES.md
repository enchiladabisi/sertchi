# Local files Sertchi needs (not in this repo)

Sertchi keeps two files outside the repository, in `~/.config/sertchi/`.
They hold your secrets and your machine's usage counts, so they are listed in
`.gitignore` and never committed. Sertchi creates both for you.

| File | What it holds | How it gets created |
|---|---|---|
| `keys.json` | Your search-provider API keys (Keenable, Exa, Brave, Tavily, Firecrawl, Serper) | `python3 -m sertchi keys set <provider>`, which asks for the key on a hidden prompt |
| `usage.json` | How many credits each provider account has used, so Sertchi stays inside free-tier limits | Automatically, on the first search |

Without `keys.json`, Sertchi still runs. It skips every provider that needs a
key and falls back to DuckDuckGo, which needs none.
