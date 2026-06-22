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


# ---------------------------------------------------------------------------
# Knockout-bracket advancement (pure helpers - no Firestore / no network)
# ---------------------------------------------------------------------------
#
# Source of truth at a round boundary is the BRACKET, not finalRound.
# A team's finalRound only reaches round X *after* it has played an X match,
# so finalRound cannot identify who advanced at the instant a round completes.
# The upstream feed slots the surviving teams into the next round's fixtures
# as soon as the bracket is officially decided - that's our signal.

# How many fixtures each knockout round has in the 48-team format. Used to
# detect a fully-seeded bracket (all slots filled) before we act on it.
KO_FIXTURES_PER_ROUND = {"R32": 16, "R16": 8, "QF": 4, "SF": 2, "F": 1}


def round_fixtures(matches: list[dict], round_label: str) -> list[dict]:
    """All match dicts whose round == round_label."""
    return [m for m in matches if (m or {}).get("round") == round_label]


def round_fully_seeded(matches: list[dict], round_label: str) -> bool:
    """True iff round_label has its expected fixture count AND every one has
    both team slots assigned. Gates the transition so we never reprice off a
    half-seeded bracket (e.g. the feed populates fixtures one at a time)."""
    fixtures = round_fixtures(matches, round_label)
    expected = KO_FIXTURES_PER_ROUND.get(round_label)
    if expected is not None and len(fixtures) != expected:
        return False
    if not fixtures:
        return False
    return all(m.get("team1Id") and m.get("team2Id") for m in fixtures)


def advancer_slugs_for_round(matches: list[dict], round_label: str) -> set[str]:
    """Team slugs contesting round_label, read from that round's fixtures."""
    slugs: set[str] = set()
    for m in round_fixtures(matches, round_label):
        for k in ("team1Id", "team2Id"):
            if m.get(k):
                slugs.add(m[k])
    return slugs


def team_pending_counts(matches: list[dict]):
    """Returns ({slug: count of non-FINISHED fixtures it's slotted into},
    {slugs with >=1 FINISHED match}). 'Pending' counts the 3rd-place match,
    so a beaten semifinalist stays pending until that game is played."""
    from collections import defaultdict
    pending: dict[str, int] = defaultdict(int)
    played: set[str] = set()
    for m in matches:
        finished = (m or {}).get("status") == "FINISHED"
        for slug in (m.get("team1Id"), m.get("team2Id")):
            if not slug:
                continue
            if finished:
                played.add(slug)
            else:
                pending[slug] += 1
    return pending, played


def eliminated_slugs(matches: list[dict], advancer_ids: set[str],
                     champion_slug: str | None = None) -> set[str]:
    """Teams that are out: they've played, they're not advancing, they have no
    remaining fixture (incl. 3rd-place), and they're not the champion."""
    pending, played = team_pending_counts(matches)
    return {
        slug for slug in played
        if slug not in advancer_ids
        and slug != champion_slug
        and pending.get(slug, 0) == 0
    }


# ---------------------------------------------------------------------------
# Canonical WC 2026 schedule (UTC) - PUBLIC, FIXED, HARDCODED
# ---------------------------------------------------------------------------
#
# These dates are well-known public information; the game's timing logic should
# anchor to them rather than guess or depend on how fresh the feed is. Each
# value is the round's FIRST kickoff; GROUP_STAGE's entry is its LAST kickoff
# (the point after which the R32 bracket gets seeded). Verified against the
# stored fixtures (data/wc2026_matches_cache.json); see schedule_drift() for the
# runtime cross-check that flags any disagreement (a reschedule or feed bug).
ROUND_FIRST_KICKOFF_UTC = {
    "R32": "2026-06-28T19:00:00Z",
    "R16": "2026-07-04T17:00:00Z",
    "QF":  "2026-07-09T20:00:00Z",
    "SF":  "2026-07-14T19:00:00Z",
    "F":   "2026-07-19T19:00:00Z",
}
GROUP_STAGE_LAST_KICKOFF_UTC = "2026-06-28T02:00:00Z"


def parse_iso_utc(s: str):
    """Parse an ISO-8601 string (trailing Z ok) into a tz-aware UTC datetime."""
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def scheduled_kickoff(round_label: str):
    """Known first-kickoff datetime for a round, or None if not a knockout round."""
    return parse_iso_utc(ROUND_FIRST_KICKOFF_UTC.get(round_label))


def schedule_drift(matches: list[dict]) -> list[tuple]:
    """Cross-check the hardcoded schedule against the feed's actual earliest
    kickoff per round. Returns [(round, hardcoded_iso, feed_iso), ...] for any
    round whose feed date differs from the hardcoded one - i.e. a reschedule or
    a corrupt feed we should notice rather than silently follow."""
    drift = []
    for rnd, hard in ROUND_FIRST_KICKOFF_UTC.items():
        feed = [(m.get("kickoff") or m.get("utcDate")) for m in matches
                if (m or {}).get("round") == rnd]
        feed = [x for x in feed if x]
        if not feed:
            continue
        fmin = min(feed)
        if fmin[:10] != hard[:10]:   # compare at day granularity
            drift.append((rnd, hard, fmin))
    return drift


def transition_overdue(next_round: str, bracket_seeded: bool, now,
                       lead_seconds: int = 6 * 3600) -> bool:
    """True if `next_round`'s KNOWN first kickoff is within lead_seconds (or
    already past) and we still can't transition (bracket not seeded). Anchored
    to the public schedule - no guessing about how long seeding 'should' take.
    Use to escalate to the manual procedure before the round actually starts."""
    if bracket_seeded:
        return False
    dt = scheduled_kickoff(next_round)
    if dt is None:
        return False
    return (dt - now).total_seconds() <= lead_seconds
