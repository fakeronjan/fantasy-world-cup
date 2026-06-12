"""Live update pipeline - Deep Data edition.

Runs idempotently on a cron (every 15 min during match windows). Each run:

  1. Loads `config/global` for scoring weights.
  2. Pulls match-list summary from football-data.org `/v4/competitions/WC/matches`.
  3. For each match that's newly FINISHED (or whose lastUpdated has advanced),
     fetches detail via `/v4/matches/{id}` and persists:
       - Score, status, winner
       - Per-goal: scorerFdId, assistFdId, minute, type, team
       - Effective lineup fdIds (starters ∪ subs-in) per team
       - Clean-sheet flags per team
  4. Recomputes team W/D/L + advancement bonus + totalPoints idempotently.
  5. Recomputes player goals + assists + winsPlayedIn + cleanSheetsPlayedIn
     idempotently by scanning all FINISHED matches' persisted data.
  6. Recomputes user totals + writes leaderboard snapshot.

Idempotent: re-running produces the same Firestore state (no double-counting).

Usage (cron uses these env vars):
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
  FOOTBALL_DATA_KEY=...
  ./venv/bin/python scripts/ingest_results.py
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import fd_get, firestore_client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADVANCEMENT_ORDER = ["group", "R32", "R16", "QF", "SF", "F", "W"]

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

# When round X completes, transition to round X+1.
NEXT_ROUND = {
    "group": "R32",
    "R32":   "R16",
    "R16":   "QF",
    "QF":    "SF",
    "SF":    "F",
    "F":     "done",
}

# Transfer window auto-closes this many seconds before next round's first match
WINDOW_CLOSE_LEAD_SECONDS = 3600  # 1 hour

# Calendar dates (pointsByDate buckets, matchday labels) are computed in Hawaii
# time (UTC-10, no DST). Every WC 2026 match is in North America, so no real
# game crosses midnight Hawaii - this keeps a late-night-Eastern game (which is
# early-morning UTC) on its intended matchday instead of rolling to the next day.
HAWAII_TZ = timezone(timedelta(hours=-10))

def hawaii_date(dt: datetime) -> str:
    """YYYY-MM-DD calendar date of a tz-aware datetime, in Hawaii time."""
    return dt.astimezone(HAWAII_TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Step 1: sync match catalog + fetch detail for newly-finished matches
# ---------------------------------------------------------------------------

def _our_round(stage: str) -> str:
    return _STAGE_MAP.get(stage or "", stage or "group")


def _index_teams_by_fdid(db):
    out = {}
    for doc in db.collection("teams").stream():
        d = doc.to_dict() or {}
        if d.get("fdId") is not None:
            out[int(d["fdId"])] = doc.id
    return out


def _index_players_by_fdid(db):
    out = {}
    for doc in db.collection("players").stream():
        d = doc.to_dict() or {}
        if d.get("fdId") is not None:
            out[int(d["fdId"])] = doc.id
    return out


def _effective_lineup(team_data: dict, substitutions: list, team_fd_id: int) -> list[int]:
    """All fdIds who played for this team - starters ∪ subs in."""
    ids = []
    seen = set()
    for p in team_data.get("lineup") or []:
        pid = p.get("id")
        if pid and pid not in seen:
            ids.append(int(pid)); seen.add(pid)
    for sub in substitutions or []:
        if (sub.get("team") or {}).get("id") == team_fd_id:
            pin = sub.get("playerIn") or {}
            pid = pin.get("id")
            if pid and pid not in seen:
                ids.append(int(pid)); seen.add(pid)
    return ids


def _normalize_match_summary(m: dict, team_fd_to_slug: dict) -> dict:
    """Shape returned by competition-list endpoint (no goals/lineups)."""
    score = m.get("score", {}) or {}
    full  = score.get("fullTime", {}) or {}
    stage = m.get("stage") or "GROUP_STAGE"
    h_fd  = (m.get("homeTeam") or {}).get("id")
    a_fd  = (m.get("awayTeam") or {}).get("id")
    return {
        "fdId":          int(m["id"]),
        "round":         _our_round(stage),
        "stage":         stage,
        "group":         m.get("group"),
        "team1Id":       team_fd_to_slug.get(h_fd),
        "team2Id":       team_fd_to_slug.get(a_fd),
        "team1Name":     (m.get("homeTeam") or {}).get("name"),
        "team2Name":     (m.get("awayTeam") or {}).get("name"),
        "team1FdId":     h_fd,
        "team2FdId":     a_fd,
        "score1":        full.get("home"),
        "score2":        full.get("away"),
        "winner":        score.get("winner"),
        "status":        m.get("status"),
        "kickoff":       m.get("utcDate"),
        "lastUpdated":   m.get("lastUpdated"),
    }


def _enrich_with_detail(detail: dict, summary: dict) -> dict:
    """Take a /v4/matches/{id} response and produce the rich Firestore shape."""
    goals_raw = detail.get("goals") or []
    home_team = detail.get("homeTeam") or {}
    away_team = detail.get("awayTeam") or {}
    subs      = detail.get("substitutions") or []

    home_fd = (home_team.get("id"))
    away_fd = (away_team.get("id"))

    goals = []
    for g in goals_raw:
        team_obj = g.get("team") or {}
        side = "home" if team_obj.get("id") == home_fd else "away"
        scorer = g.get("scorer") or {}
        assist = g.get("assist") or {}
        goals.append({
            "minute":      g.get("minute"),
            "injuryTime":  g.get("injuryTime"),
            "type":        g.get("type"),
            "side":        side,
            "scorerFdId":  scorer.get("id"),
            "scorerName":  scorer.get("name"),
            "assistFdId":  assist.get("id") if assist else None,
            "assistName":  assist.get("name") if assist else None,
        })

    bookings = []
    for b in detail.get("bookings") or []:
        player = b.get("player") or {}
        team   = b.get("team") or {}
        bookings.append({
            "minute":     b.get("minute"),
            "playerFdId": player.get("id"),
            "playerName": player.get("name"),
            "teamFdId":   team.get("id"),
            "card":       b.get("card"),
        })

    home_lineup = _effective_lineup(home_team, subs, home_fd)
    away_lineup = _effective_lineup(away_team, subs, away_fd)

    score = detail.get("score", {}) or {}
    full  = score.get("fullTime", {}) or {}
    s1 = full.get("home") if full.get("home") is not None else summary.get("score1")
    s2 = full.get("away") if full.get("away") is not None else summary.get("score2")

    return {
        **summary,
        "goals":             goals,
        "bookings":          bookings,
        "homeLineupFdIds":   home_lineup,
        "awayLineupFdIds":   away_lineup,
        "cleanSheetHome":    (s2 == 0) if s2 is not None else False,
        "cleanSheetAway":    (s1 == 0) if s1 is not None else False,
        "detailIngestedAt":  datetime.now(timezone.utc).isoformat(),
    }


def sync_matches(db, team_fd_to_slug: dict) -> int:
    """Sync match list + fetch details for FINISHED matches whose detail
    we haven't ingested yet (or whose lastUpdated has changed).
    Returns count of detail-fetches performed."""
    payload = fd_get("/competitions/WC/matches")
    matches_raw = payload.get("matches") or []
    print(f"  fetched {len(matches_raw)} matches from football-data.org")

    detail_fetches = 0
    batch = db.batch()
    n = 0

    for m_raw in matches_raw:
        summary = _normalize_match_summary(m_raw, team_fd_to_slug)
        doc_id = str(summary["fdId"])
        ref = db.collection("matches").document(doc_id)

        # Check if we need to fetch the detailed event data
        existing_snap = ref.get()
        existing = existing_snap.to_dict() if existing_snap.exists else {}
        needs_detail = (
            summary["status"] == "FINISHED"
            and (
                "goals" not in existing
                or existing.get("lastUpdated") != summary["lastUpdated"]
            )
        )

        if needs_detail:
            try:
                detail = fd_get(f"/matches/{summary['fdId']}")
                enriched = _enrich_with_detail(detail, summary)
                batch.set(ref, enriched, merge=True)
                detail_fetches += 1
                # Be polite to API - 30/min cap, sleep briefly between calls
                time.sleep(2.5)
            except Exception as e:
                print(f"  ! detail fetch failed for {summary['fdId']}: {e}", file=sys.stderr)
                batch.set(ref, summary, merge=True)
        else:
            batch.set(ref, summary, merge=True)

        n += 1
        if n % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    print(f"  synced {n} matches; fetched detail for {detail_fetches} newly-finished")
    return detail_fetches


# ---------------------------------------------------------------------------
# Step 2: recompute team stats
# ---------------------------------------------------------------------------

def recompute_teams(db, weights: dict) -> dict[str, dict]:
    """Walk all FINISHED matches, recompute W/D/L/GF/GA/CS per team.
    Determine deepest round reached. Compute totalPoints.
    Returns {team_doc_id: stats} for downstream player computation."""
    matches = []
    for mdoc in db.collection("matches").stream():
        m = mdoc.to_dict() or {}
        if m.get("status") == "FINISHED" and m.get("score1") is not None:
            matches.append(m)

    stats: dict[str, dict] = {}
    def _row(slug):
        if slug not in stats:
            stats[slug] = dict(
                matchesWon=0, matchesDrawn=0, matchesLost=0,
                goalsFor=0, goalsAgainst=0, cleanSheets=0,
                finalRound="group",
            )
        return stats[slug]

    for m in matches:
        t1, t2 = m.get("team1Id"), m.get("team2Id")
        if not (t1 and t2): continue
        s1, s2 = m["score1"], m["score2"]
        r1 = _row(t1); r2 = _row(t2)
        r1["goalsFor"] += s1; r1["goalsAgainst"] += s2
        r2["goalsFor"] += s2; r2["goalsAgainst"] += s1
        w = m.get("winner")
        if w == "HOME_TEAM":
            r1["matchesWon"] += 1; r2["matchesLost"] += 1
        elif w == "AWAY_TEAM":
            r2["matchesWon"] += 1; r1["matchesLost"] += 1
        else:
            r1["matchesDrawn"] += 1; r2["matchesDrawn"] += 1
        if s2 == 0: r1["cleanSheets"] += 1
        if s1 == 0: r2["cleanSheets"] += 1
        # Track deepest round
        cur_r = m.get("round", "group")
        for slug in (t1, t2):
            r = "SF" if cur_r == "third" else cur_r
            if r in ADVANCEMENT_ORDER:
                if ADVANCEMENT_ORDER.index(r) > ADVANCEMENT_ORDER.index(stats[slug]["finalRound"]):
                    stats[slug]["finalRound"] = r
        if cur_r == "F" and w in ("HOME_TEAM", "AWAY_TEAM"):
            champ = t1 if w == "HOME_TEAM" else t2
            stats[champ]["finalRound"] = "W"

    # Compute totalPoints + eliminated, write back
    BONUSES = {
        "R32": weights["bonus_r32"], "R16": weights["bonus_r16"],
        "QF":  weights["bonus_qf"],  "SF":  weights["bonus_sf"],
        "F":   weights["bonus_final"], "W":  weights["bonus_champion"],
    }
    batch = db.batch()
    n = 0
    fd_to_slug = _index_teams_by_fdid(db)
    matches_by_team_pending = defaultdict(int)  # team_slug → count of non-FINISHED matches
    for m in db.collection("matches").stream():
        md = m.to_dict() or {}
        if md.get("status") != "FINISHED":
            for slug in (md.get("team1Id"), md.get("team2Id")):
                if slug:
                    matches_by_team_pending[slug] += 1

    for slug, row in stats.items():
        pts = weights["team_win"] * row["matchesWon"] + weights["team_draw"] * row["matchesDrawn"]
        fr = row["finalRound"]
        idx = ADVANCEMENT_ORDER.index(fr) if fr in ADVANCEMENT_ORDER else 0
        for r in ADVANCEMENT_ORDER[1:idx + 1]:
            pts += BONUSES.get(r, 0)
        row["totalPoints"] = int(pts)
        row["eliminated"] = (matches_by_team_pending.get(slug, 0) == 0) and fr != "W" and fr != "group"
        ref = db.collection("teams").document(slug)
        batch.set(ref, row, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  updated {n} teams")
    return stats


# ---------------------------------------------------------------------------
# Step 3: recompute player stats from per-match data (idempotent)
# ---------------------------------------------------------------------------

def recompute_players(db, team_stats: dict, weights: dict) -> None:
    """For every player, recompute totals from match data:
       goals, assists, winsPlayedIn, cleanSheetsPlayedIn."""
    players_by_fd = _index_players_by_fdid(db)
    teams_fd_to_slug = _index_teams_by_fdid(db)

    # Pre-load all FINISHED match docs into memory
    finished_matches = []
    for mdoc in db.collection("matches").stream():
        m = mdoc.to_dict() or {}
        if m.get("status") == "FINISHED":
            finished_matches.append(m)

    # Per-player accumulators
    counts = defaultdict(lambda: {"goals": 0, "assists": 0, "wins_played": 0, "cs_played": 0})

    for m in finished_matches:
        winner_slug = (
            m.get("team1Id") if m.get("winner") == "HOME_TEAM"
            else m.get("team2Id") if m.get("winner") == "AWAY_TEAM"
            else None
        )
        cs_home = m.get("cleanSheetHome", False)
        cs_away = m.get("cleanSheetAway", False)
        team1_slug = m.get("team1Id")
        team2_slug = m.get("team2Id")

        # Goals + assists
        for g in m.get("goals", []):
            scorer_fd = g.get("scorerFdId")
            if scorer_fd and scorer_fd in players_by_fd:
                counts[players_by_fd[scorer_fd]]["goals"] += 1
            assist_fd = g.get("assistFdId")
            if assist_fd and assist_fd in players_by_fd:
                counts[players_by_fd[assist_fd]]["assists"] += 1

        # Lineup-based win share + CS
        for fd in m.get("homeLineupFdIds", []):
            if fd in players_by_fd:
                pid = players_by_fd[fd]
                if winner_slug == team1_slug: counts[pid]["wins_played"] += 1
                if cs_home: counts[pid]["cs_played"] += 1
        for fd in m.get("awayLineupFdIds", []):
            if fd in players_by_fd:
                pid = players_by_fd[fd]
                if winner_slug == team2_slug: counts[pid]["wins_played"] += 1
                if cs_away: counts[pid]["cs_played"] += 1

    # Write player updates
    batch = db.batch()
    n = 0
    for pdoc in db.collection("players").stream():
        p = pdoc.to_dict() or {}
        s = counts.get(pdoc.id, {})
        goals       = s.get("goals", 0)
        assists     = s.get("assists", 0)
        wins_played = s.get("wins_played", 0)
        cs_played   = s.get("cs_played", 0)
        pos = (p.get("position") or "").upper()
        if   pos == "GK":  cs_rate = weights["player_clean_sheet_gk"]
        elif pos == "DEF": cs_rate = weights.get("player_clean_sheet_def", 2)
        else:                cs_rate = weights.get("player_clean_sheet_other", 0)
        total_pts = (
            goals * weights["player_goal"]
            + assists * weights["player_assist"]
            + wins_played * weights["player_win_share"]
            + cs_played * cs_rate
        )
        team_stats_row = team_stats.get(p.get("teamId"), {})
        batch.set(pdoc.reference, {
            "goals":              goals,
            "assists":            assists,
            "winsPlayedIn":       wins_played,
            "cleanSheetsPlayedIn": cs_played,
            "totalPoints":        int(total_pts),
            "eliminated":         team_stats_row.get("eliminated", False),
        }, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  updated {n} players")


# ---------------------------------------------------------------------------
# Step 4: recompute user totals + leaderboard snapshot
# ---------------------------------------------------------------------------

def recompute_users(db) -> list[dict]:
    teams_cache   = {d.id: d.to_dict() or {} for d in db.collection("teams").stream()}
    players_cache = {d.id: d.to_dict() or {} for d in db.collection("players").stream()}

    leaderboard: list[dict] = []
    batch = db.batch()
    n = 0
    for udoc in db.collection("users").stream():
        u = udoc.to_dict() or {}
        roster = u.get("roster") or []
        # Forward-only scoring. Start from points banked when picks were sold
        # (transfers + auto-sell), then add each HELD pick's points earned SINCE
        # purchase = asset.totalPoints now minus its totalPoints when bought
        # (pointsAtPurchase). Draft picks were bought pre-tournament so their
        # snapshot is 0 -> they score the asset's full total, unchanged.
        total = int(u.get("bankedPoints", 0) or 0)
        tiebreaker = int(u.get("bankedTiebreaker", 0) or 0)
        for pick in roster:
            kind = pick.get("kind")
            ast_id = pick.get("assetId")
            ast = (teams_cache if kind == "team" else players_cache).get(ast_id) or {}
            asset_pts = int(ast.get("totalPoints", 0))
            # Tiebreaker contribution: teams -> goalsFor; players -> goals+assists
            asset_tb = (int(ast.get("goalsFor", 0)) if kind == "team"
                        else int(ast.get("goals", 0)) + int(ast.get("assists", 0)))
            fwd_pts = max(0, asset_pts - int(pick.get("pointsAtPurchase", 0) or 0))
            fwd_tb  = max(0, asset_tb  - int(pick.get("tbAtPurchase", 0) or 0))
            total += fwd_pts
            tiebreaker += fwd_tb
            pick["points"] = fwd_pts          # points THIS holder earned from the pick
            pick["eliminated"] = bool(ast.get("eliminated", False))
        # Snapshot today's cumulative total into pointsByDate so the
        # leaderboard chart can draw a real trajectory over time. Keyed by
        # Hawaii date so late-night-Eastern games stay on their matchday.
        today = hawaii_date(datetime.now(timezone.utc))
        points_by_date = dict(u.get("pointsByDate") or {})
        points_by_date[today] = int(total)
        batch.set(
            udoc.reference,
            {
                "roster": roster,
                "totalPoints": total,
                "tieBreakerScore": tiebreaker,
                "pointsByDate": points_by_date,
            },
            merge=True,
        )
        leaderboard.append({
            "uid":         udoc.id,
            "displayName": u.get("displayName") or u.get("email"),
            "totalPoints": total,
            "picks":       len(roster),
        })
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  updated {n} users")
    return leaderboard


def maybe_transition_round(db, cfg: dict) -> str | None:
    """Detect round completion + transition to next round.

    A round is complete when all matches with that round label are
    status=FINISHED. On transition:
      1. Set config.currentRound to next round
      2. Open transfer window
      3. Run reprice (forward-looking pricing + auto-sell + value snapshots)

    Idempotent - once currentRound has advanced, the new round's matches
    are checked instead of the old, so we don't re-fire.
    Returns the new round label if a transition happened, else None."""
    current = (cfg.get("currentRound") or "pre")
    if current == "done":
        return None

    # Special case: pre → group. Triggered ONLY when a match is actually
    # IN_PLAY / PAUSED / FINISHED - i.e. the tournament has really started.
    # The earlier check (status != SCHEDULED) falsely fired on TIMED
    # transitions weeks before kickoff. Doesn't open a transfer window  - 
    # that opens after group stage completes.
    if current == "pre":
        REAL_STATUSES = {"IN_PLAY", "PAUSED", "FINISHED"}
        for mdoc in db.collection("matches").stream():
            m = mdoc.to_dict() or {}
            if m.get("status") in REAL_STATUSES:
                db.collection("config").document("global").set({
                    "currentRound": "group",
                }, merge=True)
                print(f"  Kickoff detected - pre → group (no transfer window)")
                return "group"
        return None

    # Collect all matches for the current round
    round_matches = []
    for mdoc in db.collection("matches").stream():
        m = mdoc.to_dict() or {}
        if m.get("round") == current:
            round_matches.append(m)

    if not round_matches:
        return None
    if not all(m.get("status") == "FINISHED" for m in round_matches):
        return None  # round not done yet

    nxt = NEXT_ROUND.get(current)
    if not nxt:
        return None

    if nxt == "done":
        db.collection("config").document("global").set({
            "currentRound":        "done",
            "transferWindowOpen":  False,
        }, merge=True)
        print(f"  Tournament complete - currentRound=done, window closed")
        return nxt

    print(f"  Round transition detected: {current} → {nxt}")
    db.collection("config").document("global").set({
        "currentRound":       nxt,
        "transferWindowOpen": True,
    }, merge=True)
    print(f"  Set currentRound={nxt}, transferWindowOpen=True")

    # Trigger reprice for the new round
    try:
        from collections import defaultdict
        from reprice import (
            live_advancers, reprice as compute_reprice,
            write_prices_to_firestore, auto_sell_eliminated_picks,
            snapshot_user_values,
        )
        from simulate_2026 import load_seed
        teams, players = load_seed()
        players_by_team = defaultdict(list)
        for p in players:
            players_by_team[p["teamId"]].append(p)

        advancers = live_advancers(db, nxt)
        if not advancers:
            print(f"  WARN: no advancers found for {nxt}. Did ingest mark finalRound correctly?")
            return nxt

        print(f"  Repricing for {nxt} ({len(advancers)} advancers, 1000 sims)...")
        prices = compute_reprice(advancers, nxt, players_by_team, runs=1000, seed=42)
        write_prices_to_firestore(db, prices, advancers, teams, nxt)
        auto_sell_eliminated_picks(db)
        snapshot_user_values(db, nxt)
        print(f"  Reprice + auto-sell + snapshot complete for {nxt}")
    except Exception as e:
        print(f"  ERROR during reprice trigger: {e}")
        # Don't crash the whole ingest - window is open, admin can re-run
        # reprice manually if needed.
    return nxt


def maybe_close_window(db, cfg: dict) -> bool:
    """Close the transfer window when next round's first match is imminent.

    Idempotent: no-op if window is already closed or no match is within
    WINDOW_CLOSE_LEAD_SECONDS. Returns True if we closed it this call."""
    if not cfg.get("transferWindowOpen"):
        return False
    current = (cfg.get("currentRound") or "pre")
    if current in ("pre", "done"):
        return False

    # Earliest unplayed match for the CURRENT round (the round about to start)
    earliest_iso = None
    for mdoc in db.collection("matches").stream():
        m = mdoc.to_dict() or {}
        if m.get("round") != current:
            continue
        if m.get("status") == "FINISHED":
            continue
        utc = m.get("utcDate")
        if utc and (earliest_iso is None or utc < earliest_iso):
            earliest_iso = utc

    if not earliest_iso:
        return False

    now = datetime.now(timezone.utc)
    try:
        earliest_dt = datetime.fromisoformat(earliest_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    delta = (earliest_dt - now).total_seconds()
    if delta > WINDOW_CLOSE_LEAD_SECONDS:
        return False

    db.collection("config").document("global").set({
        "transferWindowOpen": False,
    }, merge=True)
    print(f"  Transfer window closed - {current} first match starts in {int(delta/60)}min")
    return True


def write_leaderboard_snapshot(db, entries: list[dict]) -> None:
    entries.sort(key=lambda e: e["totalPoints"], reverse=True)
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    db.collection("leaderboard").document("snapshot").set({
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "entries":   entries,
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
        sys.exit("config/global is missing - run scripts/seed_assets.py first")
    cfg = cfg_doc.to_dict()
    weights = cfg["scoringWeights"]

    print("Step 1: sync match catalog + fetch details for finished matches…")
    team_fd_to_slug = _index_teams_by_fdid(db)
    sync_matches(db, team_fd_to_slug)

    print("Step 2: recompute team records + points…")
    team_stats = recompute_teams(db, weights)

    print("Step 3: recompute player goals/assists/wins/CS from match data…")
    recompute_players(db, team_stats, weights)

    print("Step 4: recompute user totals + leaderboard snapshot…")
    leaderboard = recompute_users(db)
    write_leaderboard_snapshot(db, leaderboard)

    # Reload config - Steps 1-4 may have changed it (e.g., kickoff detection).
    cfg = db.collection("config").document("global").get().to_dict() or {}

    print("Step 5: check for round transition…")
    transitioned = maybe_transition_round(db, cfg)
    if transitioned:
        # Re-fetch config since the transition just changed it
        cfg = db.collection("config").document("global").get().to_dict() or {}

    print("Step 6: check if transfer window should auto-close…")
    maybe_close_window(db, cfg)

    print("\nDone. Live state refreshed.")


if __name__ == "__main__":
    main()
