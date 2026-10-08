# Sertchi

**One search box for six search engines.** You ask Sertchi a question, it
picks a search service that still has free searches left, and you get back
results in the same simple shape every time — no matter who actually
answered.

Think of it like a universal remote. Keenable, Exa, Tavily, Firecrawl,
Serper and DuckDuckGo all speak slightly different languages and all have
their own free limits. Sertchi speaks all of them, keeps track of how much
of each free allowance you've used, and quietly moves on to the next one if
one is down, busy, or used up. Your code only ever learns one way to search.

It's a small Python library, not a server. There's nothing to keep running.
You import it, call it, and get results.

---

## What you'll need

- **Python 3.12 or newer**
- **[httpx](https://www.python-httpx.org/)**, the one outside package Sertchi uses
- **API keys for whichever search services you want** (optional; see below)

That's it. Everything else comes with Python.

---

## Installing it

**1. Grab the code.**

```sh
cd ~/code            # or wherever you keep your projects
git clone https://github.com/enchiladabisi/sertchi.git
```

Keep the folder named `sertchi`. Python finds the library by that name.

**2. Install httpx**, whichever way you normally install Python packages:

```sh
pip install httpx                 # in a virtual environment, or
sudo pacman -S python-httpx       # Arch and friends, or
sudo apt install python3-httpx    # Debian/Ubuntu
```

**3. Let Python find it.** Sertchi gets imported as `sertchi`, so Python
needs to be able to see the folder that *contains* it (in this example,
`~/code`). Pick one:

- Run your scripts from that parent folder, or
- Point `PYTHONPATH` at it: `export PYTHONPATH=~/code`, or
- Drop a link to Sertchi inside your own project (see
  [Plugging it into your project](#plugging-it-into-your-project)).

> **The one gotcha:** don't run Sertchi commands from *inside* the
> `sertchi/` folder. It has a file called `types.py`, and Python will
> mistake it for its own built-in `types` module and fall over. Always run
> from the folder above it.

---

## Setting it up (your keys)

You can skip this whole step and Sertchi still works. With no keys it just
uses DuckDuckGo, which doesn't need one. But DuckDuckGo is the weakest of
the bunch and sometimes blocks you if you search too often, so a free key
or two makes a big difference.

Here's what each service gives you for free (as of late 2026; these
companies change their deals, so double-check):

| Service | Needs a key? | Free allowance Sertchi assumes | Good at |
|---|---|---|---|
| **Keenable** | yes | 100,000 searches a month | Huge free pool, so it handles most of your everyday searches |
| **Exa** | yes | $10 of credit a month (about 1,250 searches) | Very fresh news |
| **Tavily** | yes | 1,000 a month | Solid all-rounder |
| **Firecrawl** | yes | 1,000 credits a month | Good backup |
| **Serper** | yes | 2,500, one time only (never refills) | Google-style results |
| **DuckDuckGo** | no | free, but it rate-limits you | Last resort |

Sign up with whichever ones you like, then hand Sertchi each key. Run this
from the folder *above* `sertchi/`:

```sh
python3 -m sertchi keys set keenable
python3 -m sertchi keys set exa
```

It asks for the key on a hidden prompt, so nothing shows up while you
paste, and the key never ends up in your shell history. To check what
you've set up:

```sh
python3 -m sertchi keys list      # shows keys masked, like keen_***a1b2
python3 -m sertchi keys remove exa
```

Keys live in `~/.config/sertchi/`, outside the project folder, readable
only by you. Every project on your computer that uses Sertchi shares them,
so you only do this once. See [LOCAL-FILES.md](LOCAL-FILES.md) for what's
in there.

Add a key later and every project using Sertchi starts using that service
right away. You don't have to change any code.

---

## Using it

### Your first search

```python
from sertchi.sync import search

result = search("how do sourdough starters work")

for row in result.results:
    print(row.title)
    print(row.url)
    print(row.snippet)
    print()

print("Answered by:", result.provider_used)
```

### Searching the news

Ask for `"news"` and you get recent articles, often with a publish date:

```python
result = search("solar panel prices", "news")

for row in result.results:
    print(row.published_date, row.title)
```

Each row also has a `date_confidence` saying whether that date came
straight from the service (`"reported"`), is a best guess (`"inferred"`),
or is missing (`"none"`). Sertchi never passes off a guess as a real date.

### What you get back

Every search hands you the same thing, whoever answered:

- `result.results` is the list of hits. Each one has `title`, `url`,
  `snippet`, `domain`, `published_date` and `date_confidence`.
- `result.status` is `"ok"` (got results), `"empty"` (asked around, nobody
  found anything) or `"unavailable"` (nobody could be asked; maybe every
  free allowance is used up).
- `result.provider_used` tells you which service answered.
- `result.limitations` is a list of plain-English notes about anything that
  got skipped, like "tavily skipped: no API key configured".
- `result.attempts` lists every service Sertchi tried, in order, and how
  each one went.

Most services return about 10 hits. If you want fewer, just slice:
`result.results[:5]`.

### Asking everyone at once

`search()` stops at the first service that comes back with something.
That's what saves your free searches. If you'd rather hear from every
service you have a key for, and get one combined list with the duplicates
removed, use `multi_search()`:

```python
from sertchi.sync import multi_search

result = multi_search("best budget mechanical keyboard")
print(result.provider_used)   # e.g. "keenable,exa,tavily"
```

Heads up: this spends one search on *every* service it asks, so save it for
when you really want the wider net.

### Picking which services to use

Want to stick to certain services? Pass a list:

```python
from sertchi import Provider
from sertchi.sync import search

result = search("local election results", "news",
                providers=[Provider.EXA, Provider.TAVILY])

# Or force the free one:
result = search("python list comprehension", providers=[Provider.DDG])
```

### If your code is already async

The plain `sertchi.search()` and `sertchi.multi_search()` are the async
versions. Use those inside async code:

```python
import asyncio
import sertchi

async def main():
    result = await sertchi.search("rust vs go for cli tools")
    print(result.provider_used, len(result.results))

asyncio.run(main())
```

(The `sertchi.sync` versions refuse to run inside async code. They tell you
so instead of quietly misbehaving.)

### When something goes wrong

Mostly, nothing does. If a service is down, slow or out of free searches,
Sertchi just moves on to the next one. Two things *will* stop you, on
purpose:

- **A wrong or expired key** raises `ProviderAuthError`. Sertchi could
  quietly skip that service, but then you'd never find out your key is
  broken. Fix it with `keys set`.
- **A blank search** raises `InvalidRequest`.

```python
from sertchi import ProviderAuthError

try:
    result = search("whatever")
except ProviderAuthError as e:
    print("Check your keys:", e)
```

---

## How it takes care of your free searches

You don't have to babysit any of this. It just happens:

- **It tracks what you've used.** Sertchi keeps a tally for each service
  and stops using one before it hits the limit. It holds back a few
  searches as a cushion, in case two of your programs search at the exact
  same moment.
- **It spreads the load sensibly.** Each search goes to a service picked at
  random, but weighted by how many searches it has left. The service with
  the biggest free pile does most of the work, and the small ones stay
  saved for backup. Serper's one-time allowance gets used extra sparingly,
  since it never comes back.
- **DuckDuckGo goes last.** It only gets asked when every other option is
  out.
- **It backs off from broken services.** If one keeps failing, Sertchi
  gives it a break before trying it again.
- **It shares the tally across your programs.** Run three scripts at once
  and they all read and update the same count, so nobody spends a search
  somebody else already used.
- **It never pays.** Sertchi assumes every account is free and won't go
  past a free limit unless you specifically tell it you're on a paid plan.

---

## Plugging it into your project

The easy way is to put a link to Sertchi inside your project folder. Then
`import sertchi` just works, with no path fiddling:

```sh
cd ~/code/my-project
ln -s ../sertchi sertchi
```

One copy of Sertchi on your computer can serve all your projects this way,
and an update to it reaches all of them at once.

A few tips that'll save you grief:

- **Keep your tests from burning real searches.** This one bites people.
  If your tests call your code, and your code calls Sertchi, then your
  tests are spending real searches every run. Have your code accept the
  search function as an argument, and hand it a fake in tests:

  ```python
  from sertchi.sync import search as real_search

  def find_sources(topic, search=real_search):
      return [row.url for row in search(topic).results]

  # in your test:
  def fake_search(query, *args, **kwargs):
      ...  # return a pretend result
  find_sources("anything", search=fake_search)
  ```

- **Sertchi finds pages. It doesn't read them.** You get titles, links and
  short snippets. If you want the full text of a page, fetch that link
  yourself. That's on purpose, to keep Sertchi small and predictable.
- **Don't fix Sertchi through the link.** If you linked it in, editing
  `my-project/sertchi/` edits the real Sertchi for every project. Make
  changes in the actual `sertchi` folder on purpose, and run its tests.

---

## Using it from an AI assistant

Sertchi comes with a small MCP server. MCP is the plug-in system that tools
like Claude Code, OpenCode and other AI coding assistants use, so your AI
helper can search the web through Sertchi with your keys and your free
allowances.

It gives the assistant two tools, **web** and **news**. Each takes a search
term, plus an optional number of results and an optional list of services.

The assistant starts it on its own. You just tell it how:

```sh
PYTHONPATH=~/code python3 -P -m sertchi.mcp_server
```

(Swap `~/code` for the folder that contains `sertchi/`. The `-P` keeps that
`types.py` mix-up from biting here too.)

For Claude Code, that looks like:

```sh
claude mcp add --transport stdio --env PYTHONPATH=$HOME/code sertchi -- python3 -P -m sertchi.mcp_server
```

Other assistants want the same command and the same `PYTHONPATH` setting in
their own MCP config file.

---

## Things you could build with it

A few ideas to get you going:

- **Let your local chatbot look things up.** Running a model on your own
  machine? Search first, paste the top snippets into the prompt, and its
  answers stop being frozen in time.
- **A morning news digest.** A little script on a timer that runs a few
  `"news"` searches on topics you care about and emails or prints you the
  headlines.
- **A research helper.** Feed it a list of questions, collect the links for
  each, and save them into a notes file to read later.
- **Link suggestions for your notes.** Got a pile of notes in Obsidian or
  similar? Search each note's topic and suggest a few useful links to add.
- **Keeping tabs on a company or a product.** Search news for a name every
  day and flag anything new.
- **Give your AI coding assistant a web search** that runs on your own free
  allowances (see the MCP section).
- **Swap out the search code you already have.** If you've got a project
  that talks straight to one search service, point it at Sertchi instead
  and you get backups for free.

---

## Running the tests

The tests never touch the internet, so they're free and safe to run as
often as you like. From the folder *above* `sertchi/`:

```sh
python3 -m unittest discover -s sertchi/tests -t . < /dev/null
```

You should see a few hundred tests pass, with a handful skipped. The
skipped ones are checks that don't apply to every service, so that's
expected.

---

## License

Copyright (C) 2026 enchiladabisi

This project is free software, released under the GNU General Public
License v3.0 or later. The full text is in [LICENSE](LICENSE).

You are free to use it, study it, modify it, share it, and sell copies of
it. If you distribute modified versions, those versions must preserve the
same freedoms for their users.

In other words: do whatever, except don't try to claim private property in
the commons.
