# livetennisapi-haystack

[Haystack](https://haystack.deepset.ai) 2.x integration for the
[Live Tennis API](https://livetennisapi.com): live scores, matches, players, head-to-heads,
the 1968-2022 results archive, rankings and in-play statistics across ATP, WTA, Challenger,
ITF and juniors — as Haystack `Document`s for RAG and agent pipelines.

[![ci](https://github.com/livetennisapi/livetennisapi-haystack/actions/workflows/ci.yml/badge.svg)](https://github.com/livetennisapi/livetennisapi-haystack/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/livetennisapi-haystack.svg)](https://pypi.org/project/livetennisapi-haystack/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://spdx.org/licenses/MIT.html)

Every component returns `Document`s whose `content` is a clean human-readable summary and
whose `meta` carries the structured fields — directly usable in prompts, document stores and
agent tools. Built on the official
[`livetennisapi`](https://pypi.org/project/livetennisapi/) Python client (retries, error
mapping, typed models) — no hand-rolled HTTP.

## Installation

```bash
pip install livetennisapi-haystack
```

Grab a free API key at <https://livetennisapi.com/subscribe/free> and export it — the
components read `LIVETENNISAPI_KEY` by default and never accept a plain-string key:

```bash
export LIVETENNISAPI_KEY="twjp_your_key_here"
```

## Quickstart

```python
from livetennisapi_haystack import LiveTennisMatchFetcher

fetcher = LiveTennisMatchFetcher()          # key from LIVETENNISAPI_KEY
result = fetcher.run(status="live", limit=5)
for doc in result["documents"]:
    print(doc.content)
    # e.g. "Carlos Alcaraz (ESP, #2) vs Jannik Sinner (ITA, #1) — match at Wimbledon,
    #       grass court, round QF, best of 5. Live now. Score: sets 1-1, games 6-4, 3-6,
    #       2-1, points 30-15. Carlos Alcaraz (ESP, #2) is serving."
```

### In a pipeline (runnable with only `LIVETENNISAPI_KEY`)

```python
from haystack import Pipeline

from livetennisapi_haystack import LiveTennisMatchFetcher, LiveTennisPlayerSearch

pipe = Pipeline()
pipe.add_component("matches", LiveTennisMatchFetcher(limit=5))
pipe.add_component("players", LiveTennisPlayerSearch(limit=3))

result = pipe.run({"matches": {"status": "live"}, "players": {"query": "alcaraz"}})
for doc in result["matches"]["documents"] + result["players"]["documents"]:
    print("-", doc.content)
```

### RAG over live scores

```python
from haystack import Pipeline
from haystack.components.builders.chat_prompt_builder import ChatPromptBuilder
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage

from livetennisapi_haystack import LiveTennisMatchFetcher

prompt_template = [
    ChatMessage.from_system("You are a tennis commentator."),
    ChatMessage.from_user(
        "Current matches:\n"
        "{% for document in documents %}{{ document.content }}\n{% endfor %}\n"
        "Answer the following question: {{ query }}\nAnswer:"
    ),
]

pipe = Pipeline()
pipe.add_component("matches", LiveTennisMatchFetcher(limit=10))
pipe.add_component("prompt_builder", ChatPromptBuilder(template=prompt_template, required_variables={"query", "documents"}))
pipe.add_component("llm", OpenAIChatGenerator(model="gpt-4o-mini"))
pipe.connect("matches.documents", "prompt_builder.documents")
pipe.connect("prompt_builder.prompt", "llm.messages")

query = "Who is closest to winning right now?"
result = pipe.run({"matches": {"status": "live"}, "prompt_builder": {"query": query}})
print(result["llm"]["replies"][0].text)
```

A complete runnable script lives at [`examples/live_demo.py`](examples/live_demo.py).

## Components

| Component | What it fetches | API endpoint(s) | Tier |
|---|---|---|---|
| `LiveTennisMatchFetcher` | Live / upcoming / completed matches, one match by id; filters: `tour`, `player`, `country`, `from_`/`to` | `/matches`, `/matches/{id}` | FREE (`status="completed"` listings: BASIC or any History plan) |
| `LiveTennisPlayerSearch` | Player search by name, ranked players first | `/players` | FREE |
| `LiveTennisH2HFetcher` | Head-to-head between two players — results archive (1968-2022) + current matches (2023-now) in one record | `/h2h` | BASIC |
| `LiveTennisArchiveFetcher` | The results archive: 1,485,752 matches 1968-2022 (`mode="matches"`), player bios (`mode="players"`), career aggregates (`mode="career"`) | `/history/archive/*` | BASIC |
| `LiveTennisRankingsFetcher` | A published ranking table (`atp`, `wta`, `itf_jt`, `itf_mt`, `itf_wt`), optionally as of a past week | `/rankings` | PRO |
| `LiveTennisMatchStatisticsFetcher` | In-play statistics: aces, double faults, serve split, hold/break %, break points | `/matches/{id}/statistics` | ULTRA |

All tour-filterable components accept `tour` values `"atp"`, `"wta"`, `"challenger"`,
`"itf"` and `"juniors"`; each value covers its singles and doubles draws.

## Quotas

| Tier | Requests/min | Requests/day | Price |
|---|---|---|---|
| FREE | 30 | 100 | $0 |
| BASIC | 60 | 1,000 | $9.99/mo |
| PRO | 300 | 10,000 | $29.99/mo |
| ULTRA | 600 | 500,000 | $99.99/mo |

At 100/day, poll no faster than every ~15 minutes on a free key; for an always-on dashboard,
BASIC is the tier to recommend. Full details at <https://docs.livetennisapi.com>.

## Authentication

The components resolve the key through Haystack's `Secret` (from `LIVETENNISAPI_KEY` by
default) and hand it to the official client, which sends it as an `Authorization: Bearer`
header — the API's preferred scheme (`X-API-Key` and `?token=` also exist for clients that
cannot set headers). Serialized pipelines carry only the environment-variable reference,
never the key value.

## Behavior worth knowing

- **403 tier wall**: when your key is valid but the plan does not unlock an endpoint, the
  component returns a single readable `Document` (tagged `meta["error"] = "upgrade_required"`)
  instead of raising — an agent can tell the user; a RAG pipeline can filter it out. The case
  you will actually hit: `status="completed"` listings return 403 on a free key — they need
  the BASIC tier ($9.99/mo) or any History plan
  (<https://livetennisapi.com/subscribe/upgrade>). `status="live"` / `"upcoming"` and
  single-match fetches via `match_id` (even for a completed match) work on the free tier.
- **429s**: the official client transparently retries the per-minute window (and if it still
  surfaces, the component fails loud — that is a transient error). The two NON-retryable
  shapes become readable Documents instead: the **daily cap** (tagged
  `meta["error"] = "rate_limited"`, with `resets_at` — the absolute instant the day quota
  resets, derived from a local midnight) and the **abuse throttle** (tagged
  `"abuse_throttled"`, with `retry_at_epoch` — a 24-hour block for chronic over-cap clients;
  fix the retry loop, retrying is what earns it).
- **Ambiguous names**: the name-keyed endpoints (`/h2h`, archive career) refuse a fragment
  matching more than one player; the component turns that into a Document tagged
  `meta["error"] = "ambiguous_name"` carrying the candidate list, so an agent can ask which
  one was meant.
- **Sparse data is normal**: `score.server` is nullable (between points the feed may not
  know who serves next — the summary simply omits the serving sentence), doubles teams have
  no individual rankings, points are strings (`"0"`, `"15"`, `"30"`, `"40"`, `"AD"`), and
  archive-era fields (`stats` before 1991, per-match dates) are honestly `None`. The
  components tolerate all of it and render only what exists.
- **Serialization**: every component implements `to_dict`/`from_dict`; the API key is stored
  as a `Secret` environment-variable reference, never as a value, so pipelines serialize
  safely to YAML. Note that Haystack 3.0 refuses to deserialize third-party components
  unless their module is allow-listed, so reload pipelines with
  `Pipeline.loads(yaml_str, allowed_modules=["livetennisapi_haystack.match_fetcher", ...])`
  (or `haystack.core.serialization.allow_deserialization_module(...)`).
- **Sync only** for now: `run()` — no `run_async` yet, although the official client has an
  async twin. Planned.

## Links

- Docs: <https://docs.livetennisapi.com>
- Free API key: <https://livetennisapi.com/subscribe/free>
- Discord: <https://discord.gg/f8WUZHgDm6>
- GitHub org: <https://github.com/livetennisapi>

## Development

```bash
pip install -e . pytest ruff
pytest                    # unit tests, fully mocked, no network
ruff check src tests examples
sh scripts/truthcheck.sh  # product-facts pin (also runs in CI)
LIVETENNISAPI_KEY=... pytest -m integration   # live tests, needs a key
```

## Affiliate program

Know developers who need tennis data? The [affiliate program](https://affiliates.livetennisapi.com/program) pays 51% recurring commission for the life of every referred subscription — 30-day cookie, and the people you refer get 10% off.

## License

`livetennisapi-haystack` is distributed under the terms of the
[MIT](https://spdx.org/licenses/MIT.html) license.
