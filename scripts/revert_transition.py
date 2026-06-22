"""Manual override: revert a borked round transition.

Restores the pre-transition snapshot that ingest wrote to
transitionBackups/{round} - team/player prices + elimination flags, every
user's roster/budget/banked points, and the config round/window flags - and
deletes that round's auto-sell transactions.

A revert is only CLEAN before users trade. If any user transfers exist for the
round (the window opened and people traded), this refuses unless you pass
--force, because reverting then unwinds real trades. The in-app banner warns
users this can happen during a transition, so --force is fair - but it must be
a deliberate act, never silent.

Usage:
  # See what would change (always start here):
  GOOGLE_APPLICATION_CREDENTIALS=...sa.json \\
    ./venv/bin/python scripts/revert_transition.py --round R32 --dry-run

  # Revert (clean - no user trades yet):
  GOOGLE_APPLICATION_CREDENTIALS=...sa.json \\
    ./venv/bin/python scripts/revert_transition.py --round R32

  # Revert even though users have traded (unwinds those trades):
  GOOGLE_APPLICATION_CREDENTIALS=...sa.json \\
    ./venv/bin/python scripts/revert_transition.py --round R32 --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client
from firebase_admin import firestore


def user_trades_for_round(db, round_label: str) -> list:
    """User transfer transactions for this round. User transfers write a doc
    with round=<round> and NO `type` field; auto-sells carry
    type='auto-sell-elimination' and price-adjustments type='price-adjustment',
    so we exclude anything with a `type`."""
    out = []
    for tx in db.collection("transactions").where("round", "==", round_label).stream():
        d = tx.to_dict() or {}
        if d.get("type"):
            continue  # auto-sell / price-adjustment, not a user trade
        if d.get("buys") or d.get("sells"):
            out.append((tx.id, d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", required=True,
                    help="Round whose transition to revert (e.g. R32)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing")
    ap.add_argument("--force", action="store_true",
                    help="Revert even if users have already traded this round")
    args = ap.parse_args()

    db = firestore_client()

    snap_doc = db.collection("transitionBackups").document(args.round).get()
    if not snap_doc.exists:
        sys.exit(f"No snapshot at transitionBackups/{args.round}. Nothing to revert.")
    snap = snap_doc.to_dict() or {}
    teams = snap.get("teams") or {}
    players = snap.get("players") or {}
    users = snap.get("users") or {}
    cfg_prev = snap.get("config") or {}
    print(f"Snapshot from {snap.get('createdAt')}: "
          f"{len(teams)} teams, {len(players)} players, {len(users)} users")

    trades = user_trades_for_round(db, args.round)
    if trades:
        print(f"\n⚠️  {len(trades)} user transfer(s) already happened in {args.round}:")
        for tx_id, d in trades[:10]:
            print(f"    {d.get('uid','?')[:8]}  buys={len(d.get('buys') or [])} "
                  f"sells={len(d.get('sells') or [])}  @ {d.get('timestamp')}")
        if not args.force and not args.dry_run:
            sys.exit("\nRefusing: reverting now would unwind real trades. "
                     "Re-run with --force if that's intended.")
    else:
        print("\nNo user trades for this round - clean revert.")

    autosells = list(db.collection("transactions")
                     .where("round", "==", "auto-sell").stream())
    # Only the auto-sells from THIS transition (match by being elimination type).
    autosells = [t for t in autosells
                 if (t.to_dict() or {}).get("type") == "auto-sell-elimination"]

    print(f"\nWould restore {len(teams)} team + {len(players)} player price docs, "
          f"{len(users)} user docs, config -> {cfg_prev}, and delete "
          f"{len(autosells)} auto-sell transaction(s)"
          + (f" + {len(trades)} user trade(s)" if (args.force and trades) else "") + ".")

    if args.dry_run:
        print("\n[dry-run - nothing written.]")
        return

    batch = db.batch()
    n = 0

    def _flush():
        nonlocal batch, n
        batch.commit()
        batch = db.batch()
        n = 0

    for tid, fields in teams.items():
        batch.set(db.collection("teams").document(tid), fields, merge=True)
        n += 1
        if n >= 400:
            _flush()
    for pid, fields in players.items():
        batch.set(db.collection("players").document(pid), fields, merge=True)
        n += 1
        if n >= 400:
            _flush()
    for uid, fields in users.items():
        batch.set(db.collection("users").document(uid), fields, merge=True)
        n += 1
        if n >= 400:
            _flush()

    # Restore config to pre-transition and clear the transition flags.
    batch.set(db.collection("config").document("global"), {
        "currentRound":       cfg_prev.get("currentRound"),
        "transferWindowOpen": cfg_prev.get("transferWindowOpen", False),
        "transitionState":    False,
        "transitionRound":    firestore.DELETE_FIELD,
        "settleUntil":        firestore.DELETE_FIELD,
        "windowClosesAt":     firestore.DELETE_FIELD,
        "windowOpenedAt":     firestore.DELETE_FIELD,
    }, merge=True)
    n += 1

    for t in autosells:
        batch.delete(t.reference)
        n += 1
        if n >= 400:
            _flush()
    if args.force and trades:
        for tx_id, _ in trades:
            batch.delete(db.collection("transactions").document(tx_id))
            n += 1
            if n >= 400:
                _flush()

    batch.commit()
    print(f"\nReverted {args.round}. Config restored to {cfg_prev}. "
          f"Re-run ingest once the underlying issue is fixed to re-transition.")


if __name__ == "__main__":
    main()
