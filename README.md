# livetennisapi-haystack

[Haystack](https://haystack.deepset.ai) 2.x integration for the
[Live Tennis API](https://livetennisapi.com): live scores, matches and players as Haystack
`Document`s for RAG and agent pipelines.

- **`LiveTennisMatchFetcher`** — live / upcoming / completed matches (optionally one match by
  id, optionally filtered by tour) as `Document`s. `content` is a clean human-readable match
  summary; `meta` carries the structured fields (ids, players, sets/games/points, server,
  winner).
- **`LiveTennisPlayerSearch`** — player search by name, ranked players first, same
  `Document` shape.

Built on the official [`livetennisapi`](https://pypi.org/project/livetennisapi/) Python
client (retries, error mapping, typed models) — no hand-rolled HTTP.

## Installation

```bash
pip install livetennisapi-haystack
```

You need a Live Tennis API key (free tier: 1000 requests/day, 30/min). Export it as an
environment variable — the components read `LIVETENNISAPI_KEY` by default and never accept a
plain-string key:

```bash
export LIVETENNISAPI_KEY="your-key"
```

## Usage

### Standalone

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

## Behavior worth knowing

- **403 tier wall**: when your key is valid but the plan does not unlock an endpoint, the
  component returns a single readable `Document` (tagged `meta["error"] = "upgrade_required"`)
  instead of raising — an agent can tell the user; a RAG pipeline can filter it out. All
  other errors (bad key, network down, rate limit) still raise the official client's typed
  exceptions.
- **Sparse data is normal**: `score.server` is nullable (between points the feed may not
  know who serves next — the summary simply omits the serving sentence), doubles teams have
  no individual rankings and a null `data_completeness`, and points are strings
  (`"0"`, `"15"`, `"30"`, `"40"`, `"AD"`). The components tolerate all of it.
- **Serialization**: both components implement `to_dict`/`from_dict`; the API key is stored
  as a `Secret` environment-variable reference, never as a value, so pipelines serialize
  safely to YAML.
- **`tour` filter**: the API's documented `tour` query parameter is not yet exposed by
  `livetennisapi` 1.0.2's `list_matches()`, so the component routes that one call through
  the official client's transport layer (same auth/retries/error mapping).
- **Sync only** for now: `run()` — no `run_async` yet, although the official client has an
  async twin. Planned.

## Parameters

`LiveTennisMatchFetcher(api_key, status="live", tour=None, limit=10, base_url=None, timeout=30.0)`
— `status`/`tour`/`limit` can be overridden per `run()`, and `run(match_id=...)` fetches a
single match. `LiveTennisPlayerSearch(api_key, limit=10, base_url=None, timeout=30.0)` —
`run(query, limit=None)`.

## Development

```bash
pip install -e . pytest ruff
pytest                    # unit tests, fully mocked, no network
ruff check src tests examples
LIVETENNISAPI_KEY=... pytest -m integration   # live tests, needs a key
```

## License

`livetennisapi-haystack` is distributed under the terms of the
[MIT](https://spdx.org/licenses/MIT.html) license.
