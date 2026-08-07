from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import NotFound, RateLimited, UpgradeRequired
from livetennisapi.models import MatchStatistics

from ._util import make_upgrade_document, quota_notice

logger = logging.getLogger(__name__)


@component
class LiveTennisMatchStatisticsFetcher:
    """
    Fetches in-play statistics for one match from the [Live Tennis API](https://livetennisapi.com)
    as a single Haystack Document. **Requires the ULTRA tier.**

    Aces, double faults, the serve split, hold/break percentages, break points and
    service/return points — in two families that are deliberately not merged: DERIVED figures
    are rebuilt from the point-by-point record, MEASURED figures are counted upstream (which
    is why only they can hold aces and double faults). Every measured field is optional and an
    absent field is omitted, never zero-filled; the Document renders only what exists.

    The statistics payload does not carry player names; pass ``p1_name`` / ``p2_name`` (for
    example from a ``LiveTennisMatchFetcher`` Document's meta) to label the summary, otherwise
    the players are called "player 1" and "player 2".

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisMatchStatisticsFetcher

    stats = LiveTennisMatchStatisticsFetcher()  # key from LIVETENNISAPI_KEY
    result = stats.run(match_id=12345, p1_name="Alcaraz", p2_name="Sinner")
    print(result["documents"][0].content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisMatchStatisticsFetcher component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        self.api_key = api_key
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
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisMatchStatisticsFetcher":
        """
        Deserializes the component from a dictionary.

        :param data: Dictionary to deserialize from.
        :returns: Deserialized component.
        """
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)

    @component.output_types(documents=list[Document])
    def run(self, match_id: int, p1_name: str | None = None, p2_name: str | None = None) -> dict[str, Any]:
        """
        Fetch one match's statistics and return them as a single Document.

        :param match_id: The match to fetch statistics for.
        :param p1_name: Optional label for player 1 in the readable summary.
        :param p2_name: Optional label for player 2 in the readable summary.
        :returns: A dictionary with:
            - ``documents``: a single Document with the readable statistics summary
              (``meta`` carries the raw per-player statistics families). A match we hold
              nothing for — the API answers 200 with null players — and an unknown match id
              both yield an empty list. A 403 tier wall yields the ``upgrade_required``
              Document; a daily-quota or abuse-throttle 429 the ``rate_limited`` /
              ``abuse_throttled`` Document.
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisMatchStatisticsFetcher client failed to initialize."
            raise RuntimeError(msg)

        try:
            stats = self._client.get_match_statistics(match_id)
        except NotFound:
            logger.warning("Live Tennis API: match {match_id} not found", match_id=match_id)
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

        if stats is None or stats.players is None:
            # 200 with null players is the API's honest "we hold nothing for this match".
            logger.info("Live Tennis API: no statistics held for match {match_id}", match_id=match_id)
            return {"documents": []}
        return {"documents": [statistics_to_document(stats, p1_name=p1_name, p2_name=p2_name)]}


def statistics_to_document(
    stats: MatchStatistics, *, p1_name: str | None = None, p2_name: str | None = None
) -> Document:
    """
    Convert one ``livetennisapi`` MatchStatistics into a Haystack Document.

    Renders only the fields that exist — measured coverage is not uniform across tours, and
    an absent measured field means "not counted", never zero.
    """
    labels = {"p1": p1_name or "player 1", "p2": p2_name or "player 2"}
    lines = [f"Match statistics for match {stats.match_id}."]
    for key in ("p1", "p2"):
        side = (stats.players or {}).get(key)
        if isinstance(side, dict):
            summary = _side_summary(side)
            if summary:
                lines.append(f"{labels[key].capitalize()}: {summary}.")

    return Document(
        content=" ".join(lines),
        meta={
            "source": "livetennisapi",
            "match_id": stats.match_id,
            "coverage": stats.coverage,
            "as_of": stats.as_of,
            "games_counted": stats.games_counted,
            "sets_covered": stats.sets_covered,
            "freshness": stats.freshness,
            "p1_name": p1_name,
            "p2_name": p2_name,
            "p1_statistics": stats.p1,
            "p2_statistics": stats.p2,
        },
    )


def _side_summary(side: dict[str, Any]) -> str | None:
    """One readable clause list for one player's statistics; only what exists is shown."""
    bits: list[str] = []
    measured = side.get("measured") if isinstance(side.get("measured"), dict) else {}

    if measured.get("aces") is not None:
        bits.append(f"{measured['aces']} aces")
    if measured.get("double_faults") is not None:
        bits.append(f"{measured['double_faults']} double faults")
    if measured.get("first_serves_in_pct") is not None:
        bits.append(f"{measured['first_serves_in_pct']}% first serves in")
    if measured.get("first_serve_points_won_pct") is not None:
        bits.append(f"{measured['first_serve_points_won_pct']}% first-serve points won")

    played, won = side.get("service_games_played"), side.get("service_games_won")
    if played:
        hold = f"won {won}/{played} service games"
        if side.get("hold_pct") is not None:
            hold += f" ({side['hold_pct']}% hold)"
        bits.append(hold)
    faced = side.get("break_points_faced")
    if faced:
        bits.append(f"saved {side.get('break_points_saved', 0)}/{faced} break points faced")
    bp_played = side.get("break_points_played")
    if bp_played:
        bits.append(f"converted {side.get('break_points_converted', 0)}/{bp_played} break points")
    points_played = side.get("points_played")
    if points_played:
        bits.append(f"won {side.get('points_won', 0)}/{points_played} points")

    return ", ".join(bits) if bits else None
