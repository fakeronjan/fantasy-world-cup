"""Recompute currentPrice for every team and player.

Run manually between rounds — typically right after a knockout match
eliminates a team. v1 policy:
  - Eliminated team   → currentPrice = 0
  - Eliminated player → currentPrice = 0 (their team is out)
  - Survivor          → currentPrice = basePrice (no performance premium yet)

A future version will add a performance premium (e.g., currentPrice =
basePrice + min(totalPoints * k, basePrice * 0.5)) so hot picks both
score AND become more valuable to sell. For now, repricing just enforces
the elimination → $0 rule so the transfer page can reflect lost picks.

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=$PWD/<sa.json> \
    ./venv/bin/python scripts/reprice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client


def main() -> None:
    db = firestore_client()

    # Build a set of eliminated team slugs.
    eliminated_teams = set()
    batch = db.batch()
    n_teams = 0
    for tdoc in db.collection("teams").stream():
        t = tdoc.to_dict() or {}
        elim = bool(t.get("eliminated"))
        if elim:
            eliminated_teams.add(tdoc.id)
        base = t.get("basePrice", 0)
        new_price = 0 if elim else base
        batch.set(tdoc.reference, {"currentPrice": new_price}, merge=True)
        n_teams += 1
        if n_teams % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  repriced {n_teams} teams ({len(eliminated_teams)} eliminated → $0)")

    # Players inherit elimination from their team.
    batch = db.batch()
    n_players = 0
    elim_players = 0
    for pdoc in db.collection("players").stream():
        p = pdoc.to_dict() or {}
        elim = p.get("teamId") in eliminated_teams
        if elim:
            elim_players += 1
        base = p.get("basePrice", 0)
        new_price = 0 if elim else base
        batch.set(pdoc.reference, {"currentPrice": new_price, "eliminated": elim}, merge=True)
        n_players += 1
        if n_players % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  repriced {n_players} players ({elim_players} on eliminated teams → $0)")

    print("\nDone. Run scripts/ingest_results.py (or wait for cron) to refresh user totals.")


if __name__ == "__main__":
    main()
