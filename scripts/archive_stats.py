"""One-time end-of-tournament archive: freezes the final Firestore state into
static JSON under docs/archive/2026/, so the season's results survive
independent of Firestore (which could get wiped/reused for a future
tournament). Called once by ingest_results.py the moment currentRound flips
to "done" - see that file's `if nxt == "done":` branch.

Modeled on the read-only field-picking style of _dump_state.py, but writes
into the repo (for commit + Pages hosting) instead of /tmp, and never emits
raw email/uid - only the same display-name fallback docs/shared.js's
nameFor() already shows publicly on the live leaderboard.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client

ARCHIVE_DIR = Path("docs/archive/2026")


def _pick(d: dict, keys: list[str]) -> dict:
    return {k: d.get(k) for k in keys}


def _display_name(u: dict) -> str:
    """Mirrors docs/shared.js:232 nameFor() - nickname -> displayName ->
    email-local-part -> masked uid. Never emits the raw email or full uid."""
    nick = (u.get("leagueNickname") or "").strip()
    if nick:
        return nick
    dn = (u.get("displayName") or "").strip()
    if dn:
        return dn
    email = (u.get("email") or "").strip()
    if email:
        return email.split("@")[0]
    return f"Player {(u.get('uid') or '')[:6]}"


def build_archive(db) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    cfg = (db.collection("config").document("global").get().to_dict() or {})

    teams_by_id = {}
    for t in db.collection("teams").stream():
        td = t.to_dict() or {}
        row = _pick(td, ["name", "totalPoints", "matchesWon", "matchesDrawn",
                         "finalRound", "eliminated"])
        row["id"] = t.id
        teams_by_id[t.id] = row

    players_by_id = {}
    for p in db.collection("players").stream():
        pd = p.to_dict() or {}
        row = _pick(pd, ["name", "teamId", "position", "totalPoints", "goals",
                         "assists", "winsPlayedIn", "cleanSheetsPlayedIn"])
        row["id"] = p.id
        players_by_id[p.id] = row

    matches = []
    for m in db.collection("matches").stream():
        md = m.to_dict() or {}
        if md.get("status") != "FINISHED":
            continue
        matches.append(_pick(md, ["round", "team1Id", "team2Id", "score1",
                                   "score2", "kickoff", "winner"]))

    users = []
    for u in db.collection("users").stream():
        ud = u.to_dict() or {}
        roster = []
        for pk in (ud.get("roster") or []):
            roster.append(_pick(pk, ["kind", "assetId", "points",
                                      "pointsAtPurchase", "currentlyHeld"]))
        exited = []
        for e in (ud.get("exitedPicks") or []):
            exited.append(_pick(e, ["kind", "assetId", "points", "exitReason"]))
        users.append({
            "name":            _display_name(ud),
            "countryFlag":     ud.get("countryFlag") or "",
            "totalPoints":     ud.get("totalPoints") or 0,
            "tieBreakerScore": ud.get("tieBreakerScore") or 0,
            "groupIds":        ud.get("groupIds") or ([ud["groupId"]] if ud.get("groupId") else []),
            "roster":          roster,
            "exitedPicks":     exited,
        })
    users.sort(key=lambda u: (-u["totalPoints"], -u["tieBreakerScore"]))

    champion = next((t for t in teams_by_id.values() if t.get("finalRound") == "W"), None)

    (ARCHIVE_DIR / "meta.json").write_text(json.dumps({
        "seasonLabel":      "2026",
        "generatedAt":      datetime.now(timezone.utc).isoformat(),
        "championTeamId":   champion["id"] if champion else None,
        "championTeamName": champion["name"] if champion else None,
        "scoringWeights":   cfg.get("scoringWeights") or {},
    }, indent=2))
    (ARCHIVE_DIR / "leaderboard.json").write_text(json.dumps(users, indent=2))
    (ARCHIVE_DIR / "teams.json").write_text(json.dumps(list(teams_by_id.values()), indent=2))
    (ARCHIVE_DIR / "players.json").write_text(json.dumps(list(players_by_id.values()), indent=2))
    (ARCHIVE_DIR / "matches.json").write_text(json.dumps(matches, indent=2))


if __name__ == "__main__":
    build_archive(firestore_client())
    print(f"Archive written to {ARCHIVE_DIR}/")
