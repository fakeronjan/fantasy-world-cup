"""Pull all 48 WC 2026 team squads from football-data.org.

Output: docs/data/wc2026_squads.json  (full per-team squad with positions)
        docs/data/wc2026_teams.json   (team metadata: id, name, tla, crest URL)

Rate-limit aware (free tier = 10 calls/min). Sleeps between calls so the
whole pull takes ~6 minutes for 48 teams.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.football-data.org/v4"

# Read the key from the local zidane .env (shares FOOTBALL_DATA_KEY); env var wins.
ENV_PATH = Path.home() / "code/fakeronjan/sports/zidane/.env"


def _load_key() -> str:
    env = os.environ.get("FOOTBALL_DATA_KEY")
    if env:
        return env
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("FOOTBALL_DATA_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("FOOTBALL_DATA_KEY not found in env or Power Rankings .env")


def fetch(path: str, key: str) -> dict:
    req = Request(f"{API_BASE}{path}", headers={"X-Auth-Token": key})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    key = _load_key()

    print("Fetching WC 2026 team list...")
    teams_response = fetch("/competitions/WC/teams", key)
    teams = teams_response.get("teams", [])
    print(f"  found {len(teams)} teams")

    # Save the trimmed team metadata
    team_meta = [
        {
            "id": t["id"],
            "name": t["name"],
            "shortName": t.get("shortName"),
            "tla": t.get("tla"),
            "crest": t.get("crest"),
        }
        for t in teams
    ]
    (OUT_DIR / "wc2026_teams.json").write_text(
        json.dumps(team_meta, indent=2, ensure_ascii=False)
    )
    print(f"  saved teams → {OUT_DIR / 'wc2026_teams.json'}")

    # Pull each squad
    squads = {}
    for i, t in enumerate(teams, 1):
        # Throttle to ~9 calls/min to stay under the 10/min limit
        if i > 1:
            time.sleep(7)
        try:
            print(f"  [{i}/{len(teams)}] {t['name']}...", end="", flush=True)
            data = fetch(f"/teams/{t['id']}", key)
            squad = data.get("squad", []) or []
            squads[t["name"]] = {
                "id": t["id"],
                "tla": t.get("tla"),
                "squad": [
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "position": p.get("position", "Unknown"),
                        "dateOfBirth": p.get("dateOfBirth"),
                        "nationality": p.get("nationality"),
                    }
                    for p in squad
                ],
                "coach": (data.get("coach") or {}).get("name"),
            }
            print(f" {len(squad)} players")
        except Exception as e:
            print(f" ! {e}")

    out_path = OUT_DIR / "wc2026_squads.json"
    out_path.write_text(json.dumps(squads, indent=2, ensure_ascii=False))
    print(f"\nDone. Saved {len(squads)} squads → {out_path}")


if __name__ == "__main__":
    main()
