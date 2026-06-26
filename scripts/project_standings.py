"""Project each user's FINAL fantasy total via Monte Carlo of the REMAINING
tournament, conditioned on results to date. Read-only on Firestore; writes
docs/data/projections.json for the leaderboard to surface.

Scoring matches the live game exactly (ingest_results.recompute_users uses
forward-only scoring):

    projected_user[sim] = user.totalPoints(now)  +  Σ future_pts[asset][sim]

where future_pts[asset] is the points that held asset earns from REMAINING
matches only. Future points always accrue AFTER purchase, so the live
forward-scoring clamp `max(0, total - pointsAtPurchase)` never bites the future
portion -- we just add each held asset's future points to the user's current
(locked) total. So we need nothing but each user's current total + roster
asset ids.

Projections assume the user's CURRENT roster plays out the rest of the
tournament (product decision: always project the current roster; the
leaderboard header says so).

The strength model + calibration live in simulate_2026.py (validated by
scripts/validate_model_honesty.py). We import its primitives so the projection
and the offline balance sim can never drift apart.

KNOWN SIMPLIFICATION (v1): the knockout bracket is seeded by standings
(1v32, 2v31, ...), NOT the real WC2026 bracket tree (which group result feeds
which slot). That gives favorites slightly easier early paths than reality and
thus mildly over-projects rosters holding favorites. Upgrade to the faithful
bracket map once R32 seeds. Search "TODO(bracket)".

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=.../key.json \
    ./venv/bin/python scripts/project_standings.py [--runs N] [--seed S] [--dry]
"""
from __future__ import annotations
import argparse, json, random, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _fwc_lib import firestore_client
from simulate_2026 import (
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
    simulate_match, _record_match, _winner_loser, _determine_advancers,
)

OUT_PATH = ROOT / "docs" / "data" / "projections.json"


# ---------------------------------------------------------------------------
# Load live state
# ---------------------------------------------------------------------------

def load_state(db):
    # Sort every collection by id so a fixed --seed yields byte-identical output
    # for identical state, regardless of Firestore stream order. (The sim's
    # random.choices walk these lists, so their order is part of the RNG state.)
    teams = []
    for t in db.collection("teams").stream():
        td = t.to_dict() or {}
        teams.append({"id": t.id, "name": td.get("name"),
                      "basePrice": td.get("basePrice") or 1,
                      "totalPoints": int(td.get("totalPoints") or 0),
                      "finalRound": td.get("finalRound") or "group"})
    teams.sort(key=lambda t: t["id"])
    by_id = {t["id"]: t for t in teams}

    players_by_team = defaultdict(list)
    player_total = {}
    for p in db.collection("players").stream():
        pd = p.to_dict() or {}
        row = {"id": p.id, "teamId": pd.get("teamId"),
               "position": (pd.get("position") or "?"),
               "basePrice": pd.get("basePrice") or 1,
               "totalPoints": int(pd.get("totalPoints") or 0)}
        players_by_team[row["teamId"]].append(row)
        player_total[p.id] = row["totalPoints"]
    for tid in players_by_team:
        players_by_team[tid].sort(key=lambda p: p["id"])

    # actual group tables (from FINISHED group matches), remaining group fixtures
    actual_group = {t["id"]: {"pts": 0, "gd": 0, "gf": 0} for t in teams}
    team_group, remaining_group = {}, []
    for m in db.collection("matches").stream():
        md = m.to_dict() or {}
        if (md.get("stage") or "GROUP_STAGE") != "GROUP_STAGE":
            continue
        t1, t2 = md.get("team1Id"), md.get("team2Id")
        if t1 in by_id: team_group[t1] = md.get("group")
        if t2 in by_id: team_group[t2] = md.get("group")
        if md.get("status") == "FINISHED" and md.get("score1") is not None:
            s1, s2 = int(md["score1"]), int(md["score2"])
            for tid, gf, ga in ((t1, s1, s2), (t2, s2, s1)):
                if tid not in actual_group:
                    continue
                actual_group[tid]["gf"] += gf
                actual_group[tid]["gd"] += gf - ga
                actual_group[tid]["pts"] += 3 if gf > ga else (1 if gf == ga else 0)
        elif t1 in by_id and t2 in by_id:
            remaining_group.append((by_id[t1], by_id[t2]))
    remaining_group.sort(key=lambda pair: (pair[0]["id"], pair[1]["id"]))

    users = []
    for u in db.collection("users").stream():
        ud = u.to_dict() or {}
        # Only currently-HELD picks earn future points (matches the live game's
        # forward-only scoring; sold picks are banked into totalPoints already).
        # No-op today (sales remove picks from roster) but correct once
        # knockout-round transfers leave currentlyHeld=false entries behind.
        roster = [(pk.get("kind"), pk.get("assetId"))
                  for pk in (ud.get("roster") or [])
                  if pk.get("currentlyHeld") is not False]
        users.append({"uid": u.id,
                      "name": ud.get("displayName") or ud.get("email") or u.id,
                      "current": int(ud.get("totalPoints") or 0),
                      "roster": roster})
    users.sort(key=lambda u: u["uid"])

    # Freshness marker tied to the ingest cron, NOT wall-clock: identical state
    # -> identical generatedAt -> the cron's commit step is a true no-op when
    # no new results have landed (otherwise the timestamp alone churns 96x/day).
    snap = (db.collection("leaderboard").document("snapshot").get().to_dict() or {})
    data_as_of = snap.get("updatedAt")

    return (teams, by_id, dict(players_by_team), player_total,
            actual_group, team_group, remaining_group, users, data_as_of)


