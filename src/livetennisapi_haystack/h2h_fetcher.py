from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_secrets_inplace
from livetennisapi import LiveTennisAPI
from livetennisapi.errors import BadRequest, NotFound, RateLimited, UpgradeRequired
from livetennisapi.models import HeadToHead

from ._util import ambiguous_name_notice, make_upgrade_document, quota_notice

logger = logging.getLogger(__name__)

_MAX_MEETINGS = 200


@component
class LiveTennisH2HFetcher:
    """
    Fetches the head-to-head record between two players from the [Live Tennis API](https://livetennisapi.com)
    as one Haystack Document. **Requires the BASIC tier or above.**

    The record spans both halves of the product: the results archive (1968-2022) plus current
    completed matches (2023-now), in one call. The Document's ``content`` is a readable summary
    (who leads, the surface split, the most recent meetings) and its ``meta`` carries the
    structured totals, surface split and meeting rows.

    Players are keyed by NAME (min 3 characters each) — archive people have no roster ids. A
    fragment matching more than one player yields a single Document tagged
    ``meta["error"] = "ambiguous_name"`` with the candidate list, so an agent can ask which
    one was meant.

    Requires a Live Tennis API key, read from the ``LIVETENNISAPI_KEY`` environment variable
    by default.

    ### Usage example

    ```python
    from livetennisapi_haystack import LiveTennisH2HFetcher

    h2h = LiveTennisH2HFetcher()                # key from LIVETENNISAPI_KEY
    result = h2h.run(p1="federer", p2="nadal")
    print(result["documents"][0].content)
    ```
    """

    def __init__(
        self,
        api_key: Secret = Secret.from_env_var("LIVETENNISAPI_KEY"),
        meetings: int = 10,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize the LiveTennisH2HFetcher component.

        :param api_key:
            Live Tennis API key. Defaults to the ``LIVETENNISAPI_KEY`` environment variable.
        :param meetings:
            Default maximum number of recent meetings to include in the Document (1-200).
            The totals always cover the full record regardless of this cap.
        :param base_url:
            Optional API base URL override, mainly for testing.
        :param timeout:
            HTTP timeout in seconds.
        """
        self.api_key = api_key
        self.meetings = meetings
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
            meetings=self.meetings,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveTennisH2HFetcher":
        """
        Deserializes the component from a dictionary.

        :param data: Dictionary to deserialize from.
        :returns: Deserialized component.
        """
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key"])
        return default_from_dict(cls, data)

    @component.output_types(documents=list[Document])
    def run(self, p1: str, p2: str, meetings: int | None = None) -> dict[str, Any]:
        """
        Fetch the head-to-head record and return it as one Document.

        :param p1: First player's name, or a unique fragment of it (min 3 characters).
        :param p2: Second player's name, or a unique fragment of it (min 3 characters).
        :param meetings: Optional per-run override of how many recent meetings to include (1-200).
        :returns: A dictionary with:
            - ``documents``: a single Document summarizing the record (empty list when no
              player matches the names). A 403 tier wall, a non-retryable 429 or an ambiguous
              name each yield a single readable Document tagged with the matching
              ``meta["error"]`` (``upgrade_required``, ``rate_limited`` / ``abuse_throttled``,
              ``ambiguous_name``) instead of raising.
        """
        if self._client is None:
            self.warm_up()
        if self._client is None:
            msg = "LiveTennisH2HFetcher client failed to initialize."
            raise RuntimeError(msg)

        effective_meetings = max(1, min(int(meetings if meetings is not None else self.meetings), _MAX_MEETINGS))
        try:
            h2h = self._client.get_h2h(p1, p2)
        except NotFound:
            logger.warning("Live Tennis API: no head-to-head for {p1!r} vs {p2!r}", p1=p1, p2=p2)
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

        if h2h is None or h2h.players is None:
            logger.warning("Live Tennis API: no player matches {p1!r} / {p2!r}", p1=p1, p2=p2)
            return {"documents": []}
        return {"documents": [h2h_to_document(h2h, meetings=effective_meetings)]}


def h2h_to_document(h2h: HeadToHead, *, meetings: int = 10) -> Document:
    """
    Convert one ``livetennisapi`` HeadToHead into a Haystack Document.

    ``content`` is a readable summary; ``meta`` carries the structured record. The totals count
    only meetings with a KNOWN winner; walkovers and retirements stay part of the record, each
    meeting's ``outcome`` says which is which.
    """
    players = h2h.players or {}
    name1 = (players.get("p1") or {}).get("name") or "player 1"
    name2 = (players.get("p2") or {}).get("name") or "player 2"
    totals = h2h.totals or {}
    p1_wins = totals.get("p1_wins") or 0
    p2_wins = totals.get("p2_wins") or 0
    total_meetings = totals.get("meetings") or 0
    undecided = totals.get("undecided") or 0
    names = {1: name1, 2: name2}

    if p1_wins == p2_wins:
        head = f"Head-to-head {name1} vs {name2}: level at {p1_wins}-{p2_wins}"
    else:
        leader = 1 if p1_wins > p2_wins else 2
        head = f"Head-to-head {name1} vs {name2}: {names[leader]} leads {max(p1_wins, p2_wins)}-{min(p1_wins, p2_wins)}"
    head += f" over {total_meetings} meeting{'s' if total_meetings != 1 else ''}"
    if undecided:
        head += f" ({undecided} with no derivable winner)"
    lines = [head + "."]

    surface_bits = [
        f"{surface} {split.get('p1', 0)}-{split.get('p2', 0)}"
        for surface, split in (h2h.by_surface or {}).items()
        if isinstance(split, dict) and (split.get("p1") or split.get("p2"))
    ]
    if surface_bits:
        lines.append(f"By surface ({name1} first): {', '.join(surface_bits)}.")

    recent = (h2h.meetings or [])[:meetings]
    meeting_bits = [bit for bit in (_meeting_line(m, names) for m in recent) if bit]
    if meeting_bits:
        lines.append(f"Most recent meetings: {' '.join(meeting_bits)}")

    return Document(
        content=" ".join(lines),
        meta={
            "source": "livetennisapi",
            "p1_name": name1,
            "p2_name": name2,
            "p1_wins": p1_wins,
            "p2_wins": p2_wins,
            "meetings": total_meetings,
            "undecided": undecided,
            "by_surface": h2h.by_surface,
            "recent_meetings": recent,
        },
    )


def _meeting_line(meeting: dict[str, Any], names: dict[int, str]) -> str | None:
    """One readable sentence for a meeting row; tolerates every nullable field."""
    if not isinstance(meeting, dict):
        return None
    where = [str(v) for v in (meeting.get("date"), meeting.get("tournament")) if v]
    detail = [str(v) for v in (meeting.get("round"), meeting.get("surface")) if v]
    if detail:
        where.append(f"({', '.join(detail)})")
    winner = names.get(meeting.get("winner")) if meeting.get("winner") in (1, 2) else None
    result = f"{winner} won" if winner else "no derivable winner"
    if meeting.get("score"):
        result += f" {meeting['score']}"
    outcome = meeting.get("outcome")
    if outcome and outcome != "completed":
        result += f" ({outcome})"
    prefix = f"{' '.join(where)}: " if where else ""
    return f"{prefix}{result}."
