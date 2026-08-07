"""Helpers shared by the Live Tennis API components.

Kept private: only the components are public API.
"""

from collections.abc import Mapping
from typing import Any

from haystack import Document
from livetennisapi.errors import AbuseThrottled, BadRequest, RateLimited, UpgradeRequired


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


def quota_notice(exc: RateLimited) -> Document | None:
    """Turn a NON-RETRYABLE 429 into a readable Document; ``None`` means re-raise.

    Three 429 shapes share the status code. The per-minute window is transient
    and the official client already retried it with backoff before this
    exception surfaced — a pipeline should fail loud there (``None``). The
    other two cannot be fixed by waiting a few seconds, so they become
    information the downstream LLM (or user) can act on:

    - the DAILY cap (``scope == "day"``) — the Document carries ``resets_at``,
      the absolute instant the day quota resets (derived from a local
      midnight — never assume a UTC midnight);
    - the ABUSE THROTTLE (``abuse_throttled``) — a 24-hour block for chronic
      over-cap clients, with ``retry_at_epoch`` saying when it lifts. Fix the
      retry loop; retrying is what earns this response in the first place.
    """
    meta: dict[str, Any] = {"source": "livetennisapi", "status_code": 429}
    if isinstance(exc, AbuseThrottled):
        meta["error"] = "abuse_throttled"
        meta["retry_at_epoch"] = exc.retry_at_epoch
        meta["retry_at"] = iso_or_none(exc.retry_at)
    elif exc.scope == "day":
        meta["error"] = "rate_limited"
        meta["scope"] = "day"
        meta["limit_per_day"] = exc.limit_per_day
        meta["resets_at"] = iso_or_none(exc.resets_at)
    else:
        return None
    return Document(content=f"Live Tennis API access notice: {exc}", meta=meta)


def ambiguous_name_notice(exc: BadRequest) -> Document | None:
    """Turn an ``ambiguous_name`` 400 into a readable Document; ``None`` means re-raise.

    Name-keyed endpoints (``/h2h``, the archive career) refuse a fragment that
    matches more than one player and return the candidate list — exactly the
    information an agent needs to ask "which one did you mean?". Any other 400
    is a malformed request and should raise.
    """
    if exc.error_code != "ambiguous_name":
        return None
    candidates = exc.body.get("candidates") if isinstance(exc.body, Mapping) else None
    candidates = [str(c) for c in candidates] if isinstance(candidates, list) else []
    content = f"Live Tennis API access notice: {exc}"
    if candidates:
        content += f" — the name matches more than one player: {', '.join(candidates)}."
    return Document(
        content=content,
        meta={
            "source": "livetennisapi",
            "error": "ambiguous_name",
            "status_code": exc.status_code,
            "candidates": candidates,
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