# ---------------------------------------------------------------------------
# One Monte Carlo run of the REMAINING tournament
# ---------------------------------------------------------------------------

def simulate_future(teams, by_id, players_by_team, actual_group, team_group,
                    remaining_group):
    """Return (future_team_pts, future_player_pts, champion_id) for one run.
    team_stats is seeded with ACTUAL group tables (so advancement uses real +
    simulated results) but wins/draws start at 0 so they count FUTURE only."""
    team_stats = {t["id"]: dict(
        wins=0, draws=0, losses=0, goals_for=0, goals_against=0, clean_sheets=0,
        final_round=t["finalRound"],
        group_pts=actual_group[t["id"]]["pts"],
        group_gd=actual_group[t["id"]]["gd"],
        group_gf=actual_group[t["id"]]["gf"],
    ) for t in teams}
    pg = defaultdict(int); pa = defaultdict(int)
    pw = defaultdict(int); pc = defaultdict(int)

    # Remaining group matches
    for ta, tb in remaining_group:
        ga, gb, _ = simulate_match(ta, tb, ko=False)
        _record_match(ta, tb, ga, gb, team_stats, pg, pa, pw, pc,
                      players_by_team, is_group=True)

    # Advancers from combined (actual + simulated) group tables
    # TODO(bracket): _determine_advancers also SEEDS the bracket by standings
    # (1v32...). Replace with the real WC2026 bracket slot map once R32 seeds.
    advancer_ids = _determine_advancers(team_stats, team_group)
    advancers = [by_id[tid] for tid in advancer_ids if tid in by_id]
    for t in advancers:
        if ADVANCEMENT_ORDER.index(team_stats[t["id"]]["final_round"]) < 1:
            team_stats[t["id"]]["final_round"] = "R32"

    # Knockout rounds (pen wins recorded as draws -> FIFA convention, matches
    # the live game; the winner still advances for the round bonus).
    current = list(advancers)
    next_round = []
    for i in range(0, len(current), 2):
        if i + 1 >= len(current):
            next_round.append(current[i]); continue
        ta, tb = current[i], current[i + 1]
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        winner, _ = _winner_loser(ta, tb, ga, gb, pen)
        _record_match(ta, tb, ga, gb, team_stats, pg, pa, pw, pc,
                      players_by_team, is_group=False)
        team_stats[winner["id"]]["final_round"] = "R16"
        next_round.append(winner)
    current = next_round

    semis_losers, champion = [], None
    for round_label, next_label in [("R16", "QF"), ("QF", "SF"), ("SF", "F"), ("F", "W")]:
        nr = []
        for i in range(0, len(current), 2):
            if i + 1 >= len(current):
                nr.append(current[i]); continue
            ta, tb = current[i], current[i + 1]
            ga, gb, pen = simulate_match(ta, tb, ko=True)
            winner, loser = _winner_loser(ta, tb, ga, gb, pen)
            _record_match(ta, tb, ga, gb, team_stats, pg, pa, pw, pc,
                          players_by_team, is_group=False)
            team_stats[winner["id"]]["final_round"] = next_label
            if round_label == "SF":
                semis_losers.append(loser)
            if round_label == "F":
                champion = winner
            nr.append(winner)
        current = nr

    if len(semis_losers) == 2:
        ta, tb = semis_losers
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        _record_match(ta, tb, ga, gb, team_stats, pg, pa, pw, pc,
                      players_by_team, is_group=False)

    # Future points per asset
    team_pts = {}
    for tid, s in team_stats.items():
        pts = WEIGHTS["team_win"] * s["wins"] + WEIGHTS["team_draw"] * s["draws"]
        base_idx = ADVANCEMENT_ORDER.index(by_id[tid]["finalRound"])
        reached_idx = ADVANCEMENT_ORDER.index(s["final_round"])
        for r in ADVANCEMENT_ORDER[base_idx + 1: reached_idx + 1]:
            pts += BONUS_BY_ROUND.get(r, 0)
        team_pts[tid] = pts

    player_pts = {}
    for tid, ps in players_by_team.items():
        for p in ps:
            pos = p.get("position")
            cs = (WEIGHTS["player_clean_sheet_gk"] if pos == "GK"
                  else WEIGHTS["player_clean_sheet_def"] if pos == "DEF"
                  else WEIGHTS["player_clean_sheet_other"])
            player_pts[p["id"]] = (
                WEIGHTS["player_goal"] * pg.get(p["id"], 0)
                + WEIGHTS["player_assist"] * pa.get(p["id"], 0)
                + WEIGHTS["player_win_share"] * pw.get(p["id"], 0)
                + cs * pc.get(p["id"], 0))

    return team_pts, player_pts, (champion["id"] if champion else None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def pctl(sorted_vals, q):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry", action="store_true", help="print, don't write json")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    db = firestore_client()
    (teams, by_id, players_by_team, player_total, actual_group, team_group,
     remaining_group, users, data_as_of) = load_state(db)

    print(f"{len(teams)} teams, {len(users)} users, "
          f"{len(remaining_group)} remaining group matches", file=sys.stderr)

    # roster composition (team$ vs player$) for the face-validity readout
    team_price = {t["id"]: t["basePrice"] for t in teams}
    player_price = {p["id"]: p["basePrice"] for ps in players_by_team.values() for p in ps}

    scores = [[] for _ in users]
    win_counts = [0] * len(users)
    top3_counts = [0] * len(users)
    t0 = time.time()
    for run in range(args.runs):
        tp, pp, _ = simulate_future(teams, by_id, players_by_team,
                                    actual_group, team_group, remaining_group)
        run_scores = []
        for i, u in enumerate(users):
            fut = sum((tp if kind == "team" else pp).get(aid, 0)
                      for kind, aid in u["roster"])
            run_scores.append(u["current"] + fut)
        for i, s in enumerate(run_scores):
            scores[i].append(s)
        # Finishing order this sim: #1 -> win, top 3 -> podium.
        order = sorted(range(len(run_scores)), key=lambda i: -run_scores[i])
        win_counts[order[0]] += 1
        for i in order[:3]:
            top3_counts[i] += 1
    elapsed = time.time() - t0
    print(f"{args.runs} runs in {elapsed:.1f}s", file=sys.stderr)

    # current ranking (by current total) for rank-shift readout
    cur_rank = {u["uid"]: r for r, u in enumerate(
        sorted(users, key=lambda x: -x["current"]), 1)}

    rows = []
    for i, u in enumerate(users):
        s = sorted(scores[i])
        n_team = sum(1 for k, _ in u["roster"] if k == "team")
        n_play = sum(1 for k, _ in u["roster"] if k == "player")
        team_spend = sum(team_price.get(a, 0) for k, a in u["roster"] if k == "team")
        play_spend = sum(player_price.get(a, 0) for k, a in u["roster"] if k == "player")
        tot_spend = team_spend + play_spend
        rows.append({
            "uid": u["uid"], "name": u["name"],
            "current": u["current"],
            "median": statistics.median(s),
            "p20": pctl(s, 0.20), "p80": pctl(s, 0.80),
            "mean": round(statistics.mean(s), 1),
            "winPct": round(100 * win_counts[i] / args.runs, 1),
            "top3Pct": round(100 * top3_counts[i] / args.runs, 1),
            "teamShare": round(100 * team_spend / tot_spend) if tot_spend else 0,
            "nTeams": n_team, "nPlayers": n_play,
        })

    proj_rank = {r["uid"]: rk for rk, r in enumerate(
        sorted(rows, key=lambda x: -x["median"]), 1)}
    for r in rows:
        r["curRank"] = cur_rank[r["uid"]]
        r["projRank"] = proj_rank[r["uid"]]
        r["rankShift"] = cur_rank[r["uid"]] - proj_rank[r["uid"]]  # +ve = climbs

    rows.sort(key=lambda x: -x["median"])

    # ---- face-validity readout -------------------------------------------
    print(f"\n{'name':<20}{'cur':>5}{'rk':>3} -> {'med':>5}{'rk':>3}{'shift':>6}"
          f"{'p20':>6}{'p80':>6}{'win%':>6}{'top3%':>7}{'team$%':>7}")
    for r in rows:
        arrow = f"+{r['rankShift']}" if r['rankShift'] > 0 else str(r['rankShift'])
        print(f"{r['name'][:19]:<20}{r['current']:>5}{r['curRank']:>3} -> "
              f"{r['median']:>5.0f}{r['projRank']:>3}{arrow:>6}"
              f"{r['p20']:>6.0f}{r['p80']:>6.0f}{r['winPct']:>5.1f}%"
              f"{r['top3Pct']:>6.1f}%{r['teamShare']:>6}%")

    out = {
        "generatedAt": data_as_of or datetime.now(timezone.utc).isoformat(),
        "runs": args.runs,
        "assumesCurrentRoster": True,
        "bracketModel": "standings-seeded (v1 approximation)",
        "users": rows,
    }
    if args.dry:
        print("\n[dry run - projections.json not written]", file=sys.stderr)
    else:
        OUT_PATH.write_text(json.dumps(out, indent=2))
        print(f"\nwrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
