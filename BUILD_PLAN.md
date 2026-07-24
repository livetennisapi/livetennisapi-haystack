# BUILD_PLAN — livetennisapi-haystack

> **Status (2026-07-24): ALL UNITS DONE.** BUILD-001..004 landed as commits 1-4; BUILD-005
> verification passed (clean-venv install, 31 unit tests + 2 live integration tests green,
> ruff clean, live pipeline smoke incl. doubles/null-server/tour-filter/serialization
> round-trip, key-leak sweep 0 hits) and surfaced one real-data fix (points [None, None] on
> completed matches), landed as commit 5. Verification finding: Haystack 3.0 requires
> `allowed_modules=` to deserialize third-party components — documented in README.
> Not done on purpose (out of scope by directive): PyPI publish, GitHub repo, catalog PR.

## Build target & source of truth
Haystack 2.x integration package for the Live Tennis API. Greenfield standalone repo at
`/var/tmp/haystack-build/livetennisapi-haystack`. No `PLAN.md`; source of truth is the task
directive (explicit spec: components, Secret handling, tests, README, catalog-entry draft,
verify steps) plus:
- Official client: `livetennisapi` 1.0.2 (PyPI == local checkout
  `~/Documents/ben-is-a-dev/livetennisapi-oss/livetennisapi-python`) — reused, no hand-rolled HTTP.
- OpenAPI: `~/Documents/ben-is-a-dev/livetennisapi-oss/openapi/openapi.yaml`.
- Haystack conventions modeled on July-2026 catalog merges: DDGS (#548, `integrations/ddgs.md`),
  Linkup (#549, component source `linkup_websearch.py` + tests + pyproject in
  deepset-ai/haystack-core-integrations), Perseus Vault (#538, third-party frontmatter, MIT).

## Summary
Ready to build as specified. [FACT] Haystack latest on PyPI is 3.0.0 (2026-07); recent catalog
entries still declare `version: Haystack 2.0` and the `@component`/`Secret` API is unchanged in
the 3.0 docs — pin `haystack-ai>=2.24.1` (Linkup's pin) and verify against what installs.
[FACT] client v1.0.2 `list_matches()` does not expose the API's documented `tour` query param
(client.py:121) — tour support must go through the client's transport (`_request`), a deliberate,
commented deviation (never hand-rolled HTTP). Top risks: Haystack 3.0 API drift (verified by
tests), live smoke dependent on what's actually on court (report what's real).

## Build units (ordered)

### BUILD-001 scaffold
- Delivers: git repo, `pyproject.toml` (hatchling, MIT, deps `haystack-ai>=2.24.1`,
  `livetennisapi>=1.0.2`), `LICENSE`, `.gitignore`, `src/livetennisapi_haystack/{__init__.py,py.typed}`,
  README stub. Depends-on: —.
- Acceptance: `pip install -e .` succeeds in clean venv; `import livetennisapi_haystack` works.
- Output strategy: one-shot. Becomes commit 1.

### BUILD-002 LiveTennisMatchFetcher + tests
- Delivers: `src/livetennisapi_haystack/match_fetcher.py` — `@component` class; init
  `api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY")`, `status`, `tour`, `limit`,
  `base_url`, `timeout`; `warm_up()` lazy `LiveTennisAPI`; `run(status, tour, limit, match_id)`
  → `documents: list[Document]` (content = human-readable summary; meta = ids/players/scores,
  JSON-safe). Handles: null `score.server` (omit serving clause), doubles/null
  `data_completeness`, string points, completed+winner, `UpgradeRequired` → one readable-message
  Document (`meta.error="upgrade_required"`), `NotFound` on match_id → `[]` + warning.
  `to_dict`/`from_dict` via `default_to_dict`/`default_from_dict` + `deserialize_secrets_inplace`.
  Tests: `tests/test_match_fetcher.py`, mocked client, no net.
- Depends-on: BUILD-001.
- Acceptance: pytest green incl. null-server, doubles, 403-message, tour, serialization
  round-trip cases.
- Output strategy: staged (component skeleton → summary builder → error paths → tests).
  Becomes commit 2.

### BUILD-003 LiveTennisPlayerSearch + tests
- Delivers: `src/livetennisapi_haystack/player_search.py` — same shape; `run(query, limit)` →
  `documents`. Tests incl. empty result + 403. Depends-on: BUILD-001.
- Acceptance: pytest green. Output strategy: one-shot. Becomes commit 3.

### BUILD-004 README + example + catalog-entry draft
- Delivers: full `README.md` (runnable pipeline example), `examples/live_demo.py` (env-var key,
  builds a real Pipeline, prints Documents), `catalog-entry.md` matching current
  `integrations/*.md` frontmatter exactly (third-party author form per Perseus Vault #538).
- Depends-on: BUILD-002/003. Acceptance: example runs against live API in smoke step;
  frontmatter fields match the fetched templates field-for-field. One-shot. Becomes commit 4.

### BUILD-005 verify (no new product code)
- Clean venv (TMPDIR=/var/tmp/haystack-build/tmp): install, pytest, ruff, live smoke via
  `examples/live_demo.py` (key from env only), key-leak sweep over tree + git history (0 hits).
- Acceptance: all green; real live output captured for the report. Fixes, if any, land as
  their own commits.

## Conventions to follow
- Component shape: `linkup_websearch.py` (fetched ref): `@component` class, Secret default from
  env var (never plain string), `warm_up()` lazy client, `@component.output_types`,
  `documents` output naming, module-level `logger = logging.getLogger(__name__)`.
- Tests: `test_linkup_websearch.py` (fetched ref): class-based, `MagicMock` client injected via
  `_client`, `component_to_dict`/`component_from_dict` assertions, `@pytest.mark.integration`
  gated on env key.
- Packaging: Linkup `pyproject.toml` (hatchling, ruff config, pytest markers) minus monorepo
  hatch-vcs; static version.
- Catalog entry: `ddgs.md`/`linkup.md` frontmatter + section order; third-party `authors:` per
  `perseus-vault-haystack.md`; MIT license line per Perseus.
- Client reuse: only `livetennisapi` public API, except the documented `tour` param (OpenAPI
  openapi.yaml:302-315) via `client._request` — called out in code.

## Open questions & assumptions
- [ASSUMPTION][blocking→resolved by directive] Task is an explicit approved build+verify
  directive; proceeding through Phase 2 without an interactive gate.
- [ASSUMPTION] Sync-only `run` (no `run_async`): the official client has an async twin, but
  httpx.AsyncClient lifecycle across event loops is a real cost; conventions allow sync-only
  components. Noted as future work in README.
- [ASSUMPTION] `haystack-ai` resolves to 3.0.0; `@component`/Secret API verified unchanged by
  the test suite, and catalog `version:` stays "Haystack 2.0" per current merged entries.
- [ASSUMPTION] 403 policy: an `UpgradeRequired` becomes a single Document carrying the client's
  readable tier message (meta-tagged `error`) rather than an exception — per the directive
  "readable message, not a crash"; other errors still raise (a dead network should fail loud).

## Handoff
Build, then `/full-review`. Most relevant personas: `/code-logic-review` (summary builder edge
cases), `/qa-automation` (mock coverage vs live truths), `/security-audit` (secret handling,
key-leak). No UI — browser-driven QA not applicable.
