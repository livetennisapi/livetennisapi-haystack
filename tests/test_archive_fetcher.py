import os
from unittest.mock import MagicMock

import pytest
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import BadRequest, NotFound, UpgradeRequired
from livetennisapi.models import ArchiveCareer, ArchiveMatch, ArchivePlayerBio

from livetennisapi_haystack import LiveTennisArchiveFetcher


def archive_match_payload():
    return {
        "id": 555,
        "source_id": "1980-540",
        "tour": "atp",
        "level": "G",
        "tournament": "Wimbledon",
        "surface": "grass",
        "draw_size": 128,
        "event_date": "1980-06-23",
        "round": "F",
        "best_of": 5,
        "minutes": 235,
        "winner": {"name": "Bjorn Borg", "country": "SWE", "rank": 1, "seed": 1, "player_id": 100101},
        "loser": {"name": "John McEnroe", "country": "USA", "rank": 2, "seed": 2, "player_id": 100581},
        "score": "1-6 7-5 6-3 6-7(16) 8-6",
        "outcome": "completed",
    }


def sparse_archive_match_payload():
    """A 1970s row: no seeds, no ranks, no minutes — the era's silence."""
    return {
        "id": 556,
        "tour": "wta",
        "tournament": "US Open",
        "round": "SF",
        "winner": {"name": "Chris Evert"},
        "loser": {"name": "Unknown Player"},
        "score": "6-3 RET",
        "outcome": "retired",
    }


def bio_payload():
    return {
        "id": 100101,
        "tour": "atp",
        "name": "Bjorn Borg",
        "hand": "R",
        "dob": "1956-06-06",
        "country": "SWE",
        "height_cm": 180,
        "career_high_rank": 1,
        "career_high_date": "1977-08-23",
    }


def career_payload():
    return {
        "player": {"name": "Bjorn Borg"},
        "span": {"first": "1973-05-01", "last": "1993-06-01"},
        "record": {
            "wins": 654,
            "losses": 140,
            "titles": 66,
            "by_surface": {"clay": {"wins": 245, "losses": 41}, "grass": {"wins": 78, "losses": 14}},
        },
        "by_year": [{"year": 1980, "wins": 70, "losses": 6}],
        "serve": {"matches_with_stats": 120, "aces": 300, "double_faults": 150, "first_in_pct": 65.2},
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.list_archive_matches.return_value = [ArchiveMatch.from_dict(archive_match_payload())]
    client.list_archive_players.return_value = [ArchivePlayerBio.from_dict(bio_payload())]
    client.get_archive_career.return_value = ArchiveCareer.from_dict(career_payload())
    return client


class TestInitAndSerialization:
    def test_init_default(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisArchiveFetcher()
        assert fetcher.mode == "matches"
        assert fetcher.tour is None
        assert fetcher.limit == 10

    def test_init_rejects_bad_mode(self):
        with pytest.raises(ValueError, match="mode"):
            LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="tournaments")

    def test_init_rejects_bad_tour(self):
        with pytest.raises(ValueError, match="tour"):
            LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), tour="exhibition")

    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisArchiveFetcher(mode="players", tour="wta", limit=5)
        data = component_to_dict(fetcher, "archive")
        assert data["type"] == "livetennisapi_haystack.archive_fetcher.LiveTennisArchiveFetcher"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert init["mode"] == "players"
        assert init["tour"] == "wta"
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisArchiveFetcher(mode="career", limit=3)
        restored = component_from_dict(LiveTennisArchiveFetcher, component_to_dict(original, "archive"), "archive")
        assert restored.mode == "career"
        assert restored.limit == 3


