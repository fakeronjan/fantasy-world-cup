"""One-off make-good: retroactively reprice everyone's existing picks
to the current basePrice and refund the difference to their budget.

Used after a global price adjustment (Option A) while the game is still
in testing - so testers don't feel locked into old (higher) prices.

For each user:
  1. For each pick, set purchasePrice = current basePrice
  2. Recompute totalSpent = sum of new purchasePrices
  3. Set currentBudget = BUDGET - totalSpent
  4. Write a 'price-adjustment' transaction so there's an audit trail

Idempotent: if a pick's purchasePrice already matches the current
basePrice, no change is recorded for it.

Run from project root:
  GOOGLE_APPLICATION_CREDENTIALS=$PWD/<sa>.json \\
    ./venv/bin/python scripts/repricing_makegood.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client

BUDGET = 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print changes but don't write to Firestore")
    args = ap.parse_args()

    db = firestore_client()

    # Build lookup of current basePrices
    base_price = {}
    for tdoc in db.collection("teams").stream():
        base_price[("team", tdoc.id)] = (tdoc.to_dict() or {}).get("basePrice", 0)
    for pdoc in db.collection("players").stream():
        base_price[("player", pdoc.id)] = (pdoc.to_dict() or {}).get("basePrice", 0)
    print(f"Loaded {len(base_price)} asset basePrices from Firestore.\n")

    now_iso = datetime.now(timezone.utc).isoformat()
    n_users_affected = 0
    n_users_total = 0
    grand_refund = 0

    for udoc in db.collection("users").stream():
        n_users_total += 1
        u = udoc.to_dict() or {}
        roster = u.get("roster") or []
        if not roster:
            continue

        # Compute the deltas
        adjustments = []
        new_roster = []
        for pick in roster:
            key = (pick["kind"], pick["assetId"])
            current_base = base_price.get(key)
            if current_base is None:
                # Asset removed from catalog - leave purchasePrice alone
                new_roster.append(pick)
                continue
            old_paid = pick.get("purchasePrice", 0)
            new_pick = dict(pick)
            new_pick["purchasePrice"] = current_base
            new_roster.append(new_pick)
            if current_base != old_paid:
                adjustments.append({
                    "kind":   pick["kind"],
                    "assetId": pick["assetId"],
                    "oldPrice": old_paid,
                    "newPrice": current_base,
                    "refund":  old_paid - current_base,  # positive = user gets money back
                })

        if not adjustments:
            continue  # all picks already at current price

        new_total_spent = sum(p["purchasePrice"] for p in new_roster)
        new_current_budget = BUDGET - new_total_spent
        total_refund_for_user = sum(a["refund"] for a in adjustments)
        grand_refund += total_refund_for_user

        name = u.get("leagueNickname") or u.get("displayName") or udoc.id
        print(f"  {name:<30} {len(adjustments):>2} picks adjusted, net refund ${total_refund_for_user:>+3}, new budget ${new_current_budget}")
        for a in adjustments:
            sign = "+" if a["refund"] > 0 else ""
            print(f"      {a['kind']:<6} {a['assetId']:<26} ${a['oldPrice']} → ${a['newPrice']} ({sign}${a['refund']})")

        if not args.dry_run:
            udoc.reference.set({
                "roster":        new_roster,
                "totalSpent":    new_total_spent,
                "currentBudget": new_current_budget,
            }, merge=True)

            tx_ref = db.collection("transactions").document()
            tx_ref.set({
                "uid":       udoc.id,
                "round":     "pre",
                "timestamp": now_iso,
                "type":      "price-adjustment",
                "sells":     [],
                "buys":      [],
                "adjustments": adjustments,
                "totalRefund": total_refund_for_user,
                "note":       "Retroactive make-good after global pricing change.",
            })

        n_users_affected += 1

    print(f"\n{'='*60}")
    print(f"{n_users_affected} of {n_users_total} users adjusted")
    print(f"Total refunds across users: ${grand_refund}")
    if args.dry_run:
        print("\n(dry-run - nothing written. Re-run without --dry-run to apply.)")


if __name__ == "__main__":
    main()
