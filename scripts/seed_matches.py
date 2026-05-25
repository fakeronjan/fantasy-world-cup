"""One-off: seed the matches/ collection in Firestore from football-data.org.

Idempotent - re-running just refreshes the catalog. Knockout-stage matches
will appear empty (homeTeam/awayTeam = null) until the bracket is set
after group stage; re-run this script then to populate them.

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json \
    ./venv/bin/python scripts/seed_matches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import fd_get, firestore_client, normalize_match


def main() -> None:
    print("Fetching all WC 2026 matches from football-data.org…")
    payload = fd_get("/competitions/WC/matches")
    matches = payload.get("matches") or []
    print(f"  got {len(matches)} matches")

    db = firestore_client()
    batch = db.batch()
    written = 0
    for m in matches:
        normalized = normalize_match(m)
        # Use the football-data match id as our doc id. This makes
        # ingest_results.py idempotent: we always write into the same doc.
        doc_id = str(normalized["fdId"])
        ref = db.collection("matches").document(doc_id)
        # Only set fields that come from this source; merge to preserve
        # admin overrides (scorers list, etc.) added elsewhere.
        batch.set(ref, normalized, merge=True)
        written += 1
        if written % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"Done. {written} matches synced to Firestore.")


if __name__ == "__main__":
    main()
