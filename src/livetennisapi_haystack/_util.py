"""Helpers shared by the Live Tennis API components.

Kept private: only the components are public API.
"""

from typing import Any

from haystack import Document
from livetennisapi.errors import UpgradeRequired


def make_upgrade_document(exc: UpgradeRequired) -> Document:
    """Turn a 403 tier wall into a readable Document instead of a crash.

    The Live Tennis API returns 403 when the key is valid but the plan does not
    unlock the endpoint. In a pipeline that should surface as information the
    downstream LLM (or user) can act on, not as a traceback. The Document is
    tagged with ``meta["error"] = "upgrade_required"`` so RAG pipelines that
    only want match data can filter it out.
    """
    return Document(
        content=f"Live Tennis API access notice: {exc}",
        meta={
            "source": "livetennisapi",
            "error": "upgrade_required",
            "status_code": exc.status_code,
            "required_tier": exc.required_tier,
        },
    )


def player_label(player: Any) -> str:
    """One human-readable label for a player or doubles team.

    Tolerates sparse records: doubles teams have no ranking and lower-tour
    players may miss country. Only what exists is shown.
    """
    if player is None:
        return "unknown"
    name = getattr(player, "name", None) or "unknown"
    bits = []
    country = getattr(player, "country", None)
    if country:
        bits.append(str(country))
    ranking = getattr(player, "ranking", None)
    if ranking is not None:
        bits.append(f"#{ranking}")
    return f"{name} ({', '.join(bits)})" if bits else name


def iso_or_none(value: Any) -> Any:
    """Datetime/date -> ISO string for JSON-safe Document meta; else pass through."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else value
