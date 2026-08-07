import os
from unittest.mock import MagicMock

import pytest
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import UpgradeRequired
from livetennisapi.models import RankingRecord

from livetennisapi_haystack import LiveTennisRankingsFetcher


def atp_row():
    return {
        "player_id": 2,
        "player_name": "Jannik Sinner",
        "system": "atp",
        "tour": "atp",
        "rank": 1,
        "points": 11830,
        "previous_rank": 2,
        "effective_date": "2026-08-03",
    }


def unrostered_row():
    """A listing row for a player outside the roster: name present, id null — no silent holes."""
    return {
        "player_id": None,
        "player_name": "Some Qualifier",
        "system": "atp",
        "rank": 173,
        "points": 310,
        "effective_date": "2026-08-03",
    }


def itf_row():
    return {
        "player_id": 9,
        "player_name": "Junior Prospect",
        "system": "itf_jt",
        "rank": 4,
        "points": 900,
        "rank_movement": 3,
        "effective_date": "2026-08-04",
    }


def as_records(*payloads):
    return [RankingRecord.from_dict(p) for p in payloads]


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.list_rankings.return_value = as_records(atp_row())
    return client


class TestInitAndSerialization:
    def test_init_default(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisRankingsFetcher()
        assert fetcher.system == "atp"
        assert fetcher.as_of is None
        assert fetcher.limit == 10

    def test_init_rejects_bad_system(self):
        with pytest.raises(ValueError, match="system"):
            LiveTennisRankingsFetcher(api_key=Secret.from_token("k"), system="elo")

    def test_init_rejects_utr(self):
        """UTR has no listing — a rating, not a ranking."""
        with pytest.raises(ValueError, match="system"):
            LiveTennisRankingsFetcher(api_key=Secret.from_token("k"), system="utr")

    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisRankingsFetcher(system="wta", as_of="2026-08-01", limit=5)
        data = component_to_dict(fetcher, "rankings")
        assert data["type"] == "livetennisapi_haystack.rankings_fetcher.LiveTennisRankingsFetcher"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert init["system"] == "wta"
        assert init["as_of"] == "2026-08-01"
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisRankingsFetcher(system="itf_wt", limit=3)
        restored = component_from_dict(
            LiveTennisRankingsFetcher, component_to_dict(original, "rankings"), "rankings"
        )
        assert restored.system == "itf_wt"
        assert restored.limit == 3


class TestRun:
    def test_ranking_document(self, mock_client):
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert "ATP ranking #1: Jannik Sinner" in doc.content
        assert "11830 points" in doc.content
        assert "previous week #2" in doc.content
        assert "Effective 2026-08-03." in doc.content
        assert doc.meta["rank"] == 1
        assert doc.meta["system"] == "atp"
        assert doc.meta["previous_rank"] == 2
        mock_client.list_rankings.assert_called_once_with(system="atp", as_of=None, limit=10)

    def test_unrostered_row_keeps_name(self, mock_client):
        mock_client.list_rankings.return_value = as_records(unrostered_row())
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "ATP ranking #173: Some Qualifier" in doc.content
        assert "previous week" not in doc.content  # null previous_rank is omitted, never invented
        assert doc.meta["player_id"] is None
        assert doc.meta["player_name"] == "Some Qualifier"

    def test_itf_rank_movement(self, mock_client):
        mock_client.list_rankings.return_value = as_records(itf_row())
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"), system="itf_jt")
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "ITF junior ranking #4: Junior Prospect" in doc.content
        assert "moved +3 this week" in doc.content

    def test_run_overrides_and_clamp(self, mock_client):
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        fetcher.run(system="wta", as_of="2026-07-01", limit=9999)

        mock_client.list_rankings.assert_called_once_with(system="wta", as_of="2026-07-01", limit=200)

    def test_run_rejects_bad_runtime_system(self, mock_client):
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(ValueError, match="system"):
            fetcher.run(system="utr")

    def test_upgrade_required_becomes_readable_document(self, mock_client):
        """The listing is PRO-gated: FREE and BASIC keys get the readable Document."""
        mock_client.list_rankings.side_effect = UpgradeRequired(
            "upgrade_required", status_code=403, body={"error": "upgrade_required"}, required_tier="PRO"
        )
        fetcher = LiveTennisRankingsFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        assert "requires the PRO tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    fetcher = LiveTennisRankingsFetcher(limit=3)
    result = fetcher.run()
    assert isinstance(result["documents"], list)
