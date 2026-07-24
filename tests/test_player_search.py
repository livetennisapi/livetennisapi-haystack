import os
from unittest.mock import MagicMock

import pytest
from haystack import Document
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import UpgradeRequired
from livetennisapi.models import Player

from livetennisapi_haystack import LiveTennisPlayerSearch


def ranked_player_payload():
    return {
        "id": 1,
        "name": "Carlos Alcaraz",
        "tour": "atp",
        "country": "ESP",
        "ranking": 2,
        "ranking_points": 8600,
        "ranking_movement": "up",
        "hand": "R",
        "backhand": 2,
        "birthday": "2003-05-05",
        "is_doubles_team": False,
        "data_completeness": {"known": 6, "of": 6, "missing": []},
    }


def sparse_doubles_team_payload():
    """Doubles team: no ranking/hand/birthday, data_completeness known/of null."""
    return {
        "id": 31,
        "name": "Salisbury/Ram",
        "tour": "ATP",
        "country": None,
        "ranking": None,
        "hand": None,
        "is_doubles_team": True,
        "data_completeness": {"known": None, "of": None, "note": "doubles team"},
    }


def as_players(*payloads):
    return [Player.from_dict(p) for p in payloads]


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.search_players.return_value = as_players(ranked_player_payload())
    return client


class TestInitAndSerialization:
    def test_init_default(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        search = LiveTennisPlayerSearch()
        assert search.limit == 10
        assert search.api_key.resolve_value() == "test-key"
        assert search._client is None

    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        search = LiveTennisPlayerSearch(limit=5)
        data = component_to_dict(search, "search")
        assert data["type"] == "livetennisapi_haystack.player_search.LiveTennisPlayerSearch"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert init["limit"] == 5
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisPlayerSearch(limit=3)
        restored = component_from_dict(LiveTennisPlayerSearch, component_to_dict(original, "search"), "search")
        assert restored.limit == 3
        assert restored.api_key.resolve_value() == "test-key"


class TestRun:
    def test_ranked_player_document(self, mock_client):
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        docs = search.run(query="alcaraz")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert isinstance(doc, Document)
        assert doc.content.startswith("Carlos Alcaraz — ")
        assert "ESP" in doc.content
        assert "ranked #2 (8600 pts), moving up" in doc.content
        assert "plays right-handed with a two-handed backhand" in doc.content
        assert "born 2003-05-05" in doc.content
        assert doc.meta["player_id"] == 1
        assert doc.meta["ranking"] == 2
        assert doc.meta["birthday"] == "2003-05-05"
        mock_client.search_players.assert_called_once_with("alcaraz", limit=10)

    def test_sparse_doubles_team_document(self, mock_client):
        mock_client.search_players.return_value = as_players(sparse_doubles_team_payload())
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        doc = search.run(query="salisbury")["documents"][0]

        assert doc.content.startswith("Salisbury/Ram — doubles team")
        assert "ranked" not in doc.content
        assert "plays" not in doc.content
        assert "born" not in doc.content
        assert doc.meta["is_doubles_team"] is True
        assert doc.meta["ranking"] is None

    def test_limit_override_and_clamp(self, mock_client):
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        search.run(query="a", limit=9999)

        mock_client.search_players.assert_called_once_with("a", limit=200)

    def test_empty_result(self, mock_client):
        mock_client.search_players.return_value = []
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        assert search.run(query="nobody-by-this-name")["documents"] == []

    def test_upgrade_required_becomes_readable_document(self, mock_client):
        mock_client.search_players.side_effect = UpgradeRequired(
            "upgrade_required", status_code=403, body={"error": "upgrade_required"}, required_tier="BASIC"
        )
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        docs = search.run(query="alcaraz")["documents"]

        assert len(docs) == 1
        assert "requires the BASIC tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"

    def test_other_errors_still_raise(self, mock_client):
        mock_client.search_players.side_effect = RuntimeError("network down")
        search = LiveTennisPlayerSearch(api_key=Secret.from_token("k"))
        search._client = mock_client

        with pytest.raises(RuntimeError, match="network down"):
            search.run(query="alcaraz")


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    search = LiveTennisPlayerSearch(limit=3)
    result = search.run(query="alcaraz")
    assert isinstance(result["documents"], list)
