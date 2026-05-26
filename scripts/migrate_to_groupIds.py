"""One-off migration: convert user.groupId (string) to user.groupIds (array).

For multi-group support. Idempotent - skips users already migrated.

Run from project root:
  GOOGLE_APPLICATION_CREDENTIALS=$PWD/<sa>.json \\
    ./venv/bin/python scripts/migrate_to_groupIds.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = firestore_client()
    n_total = n_migrated = n_skipped = 0
    for udoc in db.collection("users").stream():
        n_total += 1
        u = udoc.to_dict() or {}
        if "groupIds" in u:
            n_skipped += 1
            continue
        old = (u.get("groupId") or "").strip()
        new_groups = [old] if old else []
        name = u.get("leagueNickname") or u.get("displayName") or udoc.id
        print(f"  {name:<28} groupId={old!r}  →  groupIds={new_groups!r}")
        if not args.dry_run:
            udoc.reference.set({"groupIds": new_groups}, merge=True)
        n_migrated += 1

    print(f"\nTotal users: {n_total}")
    print(f"  migrated: {n_migrated}")
    print(f"  already had groupIds: {n_skipped}")
    if args.dry_run:
        print("\n(dry-run - nothing written)")


if __name__ == "__main__":
    main()
