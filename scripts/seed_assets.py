"""Seed Firestore with the 48 teams + ~180 players for WC 2026.

Reads:  docs/data/seed_teams.json  and  docs/data/seed_players.json
Writes: teams/{teamId} and players/{playerId} collections in Firestore.

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json \
    ./venv/bin/python scripts/seed_assets.py [--dry-run]

The service-account key must have Firebase Admin SDK / Cloud Datastore
User permissions. Download one from the Firebase console:
  Project settings → Service accounts → Generate new private key.
Never commit this file (it's in .gitignore).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
SEED_TEAMS = DATA_DIR / "seed_teams.json"
SEED_PLAYERS = DATA_DIR / "seed_players.json"

# Initial scoring weights — must match SIMULATION_FINDINGS.md recommendations.
INITIAL_CONFIG = {
    "currentRound": "pre",            # "pre" | "group" | "R32" | "R16" | "QF" | "SF" | "F" | "done"
    "transferWindowOpen": False,
    "scoringWeights": {
        # Locked 2026-05-25 (Deep Data upgrade) — all integers.
        "team_win": 4,
        "team_draw": 1,
        "bonus_r32": 2,
        "bonus_r16": 3,
        "bonus_qf": 5,
        "bonus_sf": 8,
        "bonus_final": 12,
        "bonus_champion": 20,
        # Player side
        "player_goal": 5,
        "player_assist": 3,                # restored after Deep Data tier unlocked assist data
        "player_win_share": 1,             # +1 per match the player PLAYED IN that team won (lineup-based)
        "player_clean_sheet_gk": 5,        # +5 to GK who played in a clean-sheet match
        "player_clean_sheet_def": 2,       # +2 to defenders who played in a CS match
        "player_clean_sheet_other": 0,     # 0 — MID/FWD/Unknown don't earn CS (FPL-style)
    },
    "kickoffTimestamp": None,         # set to the WC 2026 kickoff datetime (Firestore Timestamp)
    "budget": 60,
    "rosterCap": 12,
    "playerMinPrice": 2,
}


def _load_json(path: Path, required: bool = True) -> list[dict]:
    if not path.exists():
        if required:
            sys.exit(f"missing seed file: {path}\n"
                     "Curate the 2026 tiers and write them to this path first "
                     "(see SIMULATION_FINDINGS.md for the recommended tier shape).")
        print(f"  (no {path.name} yet — skipping)")
        return []
    return json.loads(path.read_text())


def _init_firebase():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("firebase-admin not installed. Run:\n"
                 "  ./venv/bin/pip install -r scripts/requirements.txt")
    if not firebase_admin._apps:
        # Picks up GOOGLE_APPLICATION_CREDENTIALS from env automatically.
        firebase_admin.initialize_app(credentials.ApplicationDefault())
    return firestore.client()


def seed(dry_run: bool = False) -> None:
    teams = _load_json(SEED_TEAMS, required=True)
    players = _load_json(SEED_PLAYERS, required=False)

    print(f"Loaded {len(teams)} teams, {len(players)} players from seed files.")
    if not players:
        print("  (player tiers not yet curated — run pull_wc2026_squads.py + tier the players, then re-run this script)")

    if dry_run:
        print("[dry-run] Skipping Firestore writes. Sample team:")
        print(json.dumps(teams[0], indent=2))
        print("Sample player:")
        print(json.dumps(players[0], indent=2))
        return

    db = _init_firebase()

    print("Writing config/global ...")
    db.collection("config").document("global").set(INITIAL_CONFIG)

    print("Writing teams ...")
    batch = db.batch()
    for i, t in enumerate(teams):
        doc_id = t.get("id") or t["name"].lower().replace(" ", "-")
        ref = db.collection("teams").document(doc_id)
        # Initialize per-team running stats
        full = {
            **t,
            "currentPrice": t.get("basePrice", t.get("price", 3)),
            "eliminated": False,
            "finalRound": None,
            "matchesWon": 0,
            "matchesDrawn": 0,
            "matchesLost": 0,
            "goalsFor": 0,
            "goalsAgainst": 0,
            "totalPoints": 0,
        }
        batch.set(ref, full)
        if (i + 1) % 400 == 0:  # Firestore batch limit is 500
            batch.commit()
            batch = db.batch()
    batch.commit()

    print("Writing players ...")
    batch = db.batch()
    for i, p in enumerate(players):
        doc_id = p.get("id") or f"{p['name'].lower().replace(' ', '-')}-{p.get('teamId','x')}"
        ref = db.collection("players").document(doc_id)
        full = {
            **p,
            "currentPrice": p.get("basePrice", p.get("price", 2)),
            "eliminated": False,
            "goals": 0,
            "assists": 0,
            "cleanSheets": 0,
            "totalPoints": 0,
        }
        batch.set(ref, full)
        if (i + 1) % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()

    print(f"Done. Seeded {len(teams)} teams + {len(players)} players.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate seed files without writing to Firestore")
    args = parser.parse_args()
    seed(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