class TestMatchesMode:
    def test_archive_match_document(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(name="borg")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert "Bjorn Borg (SWE, #1, seed 1) d. John McEnroe (USA, #2, seed 2)" in doc.content
        assert "Wimbledon, grass, round F" in doc.content
        assert "tournament started 1980-06-23" in doc.content
        assert "Score: 1-6 7-5 6-3 6-7(16) 8-6." in doc.content
        assert "Duration: 235 minutes." in doc.content
        assert doc.meta["archive_id"] == 555
        assert doc.meta["source_id"] == "1980-540"
        assert doc.meta["winner_player_id"] == 100101
        assert doc.meta["outcome"] == "completed"

    def test_filters_forwarded_to_client(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        fetcher.run(name="borg", tour="atp", from_="1980-01-01", to="1980-12-31", round="F", level="G", limit=5)

        mock_client.list_archive_matches.assert_called_once_with(
            tour="atp", name="borg", from_="1980-01-01", to="1980-12-31", round="F", level="G", limit=5
        )

    def test_sparse_row_tolerated(self, mock_client):
        mock_client.list_archive_matches.return_value = [ArchiveMatch.from_dict(sparse_archive_match_payload())]
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run()["documents"][0]

        assert "Chris Evert d. Unknown Player" in doc.content
        assert "Score: 6-3 RET (retired)." in doc.content
        assert "seed" not in doc.content
        assert "Duration" not in doc.content


class TestPlayersMode:
    def test_bio_document(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="players")
        fetcher._client = mock_client

        docs = fetcher.run(name="borg")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert doc.content.startswith("Bjorn Borg (results archive 1968-2022)")
        assert "SWE" in doc.content
        assert "right-handed" in doc.content
        assert "born 1956-06-06" in doc.content
        assert "career-high rank #1 (first reached 1977-08-23)" in doc.content
        assert doc.meta["archive_player_id"] == 100101
        mock_client.list_archive_players.assert_called_once_with("borg", tour=None, limit=10)


class TestCareerMode:
    def test_career_document(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="career")
        fetcher._client = mock_client

        docs = fetcher.run(name="borg")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert "Career of Bjorn Borg in the results archive (1968-2022), active 1973-1993." in doc.content
        assert "Record: 654-140, 66 titles." in doc.content
        assert "clay 245-41" in doc.content
        assert "over 120 matches with serve statistics" in doc.content
        assert "300 aces" in doc.content
        assert doc.meta["record"]["wins"] == 654
        mock_client.get_archive_career.assert_called_once_with("borg")

    def test_career_requires_name(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="career")
        fetcher._client = mock_client

        with pytest.raises(ValueError, match="name"):
            fetcher.run()

    def test_unknown_name_yields_empty(self, mock_client):
        mock_client.get_archive_career.side_effect = NotFound("not_found", status_code=404)
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="career")
        fetcher._client = mock_client

        assert fetcher.run(name="nobody")["documents"] == []

    def test_ambiguous_name_becomes_readable_document(self, mock_client):
        mock_client.get_archive_career.side_effect = BadRequest(
            "ambiguous_name",
            status_code=400,
            body={"error": "ambiguous_name", "candidates": ["Bjorn Borg", "Bjorn Borgson"]},
        )
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"), mode="career")
        fetcher._client = mock_client

        docs = fetcher.run(name="bjo")["documents"]

        assert len(docs) == 1
        assert docs[0].meta["error"] == "ambiguous_name"
        assert docs[0].meta["candidates"] == ["Bjorn Borg", "Bjorn Borgson"]


class TestErrors:
    def test_upgrade_required_becomes_readable_document(self, mock_client):
        mock_client.list_archive_matches.side_effect = UpgradeRequired(
            "upgrade_required", status_code=403, body={"error": "upgrade_required"}, required_tier="BASIC"
        )
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run()["documents"]

        assert len(docs) == 1
        assert "requires the BASIC tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"

    def test_run_rejects_bad_runtime_mode(self, mock_client):
        fetcher = LiveTennisArchiveFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(ValueError, match="mode"):
            fetcher.run(mode="rallies")


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    fetcher = LiveTennisArchiveFetcher(limit=3)
    result = fetcher.run(name="borg")
    assert isinstance(result["documents"], list)
