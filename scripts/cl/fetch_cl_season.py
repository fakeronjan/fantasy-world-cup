"""Champions League season harness puller (Phase 0 of the CL spin-off).

Pulls a full UEFA Champions League season from football-data.org into local
JSON so we have a deterministic, offline replay test bed for the pricing /
scoring engine. Reuses the existing football-data client in scripts/_fwc_lib.py.

Outputs (under data/cl/):
  cl<season>_matches.json   - raw match list (summary shape)
  cl<season>_details.json   - {matchId: full /matches/{id} detail} for FINISHED
  cl<season>_teams.json     - 36 clubs + squads

Caching: re-runs skip matches already in the details cache, so a partial run
resumes and repeat runs are cheap. Throttled to stay well under the rate limit.

Usage:
  GOOGLE_APPLICATION_CREDENTIALS is NOT needed (no Firestore writes).
  FOOTBALL_DATA_KEY from env or the Power Rankings .env (via _fwc_lib).
  python scripts/cl/fetch_cl_season.py --season 2025
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _fwc_lib import FD_BASE, fd_key  # noqa: E402

DATA_DIR = ROOT / "data" / "cl"
THROTTLE_S = 1.3          # ~46 req/min, comfortably under the 60/min ceiling
MAX_RETRIES = 4


def fd_get(path: str) -> dict:
    """GET with retry + backoff on 429, network errors, and connection resets
    (RemoteDisconnected/ConnectionError are OSError subclasses, not URLError)."""
    key = fd_key()
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(f"{FD_BASE}{path}", headers={"X-Auth-Token": key})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                wait = 10 * (attempt + 1)
                print(f"    429 rate-limited, backing off {wait}s…")
                time.sleep(wait)
                continue
            raise
        except (URLError, OSError) as e:  # incl. RemoteDisconnected / reset
            if attempt < MAX_RETRIES - 1:
                wait = 5 * (attempt + 1)
                print(f"    network error ({e}), retry {attempt+1} in {wait}s…")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"exhausted retries for {path}")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025",
                    help="Season start year (2025 = the 2025-26 season)")
    args = ap.parse_args()
    season = args.season
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    matches_path = DATA_DIR / f"cl{season}_matches.json"
    details_path = DATA_DIR / f"cl{season}_details.json"
    teams_path = DATA_DIR / f"cl{season}_teams.json"

    # 1. Match list -------------------------------------------------------
    print(f"Fetching CL {season} match list…")
    matches = fd_get(f"/competitions/CL/matches?season={season}").get("matches", [])
    save_json(matches_path, matches)
    finished = [m for m in matches if m.get("status") == "FINISHED"]
    print(f"  {len(matches)} matches ({len(finished)} FINISHED) -> {matches_path.name}")

    # 2. Per-match detail for FINISHED matches (cached/resumable) ----------
    details = load_json(details_path, {})
    todo = [m for m in finished if str(m["id"]) not in details]
    print(f"Fetching detail for {len(todo)} matches "
          f"({len(details)} already cached)…")
    for i, m in enumerate(todo, 1):
        mid = str(m["id"])
        d = fd_get(f"/matches/{mid}")
        details[mid] = d
        if i % 10 == 0 or i == len(todo):
            save_json(details_path, details)   # checkpoint
            print(f"  {i}/{len(todo)} cached")
        time.sleep(THROTTLE_S)
    save_json(details_path, details)
    print(f"  detail cache -> {details_path.name} ({len(details)} matches)")

    # 3. Teams + squads ---------------------------------------------------
    print(f"Fetching CL {season} teams + squads…")
    teams = fd_get(f"/competitions/CL/teams?season={season}").get("teams", [])
    save_json(teams_path, teams)
    squad_sizes = [len(t.get("squad", []) or []) for t in teams]
    print(f"  {len(teams)} teams -> {teams_path.name} "
          f"(squad sizes {min(squad_sizes, default=0)}-{max(squad_sizes, default=0)})")

    print("\nDone. Local CL season harness is populated.")


if __name__ == "__main__":
    main()
