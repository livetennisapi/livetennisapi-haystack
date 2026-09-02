# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-09-02

### Added

- Match Documents' `meta` gained `has_analysis` and `has_market` — whether a model
  thesis/profile exists for the match and whether a match-winner market is mapped to it
  (every tier, server side since 2026-09-02). They carry the same facts the per-match
  analysis and market-prices endpoints answer 404 about, so a slate can be filtered in one
  call. Both keys are always present: `None` when the server predates the field.


### Added

- `LiveTennisH2HFetcher` — the head-to-head record between two players, spanning the
  results archive (1968-2022) and current matches (2023-now), as one Document (BASIC).
- `LiveTennisArchiveFetcher` — the results archive in three modes: match results
  (`mode="matches"`, filterable by player name, date range, round and level), player bios
  (`mode="players"`) and career aggregates (`mode="career"`) (BASIC).
- `LiveTennisRankingsFetcher` — published ranking tables (`atp`, `wta`, `itf_jt`,
  `itf_mt`, `itf_wt`), one Document per row, optionally as of a past week (PRO).
- `LiveTennisMatchStatisticsFetcher` — in-play statistics for one match: aces, double
  faults, serve split, hold/break percentages, break points (ULTRA).
- `LiveTennisMatchFetcher`: new list filters `player`, `country`, `from_` and `to`,
  alongside the existing `tour` filter (which now includes `juniors`).
- Non-retryable 429s become readable Documents instead of crashes: the daily cap
  (`meta["error"] = "rate_limited"`, with the absolute `resets_at` instant) and the abuse
  throttle (`meta["error"] = "abuse_throttled"`, with `retry_at_epoch`). The per-minute
  window still raises after the official client's automatic retries.
- Ambiguous player-name fragments on the name-keyed endpoints yield a Document tagged
  `meta["error"] = "ambiguous_name"` with the candidate list.
- Match Documents' `meta` gained `tour`, `tournament_id`, `round_code` and `withdrew`.
- `scripts/truthcheck.sh` — product-facts pin, wired into a new CI workflow (tests, lint,
  truthcheck).

### Changed

- Requires `livetennisapi>=1.3.0`; the `tour` filter now uses the client's native
  `list_matches(tour=...)` parameter instead of the transport-layer workaround.
- Quota copy updated to the 2026-08-06 grid: FREE 100 requests/day (30/min), BASIC
  1,000/day, PRO 10,000/day, ULTRA 500,000/day.
- README rebuilt: component table with tier gates, quota table, authentication and links
  sections.
- License holder and catalog-entry author are the Live Tennis API org identity.

### Fixed

- `__version__` now matches the package metadata (it had been left at 0.1.0) and is
  guarded by a test.

## [0.1.1] - 2026-08-02

### Changed

- Documented the tier behind `status="completed"` listings; org author metadata; repo URLs
  point at the livetennisapi org; PyPI publishing via trusted publishing (OIDC).

## [0.1.0] - 2026-07-24

### Added

- Initial release: `LiveTennisMatchFetcher` and `LiveTennisPlayerSearch` components with
  Secret-based key handling, serialization support, mocked unit tests and a live demo
  pipeline.
