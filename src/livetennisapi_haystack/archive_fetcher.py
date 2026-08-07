from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import BadRequest, NotFound, RateLimited, UpgradeRequired
from livetennisapi.models import ArchiveCareer, ArchiveMatch, ArchivePlayerBio

from ._util import ambiguous_name_notice, iso_or_none, make_upgrade_document, quota_notice

logger = logging.getLogger(__name__)

_MODES = ("matches", "players", "career")
_TOURS = ("atp", "wta", "challenger", "itf", "juniors")
_MAX_LIMIT = 200


@component
class LiveTennisArchiveFetcher:
    """
    Fetches the [Live Tennis API](https://livetennisapi.com) results archive (1968-2022) as
    Haystack Documents. **Requires the BASIC tier or above.**

    Three modes share the component:

    - ``mode="matches"`` — completed-match RESULTS from the licensed historical corpus
      (1,485,752 matches: ATP and WTA main draws, qualifying and the ITF/futures tiers),
      one Document per result, filterable by player name, date range, round and level.
    - ``mode="players"`` — the people of the archive, one Document per bio (hand, country,
      date of birth, height, career-high rank).
    - ``mode="career"`` — career aggregates for ONE player (win-loss record, titles, surface
      and year splits, summed serve statistics from 1991), one Document.

    The archive ends 2022-12-31 by design — from 2023 the history product serves current
    matches, so no match is ever served from two datasets. ``None`` fields are the era's
    silence, never filled in.

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisArchiveFetcher

    archive = LiveTennisArchiveFetcher()        # key from LIVETENNISAPI_KEY
    result = archive.run(name="borg", from_="1980-01-01", to="1980-12-31", round="F")
    for doc in result["documents"]:
        print(doc.content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        mode: str = "matches",
        tour: str | None = None,
        limit: int = 10,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisArchiveFetcher component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param mode:
            Default mode: ``"matches"``, ``"players"`` or ``"career"``.
        :param tour:
            Optional default tour filter (``"atp"``, ``"wta"``, ``"challenger"``, ``"itf"``,
            ``"juniors"``). Ignored in career mode.
        :param limit:
            Default maximum number of Documents to return (1-200). Career mode always returns
            a single Document.
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        _validate_mode(mode)
        _validate_tour(tour)
        self.api_key = api_key
        self.mode = mode
        self.tour = tour
        self.limit = limit
        self.base_url = base_url
        self.timeout = timeout
        self._client: LiveTennisAPI | None = None

    def warm_up(self) -> None:
        """
        Initialize the Live Tennis API client.

        Called automatically on first use. Can be called explicitly to avoid cold-start latency.
        """
        if self._client is None:
            self._client = LiveTennisAPI(
                api_key=self.api_key.resolve_value(),
                base_url=self.base_url,
                timeout=self.timeout,
            )

    def to_dict(self) -> dict[str, Any]:
        """
        Serializes the component to a dictionary.

        :returns: Dictionary with serialized data. The API key is stored as a Secret reference,
            never as a plain string.
        """
        return default_to_dict(
            self,
            api_key=self.api_key.to_dict(),
            mode=self.mode,
            tour=self.tour,
            limit=self.limit,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisArchiveFetcher":
        """
        Deserializes the component from a dictionary.

        :param data: Dictionary to deserialize from.
        :returns: Deserialized component.
        """
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)

    @component.output_types(documents=list[Document])
    def run(
        self,
        mode: str | None = None,
        name: str | None = None,
        tour: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        round: str | None = None,  # noqa: A002 - mirrors the API's query parameter
        level: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch archive records and return them as Documents.

        :param mode:
            Optional per-run override of the mode (``"matches"``, ``"players"``, ``"career"``).
        :param name:
            Player-name filter (case-insensitive substring, min 3 characters). In matches mode
            it matches EITHER player; in players mode it filters the bios; in career mode it is
            REQUIRED and must resolve to exactly one person — an ambiguous fragment yields a
            Document tagged ``meta["error"] = "ambiguous_name"`` with the candidate list.
        :param tour:
            Optional per-run override of the tour filter. Ignored in career mode.
        :param from_:
            Matches mode only: lower bound on the tournament START date (``YYYY-MM-DD``) —
            the only date this era's records carry.
        :param to:
            Matches mode only: upper bound on the tournament start date.
        :param round:
            Matches mode only: round in the archive's controlled vocabulary
            (``F``, ``SF``, ``QF``, ``R16``, ... ``Q1``-``Q4``).
        :param level:
            Matches mode only: source tier code (``G``, ``M``, ``A``, ``F``, ``D``, ``C``,
            ``O``, or a futures category code like ``"15"``).
        :param limit:
            Optional per-run override of the maximum number of Documents (1-200).
        :returns: A dictionary with:
            - ``documents``: one Document per archive record (a single Document in career
              mode; an empty list when nothing matches). A 403 tier wall, a non-retryable 429
              or an ambiguous career name each yield a single readable Document tagged with
              the matching ``meta["error"]`` instead of raising.
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisArchiveFetcher client failed to initialize."
            raise RuntimeError(msg)

        effective_mode = mode if mode is not None else self.mode
        effective_tour = tour if tour is not None else self.tour
        effective_limit = max(1, min(int(limit if limit is not None else self.limit), _MAX_LIMIT))
        _validate_mode(effective_mode)
        _validate_tour(effective_tour)
        if effective_mode == "career" and not name:
            msg = "mode='career' requires a name"
            raise ValueError(msg)

        try:
            documents = self._fetch(effective_mode, name, effective_tour, from_, to, round, level, effective_limit)
        except NotFound:
            logger.warning("Live Tennis API: no archive record for {player_name!r}", player_name=name)
            return {"documents": []}
        except UpgradeRequired as exc:
            logger.warning("Live Tennis API tier wall: {exc}", exc=exc)
            return {"documents": [make_upgrade_document(exc)]}
        except RateLimited as exc:
            notice = quota_notice(exc)
            if notice is None:  # per-minute window: transient, already retried by the client
                raise
            logger.warning("Live Tennis API quota: {exc}", exc=exc)
            return {"documents": [notice]}
        except BadRequest as exc:
            notice = ambiguous_name_notice(exc)
            if notice is None:  # a genuinely malformed request should fail loud
                raise
            logger.warning("Live Tennis API ambiguous name: {exc}", exc=exc)
            return {"documents": [notice]}

        return {"documents": documents}

    def _fetch(
        self,
        mode: str,
        name: str | None,
        tour: str | None,
        from_: str | None,
        to: str | None,
        round_: str | None,
        level: str | None,
        limit: int,
    ) -> list[Document]:
        assert self._client is not None  # noqa: S101 - checked by run()
        if mode == "career":
            career = self._client.get_archive_career(name)
            return [] if career is None else [career_to_document(career)]
        if mode == "players":
            bios = self._client.list_archive_players(name, tour=tour, limit=limit)
            return [archive_player_to_document(b) for b in bios if b is not None]
        matches = self._client.list_archive_matches(
            tour=tour, name=name, from_=from_, to=to, round=round_, level=level, limit=limit
        )
        return [archive_match_to_document(m) for m in matches if m is not None]


def _validate_mode(mode: str) -> None:
    if mode not in _MODES:
        msg = f"mode must be one of {_MODES}, got {mode!r}"
        raise ValueError(msg)


def _validate_tour(tour: str | None) -> None:
    if tour is not None and tour not in _TOURS:
        msg = f"tour must be one of {_TOURS} or None, got {tour!r}"
        raise ValueError(msg)


def _participant_label(participant: Any) -> str:
    """Readable label for an archive winner/loser; only what the era recorded is shown."""
    if participant is None:
        return "unknown"
    name = participant.name or "unknown"
    bits = []
    if participant.country:
        bits.append(str(participant.country))
    if participant.rank is not None:
        bits.append(f"#{participant.rank}")
    if participant.seed is not None:
        bits.append(f"seed {participant.seed}")
    return f"{name} ({', '.join(bits)})" if bits else name


def archive_match_to_document(match: ArchiveMatch) -> Document:
    """
    Convert one ``livetennisapi`` ArchiveMatch into a Haystack Document.

    Winner/loser-shaped, like the source records: the winner is a field, never an inference.
    ``event_date`` is the TOURNAMENT START date — per-match dates do not exist in this era.
    """
    winner, loser = match.winner, match.loser
    where = [str(v) for v in (match.tournament, match.surface) if v]
    if match.round:
        where.append(f"round {match.round}")
    when = iso_or_none(match.event_date)

    head = f"{_participant_label(winner)} d. {_participant_label(loser)}"
    if where:
        head += f" at {', '.join(where)}"
    if when:
        head += f" (tournament started {when})"
    lines = [head + "."]
    if match.score:
        score = f"Score: {match.score}"
        if match.outcome and match.outcome != "completed":
            score += f" ({match.outcome})"
        lines.append(score + ".")
    if match.minutes:
        lines.append(f"Duration: {match.minutes} minutes.")

    return Document(
        content=" ".join(lines),
        meta={
            "source": "livetennisapi",
            "archive_id": match.id,
            "source_id": match.source_id,
            "tour": match.tour,
            "level": match.level,
            "tournament": match.tournament,
            "surface": match.surface,
            "draw_size": match.draw_size,
            "event_date": when,
            "round": match.round,
            "best_of": match.best_of,
            "minutes": match.minutes,
            "winner_name": getattr(winner, "name", None),
            "winner_player_id": getattr(winner, "player_id", None),
            "winner_country": getattr(winner, "country", None),
            "winner_rank": getattr(winner, "rank", None),
            "loser_name": getattr(loser, "name", None),
            "loser_player_id": getattr(loser, "player_id", None),
            "loser_country": getattr(loser, "country", None),
            "loser_rank": getattr(loser, "rank", None),
            "score": match.score,
            "outcome": match.outcome,
        },
    )


def archive_player_to_document(bio: ArchivePlayerBio) -> Document:
    """
    Convert one ``livetennisapi`` ArchivePlayerBio into a Haystack Document.

    Tolerates the era's silence: hand, dob, height and career-high may all be ``None``.
    """
    name = bio.name or "unknown"
    bits: list[str] = []
    if bio.country:
        bits.append(str(bio.country))
    if bio.tour:
        bits.append(f"{bio.tour} tour")
    hand = {"R": "right-handed", "L": "left-handed"}.get(bio.hand)
    if hand:
        bits.append(hand)
    dob = iso_or_none(bio.dob)
    if dob:
        bits.append(f"born {dob}")
    if bio.height_cm:
        bits.append(f"{bio.height_cm} cm")
    if bio.career_high_rank is not None:
        high = f"career-high rank #{bio.career_high_rank}"
        high_date = iso_or_none(bio.career_high_date)
        if high_date:
            high += f" (first reached {high_date})"
        bits.append(high)

    content = f"{name} (results archive 1968-2022) — {'; '.join(bits)}." if bits else f"{name} (results archive)."
    return Document(
        content=content,
        meta={
            "source": "livetennisapi",
            "archive_player_id": bio.id,
            "tour": bio.tour,
            "name": bio.name,
            "hand": bio.hand,
            "dob": dob,
            "country": bio.country,
            "height_cm": bio.height_cm,
            "career_high_rank": bio.career_high_rank,
            "career_high_date": iso_or_none(bio.career_high_date),
        },
    )


def career_to_document(career: ArchiveCareer) -> Document:
    """
    Convert one ``livetennisapi`` ArchiveCareer into a Haystack Document.

    Everything is a sum or a ratio of sums over the archive's own rows — nothing is modelled.
    A 1970s career has a full win-loss record and an empty serve block: per-match serve
    statistics exist in the corpus from 1991 only.
    """
    name = (career.player or {}).get("name") or "unknown"
    record = career.record or {}
    span = career.span or {}
    wins, losses = record.get("wins"), record.get("losses")

    head = f"Career of {name} in the results archive (1968-2022)"
    first, last = span.get("first"), span.get("last")
    if first and last:
        head += f", active {str(first)[:4]}-{str(last)[:4]}"
    lines = [head + "."]
    if wins is not None and losses is not None:
        rec = f"Record: {wins}-{losses}"
        titles = record.get("titles")
        if titles is not None:
            rec += f", {titles} title{'s' if titles != 1 else ''}"
        lines.append(rec + ".")
    surface_split = record.get("by_surface")
    if isinstance(surface_split, dict) and surface_split:
        bits = [
            f"{surface} {split.get('wins', 0)}-{split.get('losses', 0)}"
            for surface, split in surface_split.items()
            if isinstance(split, dict)
        ]
        if bits:
            lines.append(f"By surface: {', '.join(bits)}.")
    serve = career.serve or {}
    if serve.get("matches_with_stats"):
        bits = [f"over {serve['matches_with_stats']} matches with serve statistics (recorded from 1991)"]
        if serve.get("aces") is not None:
            bits.append(f"{serve['aces']} aces")
        if serve.get("double_faults") is not None:
            bits.append(f"{serve['double_faults']} double faults")
        if serve.get("first_in_pct") is not None:
            bits.append(f"{serve['first_in_pct']:.0f}% first serves in")
        lines.append(f"Serve: {', '.join(bits)}.")

    return Document(
        content=" ".join(lines),
        meta={
            "source": "livetennisapi",
            "name": name,
            "span": career.span,
            "record": career.record,
            "by_year": career.by_year,
            "serve": career.serve,
        },
    )
