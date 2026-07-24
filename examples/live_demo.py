"""Live demo: a real Haystack pipeline over the Live Tennis API.

Needs only LIVETENNISAPI_KEY in the environment:

    export LIVETENNISAPI_KEY="your-key"
    python examples/live_demo.py

Fetches live matches (falling back to upcoming, then completed, if no tennis is
on right now) plus a player search, and prints every Document.
"""

import os
import sys

from haystack import Pipeline

from livetennisapi_haystack import LiveTennisMatchFetcher, LiveTennisPlayerSearch


def main() -> int:
    if not os.environ.get("LIVETENNISAPI_KEY"):
        print("Set LIVETENNISAPI_KEY first.", file=sys.stderr)
        return 1

    pipe = Pipeline()
    pipe.add_component("matches", LiveTennisMatchFetcher(limit=10))
    pipe.add_component("players", LiveTennisPlayerSearch(limit=3))

    docs = []
    for status in ("live", "upcoming", "completed"):
        result = pipe.run({"matches": {"status": status}, "players": {"query": "alcaraz"}})
        docs = result["matches"]["documents"]
        if docs:
            print(f"== {status} matches ({len(docs)}) ==")
            break
        print(f"== no {status} matches right now ==")

    for doc in docs:
        print("-", doc.content)
        print("  meta:", {k: v for k, v in doc.meta.items() if v is not None})

    player_docs = result["players"]["documents"]
    print(f"\n== player search 'alcaraz' ({len(player_docs)}) ==")
    for doc in player_docs:
        print("-", doc.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
