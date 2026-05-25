"""Live update pipeline: pull match results, recompute scoring, refresh leaderboard.

Runs idempotently — designed for a cron schedule (every 15 min during match
windows). Each invocation:

  1. Loads `config/global` (scoring weights, current round).
  2. Fetches all WC matches from football-data.org and refreshes the
     matches collection (score, status, etc.).
  3. For each team, recomputes W/D/L/GF/GA from its finished matches,
     then recomputes totalPoints using the locked scoring weights.
  4. Recomputes each player's totalPoints (goals * weight + win share +
     CS bonus). NOTE: player goal counts come from admin entry or the
     v1.1 Wikipedia scraper — this script only RECOMPUTES totals from
     whatever counts are already in the player docs.
  5. For each user, recomputes totalPoints by summing their roster.
  6. Writes a denormalized leaderboard/snapshot doc.

Usage (same auth setup as seed_assets.py):
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json \
    ./venv/bin/python scripts/ingest_results.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import fd_get, firestore_client, normalize_match


# ---------------------------------------------------------------------------
# Step 1: pull match catalog updates
# ---------------------------------------------------------------------------

def sync_matches(db) -> list[dict]:
    """Pull match catalog, write any updates, and return the normalized list."""
    payload = fd_get("/competitions/WC/matches")
    matches = payload.get("matches") or []
    print(f"  fetched {len(matches)} matches from football-data.org")

    normalized = [normalize_match(m) for m in matches]
    batch = db.batch()
    n = 0
    for nm in normalized:
        ref = db.collection("matches").document(str(nm["fdId"]))
        batch.set(ref, nm, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    return normalized


# ---------------------------------------------------------------------------
# Step 2: recompute team records + points
# ---------------------------------------------------------------------------

# Map football-data team id -> our team doc id slug.
# Built once from teams collection metadata.
def _team_id_index(db) -> dict[int, str]:
    out = {}
    for doc in db.collection("teams").stream():
        d = doc.to_dict() or {}
        if d.get("fdId") is not None:
            out[int(d["fdId"])] = doc.id
    return out


def recompute_teams(db, matches: list[dict], weights: dict) -> dict[str, dict]:
    """For each team, walk its FINISHED matches and recompute W/D/L/GF/GA/CS
    and totalPoints. Returns {team_doc_id: stats dict} for downstream use."""
    fd_to_slug = _team_id_index(db)

    # Aggregate stats per team
    stats: dict[str, dict] = {}
    def _row(slug):
        if slug not in stats:
            stats[slug] = dict(
                matchesWon=0, matchesDrawn=0, matchesLost=0,
                goalsFor=0, goalsAgainst=0, cleanSheets=0,
                finalRound="group",
            )
        return stats[slug]

    round_order = ["group", "R32", "R16", "QF", "SF", "third", "F", "W"]

    for m in matches:
        if m["status"] != "FINISHED":
            continue
        if m["score1"] is None or m["score2"] is None:
            continue
        t1 = fd_to_slug.get(m["team1Id"])
        t2 = fd_to_slug.get(m["team2Id"])
        if not (t1 and t2):
            continue
        r1 = _row(t1); r2 = _row(t2)
        r1["goalsFor"] += m["score1"]; r1["goalsAgainst"] += m["score2"]
        r2["goalsFor"] += m["score2"]; r2["goalsAgainst"] += m["score1"]

        winner = m["winner"]
        if winner == "HOME_TEAM":
            r1["matchesWon"]   += 1; r2["matchesLost"]  += 1
        elif winner == "AWAY_TEAM":
            r2["matchesWon"]   += 1; r1["matchesLost"]  += 1
        else:  # DRAW or PK shootout (FIFA convention: counts as draw)
            r1["matchesDrawn"] += 1; r2["matchesDrawn"] += 1

        if m["score2"] == 0: r1["cleanSheets"] += 1
        if m["score1"] == 0: r2["cleanSheets"] += 1

        # Track deepest round reached for advancement bonus calculation.
        cur_r = m["round"]
        # The team that "won" the match reached the round AT LEAST. If they
        # advanced further, a later match will update them. For 3rd-place
        # match, the WINNER reached SF and lost; bonus is SF.
        for slug in (t1, t2):
            row = stats[slug]
            # Special case for 3rd-place match — both teams reached SF.
            r = "SF" if cur_r == "third" else cur_r
            if round_order.index(r) > round_order.index(row["finalRound"]):
                row["finalRound"] = r
        # If this was the FINAL, the WINNER moves to "W".
        if cur_r == "F" and winner in ("HOME_TEAM", "AWAY_TEAM"):
            champ = t1 if winner == "HOME_TEAM" else t2
            stats[champ]["finalRound"] = "W"

    # Compute points + write back to Firestore.
    batch = db.batch()
    n = 0
    for slug, row in stats.items():
        pts = compute_team_points(row, weights)
        row["totalPoints"] = pts
        row["eliminated"] = (
            row["finalRound"] not in ("group",)
            and not _team_still_alive(slug, matches, fd_to_slug)
        )
        ref = db.collection("teams").document(slug)
        batch.set(ref, row, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"  updated {n} teams with current records + points")
    return stats


def _team_still_alive(slug: str, matches: list[dict], fd_to_slug: dict[int, str]) -> bool:
    """A team is alive if any non-FINISHED match has them in it."""
    # Find their fdId
    fd_id = None
    for fid, s in fd_to_slug.items():
        if s == slug: fd_id = fid; break
    if fd_id is None: return True
    for m in matches:
        if m["status"] == "FINISHED": continue
        if m["team1Id"] == fd_id or m["team2Id"] == fd_id:
            return True
    return False


def compute_team_points(row: dict, w: dict) -> int:
    pts  = w["team_win"]  * row["matchesWon"]
    pts += w["team_draw"] * row["matchesDrawn"]
    bonuses = {
        "R32": w["bonus_r32"], "R16": w["bonus_r16"], "QF": w["bonus_qf"],
        "SF": w["bonus_sf"], "F": w["bonus_final"], "W": w["bonus_champion"],
    }
    order = ["group", "R32", "R16", "QF", "SF", "F", "W"]
    idx = order.index(row["finalRound"]) if row["finalRound"] in order else 0
    for r in order[1:idx + 1]:
        pts += bonuses.get(r, 0)
    return int(pts)


# ---------------------------------------------------------------------------
# Step 3: recompute player points
# ---------------------------------------------------------------------------

def recompute_players(db, team_stats: dict[str, dict], weights: dict) -> None:
    """Recompute every player's totalPoints from their existing goal count
    (admin-entered or scraped) + win share + CS bonus from their team."""
    batch = db.batch()
    n = 0
    for pdoc in db.collection("players").stream():
        p = pdoc.to_dict() or {}
        team_slug = p.get("teamId")
        stats = team_stats.get(team_slug) or {}
        wins = stats.get("matchesWon", 0)
        cs = stats.get("cleanSheets", 0)
        goals = p.get("goals", 0) or 0
        is_gk = (p.get("position") or "").lower().startswith("goalkeep")
        cs_rate = weights["player_clean_sheet_gk"] if is_gk else weights["player_clean_sheet_other"]
        pts = (
            weights["player_goal"] * goals
            + weights["player_win_share"] * wins
            + cs_rate * cs
        )
        eliminated = (stats.get("finalRound") in ("group",)) and not _team_still_alive(team_slug, [], {})
        # We can't easily compute alive here without re-fetching matches; the
        # team record's eliminated flag is the source of truth.
        batch.set(pdoc.reference, {
            "totalPoints": int(pts),
            "eliminated": stats.get("eliminated", False),
        }, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"  updated {n} players with current points")


# ---------------------------------------------------------------------------
# Step 4: recompute user totals + leaderboard snapshot
# ---------------------------------------------------------------------------

def recompute_users(db) -> list[dict]:
    """Read each user's roster, sum the points from the catalog's current
    state, write back totalPoints to the user doc."""
    # Cache catalog so we don't re-read for each user.
    teams_cache = {d.id: d.to_dict() or {} for d in db.collection("teams").stream()}
    players_cache = {d.id: d.to_dict() or {} for d in db.collection("players").stream()}

    leaderboard: list[dict] = []
    batch = db.batch()
    n = 0
    for udoc in db.collection("users").stream():
        u = udoc.to_dict() or {}
        roster = u.get("roster") or []
        total = 0
        for pick in roster:
            kind = pick.get("kind")
            asset_id = pick.get("assetId")
            if kind == "team":
                ast = teams_cache.get(asset_id) or {}
            else:
                ast = players_cache.get(asset_id) or {}
            total += int(ast.get("totalPoints", 0))
            # Stamp current points on the pick for the roster UI to render.
            pick["points"] = int(ast.get("totalPoints", 0))
            pick["eliminated"] = bool(ast.get("eliminated", False))
        batch.set(udoc.reference, {"roster": roster, "totalPoints": total}, merge=True)
        leaderboard.append({
            "uid": udoc.id,
            "displayName": u.get("displayName") or u.get("email"),
            "totalPoints": total,
            "picks": len(roster),
        })
        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"  updated {n} users")
    return leaderboard


def write_leaderboard_snapshot(db, entries: list[dict]) -> None:
    entries.sort(key=lambda e: e["totalPoints"], reverse=True)
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    db.collection("leaderboard").document("snapshot").set({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    })
    print(f"  leaderboard snapshot written ({len(entries)} entries)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    db = firestore_client()

    print("Loading config/global…")
    cfg_doc = db.collection("config").document("global").get()
    if not cfg_doc.exists:
        sys.exit("config/global is missing — run scripts/seed_assets.py first")
    cfg = cfg_doc.to_dict()
    weights = cfg["scoringWeights"]

    print("Step 1: sync matches from football-data.org…")
    matches = sync_matches(db)

    print("Step 2: recompute team records + points…")
    team_stats = recompute_teams(db, matches, weights)

    print("Step 3: recompute player points (from existing goal counts)…")
    recompute_players(db, team_stats, weights)

    print("Step 4: recompute user totals + leaderboard snapshot…")
    leaderboard = recompute_users(db)
    write_leaderboard_snapshot(db, leaderboard)

    print("\nDone. Live state refreshed.")
    print("NOTE: player goal counts are NOT automatically scraped yet. Use")
    print("admin.html (coming in v1.1) to enter goalscorers per match, or")
    print("write directly to players/{id}.goals in Firestore.")


if __name__ == "__main__":
    main()
