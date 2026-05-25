"""Merge scraped + manually-overridden player history into the final file.

Inputs:
  docs/data/player_history_scraped.json   — produced by scrape_player_wikipedia.py
  docs/data/player_history_overrides.json — hand-curated, takes precedence

Output:
  docs/data/player_history.json           — what the frontend reads

Override keys use the same player.id format as seed_players.json
(i.e., "{fdId}-{teamSlug}"). This makes merge unambiguous.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRAPED   = ROOT / "docs" / "data" / "player_history_scraped.json"
OVERRIDES = ROOT / "docs" / "data" / "player_history_overrides.json"
OUT       = ROOT / "docs" / "data" / "player_history.json"


def main() -> None:
    scraped = json.loads(SCRAPED.read_text()) if SCRAPED.exists() else {}
    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}

    merged: dict[str, dict] = {}
    for k, v in scraped.items():
        merged[k] = v
    for k, v in overrides.items():
        # Overrides fully replace the scraped record for that player.
        # Tag the source so future debugging is easier.
        v = {**v, "_source": "manual"}
        merged[k] = v

    # Tag any non-overridden scraped entries so we can distinguish them.
    for k, v in merged.items():
        if "_source" not in v:
            v["_source"] = "scraped"

    OUT.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    n_scraped  = sum(1 for v in merged.values() if v.get("_source") == "scraped")
    n_manual   = sum(1 for v in merged.values() if v.get("_source") == "manual")
    print(f"Wrote {len(merged)} entries → {OUT}")
    print(f"  scraped:        {n_scraped}")
    print(f"  manual overrides: {n_manual}")


if __name__ == "__main__":
    main()
