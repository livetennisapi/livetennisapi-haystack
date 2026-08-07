import os
from unittest.mock import MagicMock

import pytest
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import NotFound, UpgradeRequired
from livetennisapi.models import MatchStatistics

from livetennisapi_haystack import LiveTennisMatchStatisticsFetcher


def statistics_payload():
    return {
        "match_id": 12345,
        "coverage": "live",
        "as_of": "3-2 15-30",
        "games_counted": 27,
        "sets_covered": [1, 2, 3],
        "freshness": {"derived": {"coverage": "live"}, "measured": {"coverage": "live"}},
        "players": {
            "p1": {
                "measured": {"aces": 10, "double_faults": 2, "first_serves_in_pct": 64},
                "service_games_played": 12,
                "service_games_won": 11,
                "hold_pct": 92,
                "break_points_faced": 4,
                "break_points_saved": 3,
                "break_points_played": 6,
                "break_points_converted": 2,
                "points_played": 140,
                "points_won": 75,
            },
            # An ITF-style sparse side: only tier-1 measured fields exist.
            "p2": {"measured": {"aces": 3}},
        },
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_match_statistics.return_value = MatchStatistics.from_dict(statistics_payload())
    return client


class TestInitAndSerialization:
    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisMatchStatisticsFetcher()
        data = component_to_dict(fetcher, "stats")
        assert data["type"] == "livetennisapi_haystack.statistics_fetcher.LiveTennisMatchStatisticsFetcher"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisMatchStatisticsFetcher(timeout=10.0)
        restored = component_from_dict(
            LiveTennisMatchStatisticsFetcher, component_to_dict(original, "stats"), "stats"
        )
        assert restored.timeout == 10.0
        assert restored.api_key.resolve_value() == "test-key"


class TestRun:
    def test_statistics_document(self, mock_client):
        fetcher = LiveTennisMatchStatisticsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(match_id=12345, p1_name="Alcaraz", p2_name="Sinner")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert "Match statistics for match 12345." in doc.content
        assert "Alcaraz: 10 aces, 2 double faults, 64% first serves in" in doc.content
        assert "won 11/12 service games (92% hold)" in doc.content
        assert "saved 3/4 break points faced" in doc.content
        assert "converted 2/6 break points" in doc.content
        assert "won 75/140 points" in doc.content
        assert "Sinner: 3 aces." in doc.content
        assert doc.meta["match_id"] == 12345
        assert doc.meta["coverage"] == "live"
        assert doc.meta["p1_statistics"]["measured"]["aces"] == 10
        mock_client.get_match_statistics.assert_called_once_with(12345)

    def test_default_player_labels(self, mock_client):
        fetcher = LiveTennisMatchStatisticsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run(match_id=12345)["documents"][0]

        assert "Player 1: 10 aces" in doc.content
        assert "Player 2: 3 aces." in doc.content

    def test_null_players_yields_empty(self, mock_client):
        """200 with null players is the API's honest 'we hold nothing' — not an error."""
        mock_client.get_match_statistics.return_value = MatchStatistics.from_dict(
            {"match_id": 99, "coverage": "none", "players": None}
        )
        fetcher = LiveTennisMatchStatisticsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run(match_id=99)["documents"] == []

    def test_not_found_yields_empty(self, mock_client):
        mock_client.get_match_statistics.side_effect = NotFound("not_found", status_code=404)
        fetcher = LiveTennisMatchStatisticsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run(match_id=999999)["documents"] == []

    def test_upgrade_required_becomes_readable_document(self, mock_client):
        """Statistics are ULTRA-gated: everyone below gets the readable Document."""
        mock_client.get_match_statistics.side_effect = UpgradeRequired(
            "upgrade_required", status_code=403, body={"error": "upgrade_required"}, required_tier="ULTRA"
        )
        fetcher = LiveTennisMatchStatisticsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(match_id=12345)["documents"]

        assert len(docs) == 1
        assert "requires the ULTRA tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    fetcher = LiveTennisMatchStatisticsFetcher()
    # Any id works for the shape check: unknown ids and non-entitled tiers both
    # produce a list (empty, or a single notice Document).
    result = fetcher.run(match_id=1)
    assert isinstance(result["documents"], list)
