"""Shared helpers for the seed_/ingest_ scripts.

Centralizes: Firebase Admin init, football-data.org client, config doc
access. Keeps the per-script files lean.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

FD_BASE = "https://api.football-data.org/v4"
PR_ENV_PATH = Path.home() / "code/fakeronjan/sports/zidane/.env"


# ---------------------------------------------------------------------------
# football-data.org
# ---------------------------------------------------------------------------

def fd_key() -> str:
    """Load FOOTBALL_DATA_KEY from env, falling back to the Power Rankings .env."""
    v = os.environ.get("FOOTBALL_DATA_KEY")
    if v:
        return v
    if PR_ENV_PATH.exists():
        for line in PR_ENV_PATH.read_text().splitlines():
            if line.startswith("FOOTBALL_DATA_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("FOOTBALL_DATA_KEY not in env and not in Power Rankings .env")


def fd_get(path: str) -> dict:
    """GET an endpoint on football-data.org v4 with our key."""
    req = Request(f"{FD_BASE}{path}", headers={"X-Auth-Token": fd_key()})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Firebase Admin
# ---------------------------------------------------------------------------

def firestore_client():
    """Init Firebase Admin SDK and return a Firestore client.

    Requires GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service
    account JSON key file.

    Get one from: Firebase Console → Project Settings → Service Accounts
                  → Generate new private key. Save the JSON file somewhere
                  outside the repo. NEVER commit it.
    """
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("firebase-admin not installed. Run:\n"
                 "  ./venv/bin/pip install -r scripts/requirements.txt")

    if not firebase_admin._apps:
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            sys.exit(
                "GOOGLE_APPLICATION_CREDENTIALS env var not set.\n"
                "Download a service-account JSON key from Firebase Console\n"
                "(Project Settings → Service Accounts → Generate new private key)\n"
                "and run with:\n"
                "  GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \\\n"
                "    ./venv/bin/python scripts/<script>.py"
            )
        firebase_admin.initialize_app(credentials.ApplicationDefault())
    return firestore.client()


# ---------------------------------------------------------------------------
# Match shape normalization
# ---------------------------------------------------------------------------

def normalize_match(m: dict) -> dict:
    """Convert a football-data.org match payload to our Firestore shape."""
    score = m.get("score", {}) or {}
    full = score.get("fullTime", {}) or {}
    stage = m.get("stage") or "GROUP_STAGE"
    return {
        "fdId": m["id"],
        "round": _our_round(stage),
        "team1Id": (m.get("homeTeam") or {}).get("id"),
        "team1Name": (m.get("homeTeam") or {}).get("name"),
        "team2Id": (m.get("awayTeam") or {}).get("id"),
        "team2Name": (m.get("awayTeam") or {}).get("name"),
        "score1": full.get("home"),
        "score2": full.get("away"),
        "winner": score.get("winner"),   # "HOME_TEAM" | "AWAY_TEAM" | "DRAW" | null
        "status": m.get("status"),       # "TIMED" | "IN_PLAY" | "PAUSED" | "FINISHED" | etc.
        "kickoff": m.get("utcDate"),
        "stage": stage,
        "group": m.get("group"),
        "lastUpdated": m.get("lastUpdated"),
    }


_STAGE_MAP = {
    "GROUP_STAGE": "group",
    "LAST_32":     "R32",
    "LAST_16":     "R16",
    "ROUND_OF_16": "R16",
    "QUARTER_FINALS": "QF",
    "SEMI_FINALS":   "SF",
    "THIRD_PLACE":   "third",
    "FINAL":         "F",
}

def _our_round(stage: str) -> str:
    return _STAGE_MAP.get(stage or "", stage or "group")
