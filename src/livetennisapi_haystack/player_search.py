from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import RateLimited, UpgradeRequired
from livetennisapi.models import Player

from ._util import iso_or_none, make_upgrade_document, quota_notice

logger = logging.getLogger(__name__)

_MAX_LIMIT = 200

_HANDS = {"R": "right-handed", "L": "left-handed"}
_BACKHANDS = {1: "one-handed backhand", 2: "two-handed backhand"}


@component
class LiveTennisPlayerSearch:
    """
    Searches players on the [Live Tennis API](https://livetennisapi.com) and returns them as Haystack Documents.

    Each Document's ``content`` is a readable player summary (name, country, ranking, plays)
    and its ``meta`` carries the structured fields, ready for RAG and agent pipelines.
    Ranked players come first in the API's ordering.

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisPlayerSearch

    search = LiveTennisPlayerSearch()           # key from LIVETENNISAPI_KEY
    result = search.run(query="alcaraz")
    for doc in result["documents"]:
        print(doc.content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        limit: int = 10,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisPlayerSearch component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param limit:
            Default maximum number of players to return (1-200).
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        self.api_key = api_key
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
            limit=self.limit,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisPlayerSearch":
        """
        Deserializes the component from a dictionary.

        :param data: Dictionary to deserialize from.
        :returns: Deserialized component.
        """
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)

    @component.output_types(documents=list[Document])
    def run(self, query: str, limit: int | None = None) -> dict[str, Any]:
        """
        Search players by name and return them as Documents.

        :param query: Player name (or part of it) to search for.
        :param limit: Optional per-run override of the maximum number of players (1-200).
        :returns: A dictionary with:
            - ``documents``: one Document per player. If the API answers 403 (tier wall), a
              single Document tagged ``meta["error"] = "upgrade_required"`` carries the
              readable message instead; a daily-quota or abuse-throttle 429 becomes a Document
              tagged ``rate_limited`` (with ``resets_at``) or ``abuse_throttled`` (with
              ``retry_at_epoch``).
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisPlayerSearch client failed to initialize."
            raise RuntimeError(msg)

        effective_limit = max(1, min(int(limit if limit is not None else self.limit), _MAX_LIMIT))
        try:
            players = list(self._client.search_players(query, limit=effective_limit))
        except UpgradeRequired as exc:
            logger.warning("Live Tennis API tier wall: {exc}", exc=exc)
            return {"documents": [make_upgrade_document(exc)]}
        except RateLimited as exc:
            notice = quota_notice(exc)
            if notice is None:  # per-minute window: transient, already retried by the client
                raise
            logger.warning("Live Tennis API quota: {exc}", exc=exc)
            return {"documents": [notice]}

        return {"documents": [player_to_document(p) for p in players if p is not None]}


def player_to_document(player: Player) -> Document:
    """
    Convert one ``livetennisapi`` Player into a Haystack Document.

    Tolerates sparse records: doubles teams and lower-tour players may miss country,
    ranking, hand, backhand or birthday — only what exists is written.
    """
    name = player.name or "unknown"
    bits: list[str] = []
    if player.is_doubles_team:
        bits.append("doubles team")
    if player.country:
        bits.append(str(player.country))
    if player.tour:
        # The record's own tour label is opaque by contract (e.g. "atp", "ATP",
        # "juniors_boys") — shown as-is, never parsed.
        bits.append(f"tour {player.tour}")
    if player.ranking is not None:
        rank = f"ranked #{player.ranking}"
        if player.ranking_points is not None:
            rank += f" ({player.ranking_points} pts)"
        if player.ranking_movement in ("up", "down"):
            rank += f", moving {player.ranking_movement}"
        bits.append(rank)
    plays = _HANDS.get(player.hand)
    if plays:
        backhand = _BACKHANDS.get(player.backhand)
        bits.append(f"plays {plays}" + (f" with a {backhand}" if backhand else ""))
    birthday = iso_or_none(player.birthday)
    if birthday:
        bits.append(f"born {birthday}")

    content = f"{name} — {'; '.join(bits)}." if bits else f"{name}."

    meta = {
        "source": "livetennisapi",
        "player_id": player.id,
        "name": player.name,
        "tour": player.tour,
        "country": player.country,
        "ranking": player.ranking,
        "ranking_points": player.ranking_points,
        "ranking_movement": player.ranking_movement,
        "hand": player.hand,
        "backhand": player.backhand,
        "birthday": iso_or_none(player.birthday),
        "is_doubles_team": player.is_doubles_team,
    }
    return Document(content=content, meta=meta)
