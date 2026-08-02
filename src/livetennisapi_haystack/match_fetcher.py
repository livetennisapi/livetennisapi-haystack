from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import NotFound, UpgradeRequired
from livetennisapi.models import Match

from ._util import iso_or_none, make_upgrade_document, player_label

logger = logging.getLogger(__name__)

_STATUSES = ("live", "upcoming", "completed")
_TOURS = ("atp", "wta", "challenger", "itf", "juniors")
_MAX_LIMIT = 200


@component
class LiveTennisMatchFetcher:
    """
    Fetches tennis matches from the [Live Tennis API](https://livetennisapi.com) as Haystack Documents.

    Each Document's ``content`` is a clean human-readable match summary (players, tournament,
    live score, server, winner) and its ``meta`` carries the structured fields (ids, players,
    sets/games/points), so the component is directly usable in RAG and agent pipelines.

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisMatchFetcher

    fetcher = LiveTennisMatchFetcher()          # key from LIVETENNISAPI_KEY
    result = fetcher.run(status="live")
    for doc in result["documents"]:
        print(doc.content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        status: str = "live",
        tour: str | None = None,
        limit: int = 10,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisMatchFetcher component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param status:
            Default match lifecycle status to fetch: ``"live"``, ``"upcoming"`` or ``"completed"``.
            ``"live"`` and ``"upcoming"`` work on the free tier; ``"completed"`` listings need the
            BASIC tier ($9.99/mo) or any History plan — on a free key the API answers 403 and the
            component returns a single ``upgrade_required`` Document (see :meth:`run`).
        :param tour:
            Optional default tour filter: ``"atp"``, ``"wta"``, ``"challenger"``, ``"itf"`` or
            ``"juniors"``. Each value covers its singles and doubles draws. ``None`` fetches all tours.
        :param limit:
            Default maximum number of matches to return (1-200).
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        _validate_status(status)
        _validate_tour(tour)
        self.api_key = api_key
        self.status = status
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
            status=self.status,
            tour=self.tour,
            limit=self.limit,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisMatchFetcher":
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
        status: str | None = None,
        tour: str | None = None,
        limit: int | None = None,
        match_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch matches and return them as Documents.

        :param status:
            Optional per-run override of the lifecycle status (``"live"``, ``"upcoming"``,
            ``"completed"``). Ignored when ``match_id`` is given. ``"completed"`` listings need
            the BASIC tier ($9.99/mo) or any History plan; on a free key they yield the
            ``upgrade_required`` Document described below. ``match_id`` fetches — including
            completed matches — stay on the free tier.
        :param tour:
            Optional per-run override of the tour filter. Ignored when ``match_id`` is given.
        :param limit:
            Optional per-run override of the maximum number of matches (1-200).
        :param match_id:
            Fetch one specific match by id instead of a listing. An unknown id yields an
            empty list, not an error.
        :returns: A dictionary with:
            - ``documents``: one Document per match, ``content`` holding the readable summary
              and ``meta`` the structured fields. If the API answers 403 (tier wall), a single
              Document tagged ``meta["error"] = "upgrade_required"`` carries the readable
              message instead.
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisMatchFetcher client failed to initialize."
            raise RuntimeError(msg)

        try:
            if match_id is not None:
                match = self._client.get_match(match_id)
                matches = [match] if match is not None else []
            else:
                matches = self._list_matches(status, tour, limit)
        except NotFound:
            logger.warning("Live Tennis API: match {match_id} not found", match_id=match_id)
            return {"documents": []}
        except UpgradeRequired as exc:
            logger.warning("Live Tennis API tier wall: {exc}", exc=exc)
            return {"documents": [make_upgrade_document(exc)]}

        return {"documents": [match_to_document(m) for m in matches if m is not None]}

    def _list_matches(self, status: str | None, tour: str | None, limit: int | None) -> list[Match]:
        effective_status = status if status is not None else self.status
        effective_tour = tour if tour is not None else self.tour
        effective_limit = max(1, min(int(limit if limit is not None else self.limit), _MAX_LIMIT))
        _validate_status(effective_status)
        _validate_tour(effective_tour)
        assert self._client is not None  # noqa: S101 - checked by run()

        if effective_tour is None:
            return list(self._client.list_matches(status=effective_status, limit=effective_limit))

        # The public /matches endpoint documents a `tour` query parameter
        # (openapi.yaml, parameters.tour), but livetennisapi 1.0.2 does not yet
        # expose it on list_matches(). Deliberate deviation: go through the
        # official client's transport layer (retries, error mapping, auth)
        # rather than hand-rolling HTTP. Switch to
        # `client.list_matches(status=..., tour=...)` once the client grows it.
        raw = self._client._request(
            "/matches",
            self._client._params(
                {"status": effective_status, "tour": effective_tour, "limit": effective_limit, "offset": 0}
            ),
        )
        items = (raw.get("data") or []) if isinstance(raw, dict) else (raw or [])
        return [m for m in (Match.from_dict(i) for i in items) if m is not None]


def _validate_status(status: str) -> None:
    if status not in _STATUSES:
        msg = f"status must be one of {_STATUSES}, got {status!r}"
        raise ValueError(msg)


def _validate_tour(tour: str | None) -> None:
    if tour is not None and tour not in _TOURS:
        msg = f"tour must be one of {_TOURS} or None, got {tour!r}"
        raise ValueError(msg)


def match_to_document(match: Match) -> Document:
    """
    Convert one ``livetennisapi`` Match into a Haystack Document.

    ``content`` is a human-readable summary; ``meta`` carries JSON-safe structured fields.
    Tolerates every documented sparsity: null ``score.server``, doubles teams without
    rankings, string points, absent scores on upcoming matches.
    """
    p1, p2 = match.p1, match.p2
    p1_label, p2_label = player_label(p1), player_label(p2)

    where = [str(match.tournament)] if match.tournament else []
    if match.surface:
        where.append(f"{match.surface} court" + (" (indoor)" if match.indoor else ""))
    if match.round:
        where.append(f"round {match.round}")
    if match.format:
        where.append({"BO5": "best of 5", "BO3": "best of 3"}.get(match.format, str(match.format)))
    kind = "doubles " if match.is_doubles else ""

    head = f"{p1_label} vs {p2_label} — {kind}match"
    if where:
        head += f" at {where[0]}" if len(where) == 1 else f" at {', '.join(where)}"

    lines = [head + "."]
    lines.extend(_status_lines(match, p1_label, p2_label))
    content = " ".join(lines)

    meta = {
        "source": "livetennisapi",
        "match_id": match.id,
        "status": match.status,
        "event_status": match.event_status,
        "tournament": match.tournament,
        "surface": match.surface,
        "indoor": match.indoor,
        "format": match.format,
        "round": match.round,
        "is_doubles": match.is_doubles,
        "scheduled_time": iso_or_none(match.scheduled_time),
        "p1_id": getattr(p1, "id", None),
        "p1_name": getattr(p1, "name", None),
        "p1_country": getattr(p1, "country", None),
        "p1_ranking": getattr(p1, "ranking", None),
        "p2_id": getattr(p2, "id", None),
        "p2_name": getattr(p2, "name", None),
        "p2_country": getattr(p2, "country", None),
        "p2_ranking": getattr(p2, "ranking", None),
        "winner": match.winner,
    }
    score = match.score
    if score is not None:
        meta.update(
            {
                "sets": score.sets,
                "games": score.games,
                "points": score.points,
                "server": score.server,
                "is_tiebreak": score.is_tiebreak,
                "score_timestamp": iso_or_none(score.timestamp),
            }
        )
    return Document(content=content, meta=meta)


def _status_lines(match: Match, p1_label: str, p2_label: str) -> list[str]:
    """Readable status/score sentences for one match."""
    lines: list[str] = []
    score = match.score
    names = {1: p1_label, 2: p2_label}

    if match.status == "upcoming":
        when = iso_or_none(match.scheduled_time)
        lines.append(f"Scheduled for {when}." if when else "Upcoming; not yet scheduled.")
        return lines

    if match.status == "completed":
        winner = names.get(match.winner) if match.winner in (1, 2) else None
        lines.append(f"Completed; winner: {winner}." if winner else "Completed.")
    elif match.status == "live":
        lines.append("Live now.")
    elif match.status:
        lines.append(f"Status: {match.status}.")

    if score is None:
        return lines

    sentence = _score_sentence(score)
    if sentence:
        lines.append(sentence)
    # score.server is nullable by contract: between points/matches the feed may
    # not know who serves next. Only say so when it does.
    server = names.get(score.server) if score.server in (1, 2) else None
    if server and match.status == "live":
        lines.append(f"{server} is serving.")
    return lines


def _score_sentence(score: Any) -> str | None:
    """One ``"Score: sets 1-1, games 6-4, 3-6, points 30-15."`` sentence, or None."""
    parts: list[str] = []
    if score.sets and len(score.sets) >= 2:
        parts.append(f"sets {score.sets[0]}-{score.sets[1]}")
    games = _games_summary(score.games)
    if games:
        parts.append(f"games {games}")
    # Points are strings by contract ("0", "15", "30", "40", "AD") — but live
    # data shows completed matches can carry [None, None], so only render
    # points when both sides actually exist.
    if score.points and len(score.points) >= 2 and score.points[0] is not None and score.points[1] is not None:
        parts.append(f"points {score.points[0]}-{score.points[1]}")
    if score.is_tiebreak:
        parts.append("in a tiebreak")
    return f"Score: {', '.join(parts)}." if parts else None


def _games_summary(games: Any) -> str | None:
    """Per-set games as ``"6-4, 3-6, 2-1"``.

    The wire format is player-major — ``[games_p1, games_p2]``, each a per-set
    list — so a set's score is a zip across the two sides, not one row.
    """
    if not isinstance(games, list) or len(games) < 2:
        return None
    p1, p2 = games[0], games[1]
    if not isinstance(p1, list) or not isinstance(p2, list):
        return None
    # strict=False on purpose: mid-update the two sides can briefly disagree in
    # length; showing the sets both agree on beats crashing.
    pairs = [f"{a}-{b}" for a, b in zip(p1, p2, strict=False)]
    return ", ".join(pairs) if pairs else None
