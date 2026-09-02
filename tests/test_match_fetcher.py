import os
from unittest.mock import MagicMock

import pytest
from haystack import Document
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import AbuseThrottled, NotFound, RateLimited, UpgradeRequired
from livetennisapi.models import Match

from livetennisapi_haystack import LiveTennisMatchFetcher
from livetennisapi_haystack.match_fetcher import match_to_document


def live_singles_payload():
    return {
        "id": 101,
        "tournament": "Wimbledon",
        "surface": "grass",
        "indoor": False,
        "format": "BO5",
        "round": "QF",
        "status": "live",
        "is_doubles": False,
        "players": {
            "p1": {"id": 1, "name": "Carlos Alcaraz", "country": "ESP", "ranking": 2},
            "p2": {"id": 2, "name": "Jannik Sinner", "country": "ITA", "ranking": 1},
        },
        "score": {
            "sets": [1, 1],
            "games": [[6, 3, 2], [4, 6, 1]],
            "points": ["30", "15"],
            "server": 1,
            "is_tiebreak": False,
            "timestamp": "2026-07-24T14:03:22Z",
        },
    }


def null_server_payload():
    p = live_singles_payload()
    p["id"] = 102
    p["score"]["server"] = None  # nullable by contract: between points the feed may not know
    return p


def doubles_payload():
    """Doubles: team 'players', no rankings, data_completeness known/of are null."""
    return {
        "id": 103,
        "tournament": "ATP Masters Toronto",
        "surface": "hard",
        "indoor": False,
        "format": "BO3",
        "round": "R16",
        "status": "live",
        "is_doubles": True,
        "players": {
            "p1": {
                "id": 31,
                "name": "Salisbury/Ram",
                "country": None,
                "ranking": None,
                "is_doubles_team": True,
                "data_completeness": {"known": None, "of": None, "note": "doubles team"},
            },
            "p2": {
                "id": 32,
                "name": "Krawietz/Puetz",
                "country": None,
                "ranking": None,
                "is_doubles_team": True,
                "data_completeness": {"known": None, "of": None, "note": "doubles team"},
            },
        },
        "score": {"sets": [0, 1], "games": [[3], [6]], "points": ["40", "AD"], "server": 2, "is_tiebreak": False},
    }


def completed_payload():
    return {
        "id": 104,
        "tournament": "Roland Garros",
        "surface": "clay",
        "status": "completed",
        "is_doubles": False,
        "winner": 2,
        "players": {
            "p1": {"id": 1, "name": "Carlos Alcaraz", "country": "ESP", "ranking": 2},
            "p2": {"id": 2, "name": "Jannik Sinner", "country": "ITA", "ranking": 1},
        },
        "score": {"sets": [1, 2], "games": [[6, 4, 3], [4, 6, 6]], "points": None, "server": None},
    }


def upcoming_payload():
    return {
        "id": 105,
        "tournament": "US Open",
        "surface": "hard",
        "status": "upcoming",
        "is_doubles": False,
        "scheduled_time": "2026-08-31T17:00:00Z",
        "players": {
            "p1": {"id": 5, "name": "Coco Gauff", "country": "USA", "ranking": 3},
            "p2": {"id": 6, "name": "Iga Swiatek", "country": "POL", "ranking": 2},
        },
        "score": None,
    }


def as_matches(*payloads):
    return [Match.from_dict(p) for p in payloads]


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.list_matches.return_value = as_matches(live_singles_payload())
    return client


class TestInitAndSerialization:
    def test_init_default(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisMatchFetcher()
        assert fetcher.status == "live"
        assert fetcher.tour is None
        assert fetcher.limit == 10
        assert fetcher.api_key.resolve_value() == "test-key"
        assert fetcher._client is None

    def test_init_rejects_bad_status(self):
        with pytest.raises(ValueError, match="status"):
            LiveTennisMatchFetcher(api_key=Secret.from_token("k"), status="finished")

    def test_init_rejects_bad_tour(self):
        with pytest.raises(ValueError, match="tour"):
            LiveTennisMatchFetcher(api_key=Secret.from_token("k"), tour="exhibition")

    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisMatchFetcher(status="upcoming", tour="wta", limit=5)
        data = component_to_dict(fetcher, "fetcher")
        assert data["type"] == "livetennisapi_haystack.match_fetcher.LiveTennisMatchFetcher"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert init["status"] == "upcoming"
        assert init["tour"] == "wta"
        assert init["limit"] == 5
        # The key value itself must never appear anywhere in the serialized form.
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisMatchFetcher(status="completed", limit=3)
        restored = component_from_dict(LiveTennisMatchFetcher, component_to_dict(original, "fetcher"), "fetcher")
        assert restored.status == "completed"
        assert restored.limit == 3
        assert restored.api_key.resolve_value() == "test-key"

    def test_token_secret_refuses_serialization(self):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("plain-token"))
        with pytest.raises(ValueError, match=r"[Cc]annot serialize"):
            fetcher.to_dict()


