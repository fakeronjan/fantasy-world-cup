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
from firebase_admin import firestore


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

def matchday(kickoff_iso: str) -> str | None:
    """Hawaii calendar date of a match from its ISO kickoff string."""
    if not kickoff_iso:
        return None
    try:
        return hawaii_date(datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")))
    except (ValueError, AttributeError):
        return None


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

def recompute_teams(db, weights: dict, current_round: str | None = None) -> tuple[dict[str, dict], str | None]:
    """Walk all FINISHED matches, recompute W/D/L/GF/GA/CS per team.
    Determine deepest round reached. Compute totalPoints.
    Returns ({team_doc_id: stats}, latest_matchday) - the latter is the most
    recent Hawaii matchday with results, used to bucket pointsByDate."""
    matches = []
    latest_matchday = None
    for mdoc in db.collection("matches").stream():
        m = mdoc.to_dict() or {}
        if m.get("status") == "FINISHED" and m.get("score1") is not None:
            matches.append(m)
            md = matchday(m.get("kickoff"))
            if md and (latest_matchday is None or md > latest_matchday):
                latest_matchday = md

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

    # A team is eliminated once the tournament has left the group stage AND it
    # has no remaining fixture (incl. the 3rd-place match) AND it isn't champion.
    # Gating on `group_over` (rather than the old `fr != "group"` check) is what
    # lets group-stage losers - whose finalRound stays "group" forever - finally
    # get flagged, while never false-flagging a team mid-group-stage. Advancers
    # are naturally excluded: once the bracket is seeded they hold a pending
    # next-round fixture.
    group_over = current_round not in (None, "pre", "group")
    for slug, row in stats.items():
        pts = weights["team_win"] * row["matchesWon"] + weights["team_draw"] * row["matchesDrawn"]
        fr = row["finalRound"]
        idx = ADVANCEMENT_ORDER.index(fr) if fr in ADVANCEMENT_ORDER else 0
        for r in ADVANCEMENT_ORDER[1:idx + 1]:
            pts += BONUSES.get(r, 0)
        row["totalPoints"] = int(pts)
        row["eliminated"] = group_over and (matches_by_team_pending.get(slug, 0) == 0) and fr != "W"
        ref = db.collection("teams").document(slug)
        batch.set(ref, row, merge=True)
        n += 1
        if n % 400 == 0:
            batch.commit(); batch = db.batch()
    batch.commit()
    print(f"  updated {n} teams")
    return stats, latest_matchday


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

def recompute_users(db, latest_matchday: str | None = None) -> list[dict]:
    teams_cache   = {d.id: d.to_dict() or {} for d in db.collection("teams").stream()}
    players_cache = {d.id: d.to_dict() or {} for d in db.collection("players").stream()}
    # Bucket today's cumulative total under the most recent matchday that has
    # results, NOT wall-clock now. A late game that first registers as finished
    # after Hawaii midnight (GitHub cron lag) otherwise collapses into the next
    # calendar day. Falls back to "now" only before any match has finished.
    snap_date = latest_matchday or hawaii_date(datetime.now(timezone.utc))

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
        # leaderboard chart can draw a real trajectory over time. Keyed by the
        # latest matchday with results (snap_date), not wall-clock now, so a
        # late game ingested after Hawaii midnight lands on its own matchday.
        points_by_date = dict(u.get("pointsByDate") or {})
        # Drop any stale snapshot dated after the latest real matchday - these
        # are leftovers from the old wall-clock bucketing (e.g. a 6/16 result
        # ingested 6/17 that wrote a phantom 6/17 entry).
        stale = [k for k in points_by_date if k > snap_date]
        points_by_date[snap_date] = int(total)
        for k in stale:
            del points_by_date[k]
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
        # merge=True deep-merges maps, so dropped keys must be deleted explicitly.
        for k in stale:
            batch.update(udoc.reference, {f"pointsByDate.{k}": firestore.DELETE_FIELD})
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

    # Load all matches once - used for round-completion, bracket-seeding, and
    # elimination checks below.
    all_matches = [mdoc.to_dict() or {} for mdoc in db.collection("matches").stream()]
    round_matches = [m for m in all_matches if m.get("round") == current]

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

    # The current round is over, but we can only reprice once the NEXT round's
    # bracket is fully seeded (every fixture has both teams). The upstream feed
    # fills these slots only after the bracket is officially decided, which can
    # lag the final group-stage whistle by hours. Until then, HOLD: leave
    # currentRound and the (still-closed) transfer window untouched and re-check
    # next cron. This is the fix for the old failure mode where we flipped the
    # round + opened the window against an empty bracket - which made
    # live_advancers come back empty and silently skipped elimination +
    # repricing, leaving dead teams sellable at full price.
    from _fwc_lib import (
        round_fully_seeded, eliminated_slugs as _elim_slugs,
        transition_overdue, ROUND_FIRST_KICKOFF_UTC,
    )
    if not round_fully_seeded(all_matches, nxt):
        now = datetime.now(timezone.utc)
        if transition_overdue(nxt, bracket_seeded=False, now=now):
            # Anchored to the KNOWN public kickoff, not a guessed grace period:
            # the round is about to start and we still can't auto-transition.
            print(f"  ⚠️  {current} complete but {nxt} bracket STILL not seeded - "
                  f"{nxt} kicks off {ROUND_FIRST_KICKOFF_UTC.get(nxt)}. "
                  f"Run docs/MANUAL_KNOCKOUT_TRANSITION.md NOW.")
        else:
            print(f"  {current} complete, but {nxt} bracket not fully seeded yet - holding (will retry)")
        return None

    # Reprice FIRST and only flip the round + open the window if it all
    # succeeds, so the game is never left half-transitioned (window open with
    # stale prices). On any failure we return without flipping, and the next
    # cron retries from the same state.
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
            print(f"  WARN: {nxt} bracket seeded but no team docs matched its slugs - holding")
            return None
        advancer_ids = {t["id"] for t in advancers}
        champ = next((t["id"] for t in advancers if t.get("finalRound") == "W"), None)
        elim = _elim_slugs(all_matches, advancer_ids, champ)

        # Snapshot the pre-transition state FIRST, so revert_transition.py can
        # cleanly roll the turnover back if it's borked.
        snapshot_pre_transition(db, nxt)

        print(f"  Round transition {current} → {nxt}: {len(advancers)} advancers, "
              f"{len(elim)} eliminated, repricing (1000 sims)...")
        prices = compute_reprice(advancers, nxt, players_by_team, runs=1000, seed=42)
        write_prices_to_firestore(db, prices, advancers, teams, nxt, elim)
        auto_sell_eliminated_picks(db)
        snapshot_user_values(db, nxt)
        print(f"  Reprice + auto-sell + snapshot complete for {nxt}")
    except Exception as e:
        print(f"  ERROR during reprice; NOT transitioning (next cron will retry): {e}")
        return None

    # Pricing + eliminations are persisted. Advance the round and enter the
    # SETTLE LOCK: window stays CLOSED for SETTLE_LOCK_SECONDS (a guaranteed
    # no-trades window for a clean revert), and transitionState drives the
    # site-wide "in transition" banner. maybe_open_window() opens trading once
    # the settle lock elapses, guaranteeing the minimum open-trading period.
    from _fwc_lib import SETTLE_LOCK_SECONDS
    now = datetime.now(timezone.utc)
    settle_until = now + timedelta(seconds=SETTLE_LOCK_SECONDS)
    db.collection("config").document("global").set({
        "currentRound":        nxt,
        "transferWindowOpen":  False,
        "transitionState":     True,
        "transitionRound":     nxt,
        "transitionStartedAt": now.isoformat(),
        "settleUntil":         settle_until.isoformat(),
        "windowClosesAt":      None,   # set when the window actually opens
    }, merge=True)
    print(f"  Set currentRound={nxt}; settle lock until {settle_until.isoformat()} "
          f"(window opens after, ≥{SETTLE_LOCK_SECONDS//60}min)")
    return nxt


def snapshot_pre_transition(db, round_label: str) -> None:
    """Back up the state a round-transition is about to mutate, so it can be
    reverted. Stored at transitionBackups/{round_label}. Captures each team's +
    player's pricing/elimination fields and each user's roster/budget/banked
    points, plus the config flags. Overwrites any prior backup for this round."""
    PRICE_FIELDS = ["marketValue", "buyPrice", "sellPrice", "currentPrice",
                    "liquidationValue", "meanFuturePoints", "priceHistory",
                    "eliminated"]
    USER_FIELDS = ["roster", "currentBudget", "bankedPoints", "bankedTiebreaker",
                   "exitedPicks", "valueByRound", "totalPoints", "tieBreakerScore"]
    teams = {d.id: {k: (d.to_dict() or {}).get(k) for k in PRICE_FIELDS}
             for d in db.collection("teams").stream()}
    players = {d.id: {k: (d.to_dict() or {}).get(k) for k in PRICE_FIELDS}
               for d in db.collection("players").stream()}
    users = {d.id: {k: (d.to_dict() or {}).get(k) for k in USER_FIELDS}
             for d in db.collection("users").stream()}
    cfg = db.collection("config").document("global").get().to_dict() or {}
    db.collection("transitionBackups").document(round_label).set({
        "round":         round_label,
        "createdAt":     datetime.now(timezone.utc).isoformat(),
        "config":        {"currentRound": cfg.get("currentRound"),
                          "transferWindowOpen": cfg.get("transferWindowOpen")},
        "teams":         teams,
        "players":       players,
        "users":         users,
    })
    print(f"  pre-transition snapshot saved (transitionBackups/{round_label}): "
          f"{len(teams)} teams, {len(players)} players, {len(users)} users")


def maybe_open_window(db, cfg: dict) -> bool:
    """Open trading once the settle lock has elapsed, and set windowClosesAt to
    guarantee the minimum open-trading period (pushing the close past the
    scheduled time if the bracket seeded late). Idempotent: no-op if the window
    is already open or we're not in a settling transition."""
    if cfg.get("transferWindowOpen"):
        return False
    if not cfg.get("transitionState"):
        return False
    current = (cfg.get("currentRound") or "pre")
    if current in ("pre", "done", "group"):
        return False
    settle_until = cfg.get("settleUntil")
    now = datetime.now(timezone.utc)
    if settle_until:
        dt = datetime.fromisoformat(settle_until) if "T" in settle_until else None
        if dt and now < dt:
            return False  # still settling

    from _fwc_lib import window_close_at
    closes = window_close_at(now, current, lead_seconds=WINDOW_CLOSE_LEAD_SECONDS)
    db.collection("config").document("global").set({
        "transferWindowOpen": True,
        "windowOpenedAt":     now.isoformat(),
        "windowClosesAt":     closes.isoformat(),
    }, merge=True)
    print(f"  Transfer window OPEN for {current}; closes {closes.isoformat()} "
          f"(≥4h guaranteed)")
    return True


def maybe_close_window(db, cfg: dict) -> bool:
    """Close the transfer window at its scheduled close time, and clear the
    transition banner state when we do. Prefers the windowClosesAt set at open
    time (which guarantees the ≥4h open period); falls back to "1h before the
    current round's first match" if that field isn't present (e.g. a window
    opened manually from admin). Idempotent. Returns True if we closed it."""
    if not cfg.get("transferWindowOpen"):
        return False
    current = (cfg.get("currentRound") or "pre")
    if current in ("pre", "done"):
        return False

    now = datetime.now(timezone.utc)

    closes_at = None
    wc = cfg.get("windowClosesAt")
    if wc:
        try:
            closes_at = datetime.fromisoformat(wc)
        except ValueError:
            closes_at = None

    if closes_at is None:
        # Fallback: 1h before the current round's earliest unplayed match.
        earliest_iso = None
        for mdoc in db.collection("matches").stream():
            m = mdoc.to_dict() or {}
            if m.get("round") != current or m.get("status") == "FINISHED":
                continue
            utc = m.get("utcDate")
            if utc and (earliest_iso is None or utc < earliest_iso):
                earliest_iso = utc
        if not earliest_iso:
            return False
        try:
            earliest_dt = datetime.fromisoformat(earliest_iso.replace("Z", "+00:00"))
        except ValueError:
            return False
        closes_at = earliest_dt - timedelta(seconds=WINDOW_CLOSE_LEAD_SECONDS)

    if now < closes_at:
        return False

    # Closing trading also ends the transition window - clear the banner state.
    db.collection("config").document("global").set({
        "transferWindowOpen": False,
        "transitionState":    False,
    }, merge=True)
    print(f"  Transfer window closed for {current}; transition state cleared")
    return True


def sync_schedule_to_config(db) -> None:
    """Cross-check the hardcoded canonical schedule against the feed, and publish
    the key dates into config/global so the timing logic and the UI both anchor
    to known public dates instead of guessing. Cheap (one match scan)."""
    from _fwc_lib import (
        schedule_drift, ROUND_FIRST_KICKOFF_UTC, GROUP_STAGE_LAST_KICKOFF_UTC,
    )
    matches = [m.to_dict() or {} for m in db.collection("matches").stream()]
    drift = schedule_drift(matches)
    for rnd, hard, feed in drift:
        print(f"  ⚠️ SCHEDULE DRIFT {rnd}: hardcoded {hard} vs feed {feed} - verify the schedule")
    db.collection("config").document("global").set({
        "groupStageEndsAt": GROUP_STAGE_LAST_KICKOFF_UTC,
        "roundStartsUtc":   dict(ROUND_FIRST_KICKOFF_UTC),
    }, merge=True)
    print(f"  schedule synced to config ({len(drift)} drift warning(s))")


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
    team_stats, latest_matchday = recompute_teams(db, weights, cfg.get("currentRound"))

    print("Step 3: recompute player goals/assists/wins/CS from match data…")
    recompute_players(db, team_stats, weights)

    print("Step 4: recompute user totals + leaderboard snapshot…")
    leaderboard = recompute_users(db, latest_matchday)
    write_leaderboard_snapshot(db, leaderboard)

    # Reload config - Steps 1-4 may have changed it (e.g., kickoff detection).
    cfg = db.collection("config").document("global").get().to_dict() or {}

    print("Step 5: check for round transition…")
    transitioned = maybe_transition_round(db, cfg)
    if transitioned:
        # Re-fetch config since the transition just changed it
        cfg = db.collection("config").document("global").get().to_dict() or {}

    print("Step 6: check if transfer window should open (settle lock elapsed)…")
    if maybe_open_window(db, cfg):
        cfg = db.collection("config").document("global").get().to_dict() or {}

    print("Step 7: check if transfer window should auto-close…")
    maybe_close_window(db, cfg)

    print("Step 8: sync canonical schedule to config + drift check…")
    sync_schedule_to_config(db)

    print("\nDone. Live state refreshed.")


if __name__ == "__main__":
    main()
