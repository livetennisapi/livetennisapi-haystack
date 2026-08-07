from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import RateLimited, UpgradeRequired
from livetennisapi.models import RankingRecord

from ._util import iso_or_none, make_upgrade_document, quota_notice

logger = logging.getLogger(__name__)

#: Systems with a published listing. UTR has none — a rating, not a ranking.
_SYSTEMS = ("atp", "wta", "itf_jt", "itf_mt", "itf_wt")
_SYSTEM_LABELS = {
    "atp": "ATP",
    "wta": "WTA",
    "itf_jt": "ITF junior",
    "itf_mt": "ITF men's world tennis tour",
    "itf_wt": "ITF women's world tennis tour",
}
_MAX_LIMIT = 200


@component
class LiveTennisRankingsFetcher:
    """
    Fetches a published ranking table from the [Live Tennis API](https://livetennisapi.com) as
    Haystack Documents, one per ranked player. **Requires the PRO tier or above.**

    One ranking system per run (``atp``, ``wta``, ``itf_jt``, ``itf_mt``, ``itf_wt`` — the
    systems are never collapsed into a single "rank", they are not comparable), the newest
    published week at or before ``as_of``. Rows carry the player name as published, so the
    table has no silent holes even for players outside the roster (their ``player_id`` is
    null). ITF ranking history begins 2026-07-29 and cannot be reconstructed earlier.

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisRankingsFetcher

    rankings = LiveTennisRankingsFetcher()      # key from LIVETENNISAPI_KEY
    result = rankings.run(system="wta", limit=10)
    for doc in result["documents"]:
        print(doc.content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        system: str = "atp",
        as_of: str | None = None,
        limit: int = 10,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisRankingsFetcher component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param system:
            Default ranking system: ``"atp"``, ``"wta"``, ``"itf_jt"``, ``"itf_mt"`` or
            ``"itf_wt"``.
        :param as_of:
            Optional default as-of date (``YYYY-MM-DD``): the newest published week at or
            before it. ``None`` means the latest known week.
        :param limit:
            Default maximum number of rows to return (1-200), from the top of the table.
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        _validate_system(system)
        self.api_key = api_key
        self.system = system
        self.as_of = as_of
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
            system=self.system,
            as_of=self.as_of,
            limit=self.limit,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisRankingsFetcher":
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
        system: str | None = None,
        as_of: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch one ranking table and return its rows as Documents.

        :param system:
            Optional per-run override of the ranking system.
        :param as_of:
            Optional per-run override of the as-of date (``YYYY-MM-DD``).
        :param limit:
            Optional per-run override of the maximum number of rows (1-200).
        :returns: A dictionary with:
            - ``documents``: one Document per ranking row, in rank order. On a free or BASIC
              key the listing answers 403 and a single Document tagged
              ``meta["error"] = "upgrade_required"`` carries the readable message; a
              daily-quota or abuse-throttle 429 becomes a ``rate_limited`` /
              ``abuse_throttled`` Document.
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisRankingsFetcher client failed to initialize."
            raise RuntimeError(msg)

        effective_system = system if system is not None else self.system
        effective_as_of = as_of if as_of is not None else self.as_of
        effective_limit = max(1, min(int(limit if limit is not None else self.limit), _MAX_LIMIT))
        _validate_system(effective_system)

        try:
            rows = self._client.list_rankings(system=effective_system, as_of=effective_as_of, limit=effective_limit)
        except UpgradeRequired as exc:
            logger.warning("Live Tennis API tier wall: {exc}", exc=exc)
            return {"documents": [make_upgrade_document(exc)]}
        except RateLimited as exc:
            notice = quota_notice(exc)
            if notice is None:  # per-minute window: transient, already retried by the client
                raise
            logger.warning("Live Tennis API quota: {exc}", exc=exc)
            return {"documents": [notice]}

        return {"documents": [ranking_to_document(r) for r in rows if r is not None]}


def _validate_system(system: str) -> None:
    if system not in _SYSTEMS:
        msg = f"system must be one of {_SYSTEMS}, got {system!r}"
        raise ValueError(msg)


def ranking_to_document(record: RankingRecord) -> Document:
    """
    Convert one ``livetennisapi`` RankingRecord into a Haystack Document.

    ``previous_rank`` exists for ATP/WTA only (and only when a prior week is held);
    ``rank_movement`` is the ITF circuits' own signed weekly movement. Both are omitted from
    the content when absent — never invented.
    """
    label = _SYSTEM_LABELS.get(record.system, record.system or "unknown")
    name = record.player_name or "unknown"

    head = f"{label} ranking #{record.rank}: {name}" if record.rank is not None else f"{label} ranking: {name}"
    bits: list[str] = []
    if record.points is not None:
        bits.append(f"{record.points} points")
    if record.previous_rank is not None:
        bits.append(f"previous week #{record.previous_rank}")
    if record.rank_movement:
        bits.append(f"moved {record.rank_movement:+d} this week")
    content = f"{head} — {', '.join(bits)}." if bits else head + "."
    effective = iso_or_none(record.effective_date)
    if effective:
        content += f" Effective {effective}."

    return Document(
        content=content,
        meta={
            "source": "livetennisapi",
            "player_id": record.player_id,
            "player_name": record.player_name,
            "system": record.system,
            "tour": record.tour,
            "rank": record.rank,
            "points": record.points,
            "previous_rank": record.previous_rank,
            "rank_movement": record.rank_movement,
            "effective_date": effective,
        },
    )
