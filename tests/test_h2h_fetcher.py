import os
from unittest.mock import MagicMock

import pytest
from haystack import Document
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.utils import Secret
from livetennisapi.errors import BadRequest, NotFound, UpgradeRequired
from livetennisapi.models import HeadToHead

from livetennisapi_haystack import LiveTennisH2HFetcher


def h2h_payload():
    return {
        "players": {"p1": {"name": "Roger Federer"}, "p2": {"name": "Rafael Nadal"}},
        "totals": {"p1_wins": 16, "p2_wins": 24, "meetings": 41, "undecided": 1},
        "by_surface": {
            "clay": {"p1": 2, "p2": 14},
            "hard": {"p1": 11, "p2": 9},
            "grass": {"p1": 3, "p2": 1},
        },
        "meetings": [
            {
                "era": "current",
                "date": "2026-06-08",
                "tournament": "Roland Garros",
                "round": "F",
                "surface": "clay",
                "score": "6-3 6-4 6-2",
                "outcome": "completed",
                "winner": 2,
            },
            {
                "era": "archive",
                "date": None,
                "tournament": "Wimbledon",
                "round": "SF",
                "surface": "grass",
                "score": "W/O",
                "outcome": "walkover",
                "winner": None,
            },
        ],
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_h2h.return_value = HeadToHead.from_dict(h2h_payload())
    return client


class TestInitAndSerialization:
    def test_init_default(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisH2HFetcher()
        assert fetcher.meetings == 10
        assert fetcher.api_key.resolve_value() == "test-key"
        assert fetcher._client is None

    def test_to_dict_keeps_secret_as_env_var_reference(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        fetcher = LiveTennisH2HFetcher(meetings=5)
        data = component_to_dict(fetcher, "h2h")
        assert data["type"] == "livetennisapi_haystack.h2h_fetcher.LiveTennisH2HFetcher"
        init = data["init_parameters"]
        assert init["api_key"] == {"env_vars": ["LIVETENNISAPI_KEY"], "strict": True, "type": "env_var"}
        assert init["meetings"] == 5
        assert "test-key" not in str(data)

    def test_from_dict_roundtrip(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "test-key")
        original = LiveTennisH2HFetcher(meetings=3)
        restored = component_from_dict(LiveTennisH2HFetcher, component_to_dict(original, "h2h"), "h2h")
        assert restored.meetings == 3
        assert restored.api_key.resolve_value() == "test-key"


class TestRun:
    def test_h2h_document(self, mock_client):
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(p1="federer", p2="nadal")["documents"]

        assert len(docs) == 1
        doc = docs[0]
        assert isinstance(doc, Document)
        assert "Rafael Nadal leads 24-16" in doc.content
        assert "41 meetings" in doc.content
        assert "(1 with no derivable winner)" in doc.content
        assert "clay 2-14" in doc.content  # p1 first, as labelled
        assert "Roland Garros" in doc.content
        assert "Rafael Nadal won 6-3 6-4 6-2" in doc.content
        assert "(walkover)" in doc.content
        assert doc.meta["p1_name"] == "Roger Federer"
        assert doc.meta["p2_wins"] == 24
        assert doc.meta["undecided"] == 1
        assert len(doc.meta["recent_meetings"]) == 2
        mock_client.get_h2h.assert_called_once_with("federer", "nadal")

    def test_level_record(self, mock_client):
        payload = h2h_payload()
        payload["totals"] = {"p1_wins": 2, "p2_wins": 2, "meetings": 4, "undecided": 0}
        mock_client.get_h2h.return_value = HeadToHead.from_dict(payload)
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        doc = fetcher.run(p1="a", p2="b")["documents"][0]

        assert "level at 2-2" in doc.content

    def test_meetings_cap_applies_to_content_and_meta(self, mock_client):
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"), meetings=1)
        fetcher._client = mock_client

        doc = fetcher.run(p1="federer", p2="nadal")["documents"][0]

        assert len(doc.meta["recent_meetings"]) == 1
        assert "Wimbledon" not in doc.content  # the second meeting is cut
        assert doc.meta["meetings"] == 41  # totals still cover the full record

    def test_no_matching_players_yields_empty(self, mock_client):
        mock_client.get_h2h.return_value = HeadToHead.from_dict({"players": None, "totals": None, "meetings": []})
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run(p1="zzz", p2="qqq")["documents"] == []

    def test_not_found_yields_empty(self, mock_client):
        mock_client.get_h2h.side_effect = NotFound("not_found", status_code=404)
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        assert fetcher.run(p1="zzz", p2="qqq")["documents"] == []

    def test_ambiguous_name_becomes_readable_document(self, mock_client):
        mock_client.get_h2h.side_effect = BadRequest(
            "ambiguous_name",
            status_code=400,
            body={"error": "ambiguous_name", "candidates": ["Serena Williams", "Venus Williams"]},
        )
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(p1="williams", p2="sharapova")["documents"]

        assert len(docs) == 1
        assert "Serena Williams, Venus Williams" in docs[0].content
        assert docs[0].meta["error"] == "ambiguous_name"
        assert docs[0].meta["candidates"] == ["Serena Williams", "Venus Williams"]

    def test_other_bad_request_still_raises(self, mock_client):
        mock_client.get_h2h.side_effect = BadRequest("bad_request", status_code=400, body={"error": "bad_request"})
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        with pytest.raises(BadRequest):
            fetcher.run(p1="ab", p2="cd")

    def test_upgrade_required_becomes_readable_document(self, mock_client):
        mock_client.get_h2h.side_effect = UpgradeRequired(
            "upgrade_required", status_code=403, body={"error": "upgrade_required"}, required_tier="BASIC"
        )
        fetcher = LiveTennisH2HFetcher(api_key=Secret.from_token("k"))
        fetcher._client = mock_client

        docs = fetcher.run(p1="federer", p2="nadal")["documents"]

        assert len(docs) == 1
        assert "requires the BASIC tier" in docs[0].content
        assert docs[0].meta["error"] == "upgrade_required"


@pytest.mark.skipif(
    not os.environ.get("LIVETENNISAPI_KEY"),
    reason="Export LIVETENNISAPI_KEY to run integration tests.",
)
@pytest.mark.integration
def test_run_integration():
    fetcher = LiveTennisH2HFetcher()
    result = fetcher.run(p1="alcaraz", p2="sinner")
    assert isinstance(result["documents"], list)