class TestRun:
    def test_live_match_document(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        result = fetcher.run()
        docs = result["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert isinstance(doc, Document)
        assert "Carlos Alcaraz (ESP, #2) vs Jannik Sinner (ITA, #1)" in doc.content
        assert "Wimbledon" in doc.content
        assert "sets 1-1" in doc.content
        assert "games 6-4, 3-6, 2-1" in doc.content  # player-major wire format, zipped per set
        assert "points 30-15" in doc.content
        assert "Carlos Alcaraz (ESP, #2) is serving." in doc.content
        assert doc.meta["match_id"] == 101
        assert doc.meta["p1_name"] == "Carlos Alcaraz"
        assert doc.meta["p2_ranking"] == 1
        assert doc.meta["sets"] == [1, 1]
        assert doc.meta["points"] == ["30", "15"]
        assert doc.meta["server"] == 1
        assert doc.meta["score_timestamp"] == "2026-07-24T14:03:22+00:00"
        mock_client.list_matches.assert_called_once_with(status="live", limit=10)

    def test_null_server_omits_serving_line(self, mock_client):
        mock_client.list_matches.return_value = as_matches(null_server_payload())
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "is serving" not in doc.content
        assert doc.meta["server"] is None
        assert "sets 1-1" in doc.content  # rest of the score still present

    def test_doubles_with_null_data_completeness(self, mock_client):
        mock_client.list_matches.return_value = as_matches(doubles_payload())
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "Salisbury/Ram vs Krawietz/Puetz" in doc.content
        assert "doubles match" in doc.content
        assert "#" not in doc.content  # no rankings on doubles teams
        assert "points 40-AD" in doc.content  # points are strings, incl. "AD"
        assert "Krawietz/Puetz is serving." in doc.content
        assert doc.meta["is_doubles"] is True
        assert doc.meta["p1_ranking"] is None

    def test_completed_match_names_winner(self, mock_client):
        mock_client.list_matches.return_value = as_matches(completed_payload())
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"), status="completed")
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "Completed; winner: Jannik Sinner (ITA, #1)." in doc.content
        assert "is serving" not in doc.content
        assert doc.meta["winner"] == 2

    def test_upcoming_match_shows_schedule(self, mock_client):
        mock_client.list_matches.return_value = as_matches(upcoming_payload())
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"), status="upcoming")
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "Scheduled for 2026-08-31T17:00:00+00:00." in doc.content
        assert doc.meta["scheduled_time"] == "2026-08-31T17:00:00+00:00"

    def test_run_overrides_init_defaults(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"), status="live", limit=10)
        fetcher._client = mock_client

        fetcher.run(status="completed", limit=2)

        mock_client.list_matches.assert_called_once_with(status="completed", limit=2)

    def test_limit_is_clamped_to_api_maximum(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        fetcher.run(limit=9999)

        mock_client.list_matches.assert_called_once_with(status="live", limit=200)

    def test_empty_listing(self, mock_client):
        mock_client.list_matches.return_value = []
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run()["documents"] == []

    def test_match_id_fetches_single_match(self, mock_client):
        mock_client.get_match.return_value = as_matches(live_singles_payload())[0]
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(match_id=101)["documents"]

        assert len(docs) == 1
        assert docs[0].meta["match_id"] == 101
        mock_client.get_match.assert_called_once_with(101)
        mock_client.list_matches.assert_not_called()

    def test_match_id_not_found_returns_empty(self, mock_client):
        mock_client.get_match.side_effect = NotFound("not_found", status_code=404)
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run(match_id=999999)["documents"] == []

    def test_tour_filter_uses_native_client_parameter(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"), tour="atp")
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        mock_client.list_matches.assert_called_once_with(status="live", limit=10, tour="atp")

    def test_new_filters_forwarded_to_client(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        fetcher.run(player=[1, 2], country="ned", from_="2026-08-01", to="2026-08-02")

        mock_client.list_matches.assert_called_once_with(
            status="live", limit=10, player=[1, 2], country="ned", from_="2026-08-01", to="2026-08-02"
        )

    def test_init_filter_defaults_used_when_run_omits_them(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"), player=7, country="sui")
        fetcher._client = mock_client

        fetcher.run()

        mock_client.list_matches.assert_called_once_with(status="live", limit=10, player=7, country="sui")

    def test_upgrade_required_becomes_readable_document(self, mock_client):
        mock_client.list_matches.side_effect = UpgradeRequired(
            "upgrade_required",
            status_code=403,
            body={"error": "upgrade_required"},
            required_tier="PRO",
        )
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        assert "requires the PRO tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"
        assert docs[0].meta["status_code"] == 403
        assert docs[0].meta["required_tier"] == "PRO"

    def test_other_errors_still_raise(self, mock_client):
        mock_client.list_matches.side_effect = RuntimeError("network down")
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(RuntimeError, match="network down"):
            fetcher.run()

    def test_run_rejects_bad_runtime_status(self, mock_client):
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(ValueError, match="status"):
            fetcher.run(status="paused")


class TestQuota429s:
    def test_daily_cap_becomes_readable_document(self, mock_client):
        mock_client.list_matches.side_effect = RateLimited(
            "rate_limited",
            status_code=429,
            body={"error": "rate_limited", "scope": "day", "limit_per_day": 100, "resets_at": "2026-08-07T21:00:00Z"},
            retry_after=3600.0,
        )
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        assert "daily quota exhausted" in docs[0].content
        assert docs[0].meta["error"] == "rate_limited"
        assert docs[0].meta["scope"] == "day"
        assert docs[0].meta["limit_per_day"] == 100
        assert docs[0].meta["resets_at"] == "2026-08-07T21:00:00+00:00"

    def test_abuse_throttle_becomes_readable_document(self, mock_client):
        mock_client.list_matches.side_effect = AbuseThrottled(
            "abuse_throttled",
            status_code=429,
            body={"error": "abuse_throttled", "retry_at_epoch": 1754600400},
        )
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        assert "abuse-throttled" in docs[0].content
        assert "fix the retry loop" in docs[0].content
        assert docs[0].meta["error"] == "abuse_throttled"
        assert docs[0].meta["retry_at_epoch"] == 1754600400
        assert docs[0].meta["retry_at"] is not None

    def test_minute_window_429_still_raises(self, mock_client):
        """The per-minute window is transient and already retried by the client — fail loud."""
        mock_client.list_matches.side_effect = RateLimited(
            "rate_limited", status_code=429, body={"error": "rate_limited"}, retry_after=12.0
        )
        fetcher = LiveTennisMatchFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(RateLimited):
            fetcher.run()


class TestMatchToDocument:
    def test_score_missing_entirely(self):
        payload = live_singles_payload()
        payload["score"] = None
        doc = match_to_document(Match.from_dict(payload))
        assert "sets" not in doc.meta
        assert "Live now." in doc.content

    def test_has_analysis_and_has_market_true(self):
        # Every list row and the detail carry both booleans (every tier, since 2026-09-02).
        payload = live_singles_payload()
        payload.update({"has_analysis": True, "has_market": True})
        doc = match_to_document(Match.from_dict(payload))
        assert doc.meta["has_analysis"] is True
        assert doc.meta["has_market"] is True

    def test_has_analysis_and_has_market_false(self):
        # False is an answer: nothing computed / no market mapped — filter the slate on it
        # instead of spending a 404 on /matches/{id}/analysis or /markets/{id}/prices.
        payload = live_singles_payload()
        payload.update({"has_analysis": False, "has_market": False})
        doc = match_to_document(Match.from_dict(payload))
        assert doc.meta["has_analysis"] is False
        assert doc.meta["has_market"] is False

    def test_has_analysis_and_has_market_absent_means_none(self):
        # A server that predates the field sends neither: the keys are still present, as None.
        doc = match_to_document(Match.from_dict(live_singles_payload()))
        assert "has_analysis" in doc.meta
        assert "has_market" in doc.meta
        assert doc.meta["has_analysis"] is None
        assert doc.meta["has_market"] is None


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    fetcher = LiveTennisMatchFetcher(limit=3)
    result = fetcher.run()
    assert isinstance(result["documents"], list)
    for doc in result["documents"]:
        assert isinstance(doc, Document)
        assert doc.content


class TestNullPoints:
    def test_null_points_entries_are_omitted(self):
        """Seen in live data: a completed match with points [None, None]."""
        payload = completed_payload()
        payload["score"]["points"] = [None, None]
        doc = match_to_document(Match.from_dict(payload))
        assert "points" not in doc.content
        assert "None" not in doc.content
        assert doc.meta["points"] == [None, None]  # meta keeps the raw truth
