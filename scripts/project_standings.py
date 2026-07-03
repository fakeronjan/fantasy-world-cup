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

The knockout uses the REAL WC2026 bracket: group winners / runners-up fill
their fixed Round-of-32 slots and the bracket tree (matches 73-104) is followed
exactly, so a team's path difficulty is realistic. Seeding from simulated group
positions means the bracket becomes fully exact once the group stage finalizes.
The one residual approximation: the 8 best-third-place teams are matched to
their slots by the official group constraints via a valid bipartite matching,
not FIFA's exact allocation table (affects only which winner a 3rd-place team
first meets). See R32_SLOTS / BRACKET_TREE.

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
    simulate_match, _record_match, _winner_loser,
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
               "name": pd.get("name"),
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
        # Normalize "GROUP_A" -> "A" to match the bracket's single-letter slots.
        gl = (md.get("group") or "").split("_")[-1] or None
        if t1 in by_id: team_group[t1] = gl
        if t2 in by_id: team_group[t2] = gl
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
# Official WC2026 knockout bracket
# ---------------------------------------------------------------------------
# Round of 32 = matches 73-88. Slot spec: ("1","A")=winner of group A,
# ("2","A")=runner-up A, ("3","CDF")=best-third from one of those groups.
# Source: official FIFA bracket (en.wikipedia.org/wiki/2026_FIFA_World_Cup_knockout_stage).
R32_SLOTS = {
    73: (("2", "A"), ("2", "B")),
    74: (("1", "E"), ("3", "CDF")),
    75: (("1", "F"), ("2", "C")),
    76: (("1", "C"), ("2", "F")),
    77: (("1", "I"), ("3", "CDFGH")),
    78: (("2", "E"), ("2", "I")),
    79: (("1", "A"), ("3", "CEFHI")),
    80: (("1", "L"), ("3", "EIJK")),
    81: (("1", "D"), ("3", "BEFIJ")),
    82: (("1", "G"), ("3", "AEHIJ")),
    83: (("2", "K"), ("2", "L")),
    84: (("1", "H"), ("2", "J")),
    85: (("1", "B"), ("3", "EFGIJ")),
    86: (("1", "J"), ("2", "H")),
    87: (("1", "K"), ("3", "DEIJL")),
    88: (("2", "D"), ("2", "G")),
}
# Matches beyond R32: match_no -> (feeder_match_a, feeder_match_b).
BRACKET_TREE = {
    89: (73, 75), 90: (74, 77), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
    97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96),
    101: (97, 98), 102: (99, 100),
    104: (101, 102),
}
# Round a match WINNER advances into (drives the advancement bonus). The
# third-place playoff (103) is absent: it grants no bonus and no round change.
WIN_ADVANCES_TO = {**{m: "R16" for m in range(73, 89)},
                   **{m: "QF" for m in range(89, 97)},
                   **{m: "SF" for m in range(97, 101)},
                   101: "F", 102: "F", 104: "W"}
THIRD_SLOT_MATCHES = [m for m, (a, b) in R32_SLOTS.items() if b[0] == "3"]


def _group_positions(team_stats, team_group):
    """Per-group 1st/2nd/3rd by (pts, gd, gf), plus the 8 best third-place
    groups that advance. Returns (winners, runners, thirds, qual_third_groups)."""
    byg = defaultdict(list)
    for tid, g in team_group.items():
        if g:
            byg[g].append(tid)
    key = lambda t: (team_stats[t]["group_pts"], team_stats[t]["group_gd"],
                     team_stats[t]["group_gf"])
    winners, runners, thirds, third_list = {}, {}, {}, []
    for g, tids in byg.items():
        r = sorted(tids, key=key, reverse=True)
        if len(r) >= 1: winners[g] = r[0]
        if len(r) >= 2: runners[g] = r[1]
        if len(r) >= 3:
            thirds[g] = r[2]; third_list.append(g)
    third_list.sort(key=lambda g: key(thirds[g]), reverse=True)
    return winners, runners, thirds, third_list[:8]


def _assign_thirds(qual_groups):
    """Match the 8 qualifying third-place groups to the 8 third-place R32 slots,
    respecting each slot's allowed-group constraint (deterministic MRV
    backtracking). Any perfect matching is a valid bracket; we do not replicate
    FIFA's exact allocation table (the bracket's one residual approximation)."""
    slots = sorted(((m, frozenset(R32_SLOTS[m][1][1])) for m in THIRD_SLOT_MATCHES),
                   key=lambda s: (len(s[1]), s[0]))
    qual = sorted(qual_groups)
    result, used = {}, set()

    def bt(i):
        if i == len(slots):
            return True
        m, allowed = slots[i]
        for g in qual:
            if g in used or g not in allowed:
                continue
            used.add(g); result[m] = g
            if bt(i + 1):
                return True
            used.discard(g); del result[m]
        return False

    if not bt(0):
        # Rare (only if the qualifying set has no valid matching). Greedy fill.
        result.clear(); rem = list(qual)
        for m, allowed in slots:
            pick = next((g for g in rem if g in allowed), rem[0] if rem else None)
            if pick is not None:
                result[m] = pick; rem.remove(pick)
    return result


def _resolve_r32(winners, runners, thirds, third_assign, by_id):
    """Resolve the 16 R32 slot specs to (teamA_dict, teamB_dict) pairs."""
    def slot(spec, m):
        kind, ref = spec
        if kind == "1": return winners.get(ref)
        if kind == "2": return runners.get(ref)
        g = third_assign.get(m)
        return thirds.get(g) if g else None
    pairs = {}
    for m, (sa, sb) in R32_SLOTS.items():
        a, b = slot(sa, m), slot(sb, m)
        if a in by_id and b in by_id:
            pairs[m] = (by_id[a], by_id[b])
    return pairs


def _simulate_bracket(r32_pairs, team_stats, players_by_team, pg, pa, pw, pc):
    """Play matches 73..104 through the real bracket tree, mutating team_stats
    and the player accumulators. Returns the champion team id (or None)."""
    win, lose = {}, {}
    order = list(range(73, 101)) + [101, 102, 103, 104]
    for m in order:
        if m == 103:  # third-place playoff = the two semifinal losers
            if 101 not in lose or 102 not in lose:
                continue
            ta, tb = lose[101], lose[102]
        elif m in r32_pairs:
            ta, tb = r32_pairs[m]
        elif m in BRACKET_TREE:
            fa, fb = BRACKET_TREE[m]
            if fa not in win or fb not in win:
                continue
            ta, tb = win[fa], win[fb]
        else:
            continue
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        w, l = _winner_loser(ta, tb, ga, gb, pen)
        _record_match(ta, tb, ga, gb, team_stats, pg, pa, pw, pc,
                      players_by_team, is_group=False)
        win[m], lose[m] = w, l
        adv = WIN_ADVANCES_TO.get(m)
        if adv and (ADVANCEMENT_ORDER.index(adv)
                    > ADVANCEMENT_ORDER.index(team_stats[w["id"]]["final_round"])):
            team_stats[w["id"]]["final_round"] = adv
    return win[104]["id"] if 104 in win else None


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

    # Seed the REAL WC2026 bracket from group positions (actual + simulated),
    # then play it out through the fixed tree. Becomes exact once groups finish.
    # Pen wins record as draws (FIFA convention, matches the live game); the
    # winner still advances for the round bonus.
    winners, runners, thirds, qual = _group_positions(team_stats, team_group)
    third_assign = _assign_thirds(qual)
    r32_pairs = _resolve_r32(winners, runners, thirds, third_assign, by_id)
    for ta, tb in r32_pairs.values():
        for t in (ta, tb):
            if ADVANCEMENT_ORDER.index(team_stats[t["id"]]["final_round"]) < 1:
                team_stats[t["id"]]["final_round"] = "R32"
    champion_id = _simulate_bracket(r32_pairs, team_stats, players_by_team,
                                    pg, pa, pw, pc)

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

    return team_pts, player_pts, champion_id


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
    top5_counts = [0] * len(users)
    # Per-user per-asset future-point sums: overall, and conditioned on the user
    # finishing 1st / top-3 in that same sim. Drives "keys to win": an asset
    # whose future points are markedly higher in the sims where the user wins is
    # central to their winning path. This captures differentials for free -- an
    # asset everyone owns barely swings any one manager's RELATIVE finish, so its
    # win-conditional mean tracks its overall mean (near-zero lift), while a
    # unique pick that fires in your winning sims lifts sharply.
    asset_all  = [defaultdict(float) for _ in users]
    asset_win  = [defaultdict(float) for _ in users]
    asset_top3 = [defaultdict(float) for _ in users]
    t0 = time.time()
    for run in range(args.runs):
        tp, pp, _ = simulate_future(teams, by_id, players_by_team,
                                    actual_group, team_group, remaining_group)
        run_scores, run_contrib = [], []
        for i, u in enumerate(users):
            contrib = [((kind, aid), (tp if kind == "team" else pp).get(aid, 0))
                       for kind, aid in u["roster"]]
            run_contrib.append(contrib)
            run_scores.append(u["current"] + sum(v for _, v in contrib))
        for i, s in enumerate(run_scores):
            scores[i].append(s)
        # Finishing order this sim: #1 -> win, top 3 -> podium, top 5.
        order = sorted(range(len(run_scores)), key=lambda i: -run_scores[i])
        winner, top3 = order[0], set(order[:3])
        win_counts[winner] += 1
        for i in top3:
            top3_counts[i] += 1
        for i in order[:5]:
            top5_counts[i] += 1
        for i, contrib in enumerate(run_contrib):
            aa, aw, at3 = asset_all[i], asset_win[i], asset_top3[i]
            is_win, is_t3 = i == winner, i in top3
            for key, val in contrib:
                if not val:
                    continue
                aa[key] += val
                if is_win: aw[key] += val
                if is_t3: at3[key] += val
    elapsed = time.time() - t0
    print(f"{args.runs} runs in {elapsed:.1f}s", file=sys.stderr)

    # current ranking (by current total) for rank-shift readout
    cur_rank = {u["uid"]: r for r, u in enumerate(
        sorted(users, key=lambda x: -x["current"]), 1)}

    # "Keys to win": the held assets most central to a manager's best outcome.
    # Rank each asset by its LIFT = (mean future pts in the conditioning sims)
    # minus (mean future pts overall). Condition on winning sims if the manager
    # has a realistic title path, else on top-3 sims, else give up (goal="none").
    # A path is "realistic" at >=1% of sims (also the leaderboard's <1% display
    # cutoff) with an absolute floor for a stable conditional mean.
    KEY_THR = max(10, round(0.01 * args.runs))   # min sims to condition on
    MAX_KEYS = 3

    def keys_for(i, roster):
        if win_counts[i] >= KEY_THR:
            goal, cnt, cond = "win", win_counts[i], asset_win[i]
        elif top3_counts[i] >= KEY_THR:
            goal, cnt, cond = "top3", top3_counts[i], asset_top3[i]
        else:
            return "none", []
        ranked = []
        for kind, aid in roster:
            k = (kind, aid)
            cond_mean = cond.get(k, 0) / cnt
            lift = cond_mean - asset_all[i].get(k, 0) / args.runs
            ranked.append((lift, cond_mean, kind, aid))
        # Prefer positive-lift swing assets; if none swings (you coast in on
        # others' collapses), fall back to your biggest contributors so the
        # column is never empty when a path exists. Drop assets that never score.
        pos = [r for r in ranked if r[0] > 1e-9]
        pick = (sorted(pos, key=lambda r: -r[0]) if pos
                else sorted([r for r in ranked if r[1] > 0], key=lambda r: -r[1]))
        return goal, [{"kind": kind, "id": aid} for _, _, kind, aid in pick[:MAX_KEYS]]

    rows = []
    for i, u in enumerate(users):
        s = sorted(scores[i])
        n_team = sum(1 for k, _ in u["roster"] if k == "team")
        n_play = sum(1 for k, _ in u["roster"] if k == "player")
        team_spend = sum(team_price.get(a, 0) for k, a in u["roster"] if k == "team")
        play_spend = sum(player_price.get(a, 0) for k, a in u["roster"] if k == "player")
        tot_spend = team_spend + play_spend
        keys_goal, keys = keys_for(i, u["roster"])
        rows.append({
            "uid": u["uid"], "name": u["name"],
            "current": u["current"],
            "median": statistics.median(s),
            "p20": pctl(s, 0.20), "p80": pctl(s, 0.80),
            "mean": round(statistics.mean(s), 1),
            "winPct": round(100 * win_counts[i] / args.runs, 1),
            "top3Pct": round(100 * top3_counts[i] / args.runs, 1),
            "top5Pct": round(100 * top5_counts[i] / args.runs, 1),
            "teamShare": round(100 * team_spend / tot_spend) if tot_spend else 0,
            "nTeams": n_team, "nPlayers": n_play,
            "keysGoal": keys_goal, "keys": keys,
        })

    proj_rank = {r["uid"]: rk for rk, r in enumerate(
        sorted(rows, key=lambda x: -x["median"]), 1)}
    for r in rows:
        r["curRank"] = cur_rank[r["uid"]]
        r["projRank"] = proj_rank[r["uid"]]
        r["rankShift"] = cur_rank[r["uid"]] - proj_rank[r["uid"]]  # +ve = climbs

    rows.sort(key=lambda x: -x["median"])

    # asset id -> short name for the readout (front-end resolves its own labels)
    asset_name = {("team", t["id"]): t["name"] for t in teams}
    for ps in players_by_team.values():
        for p in ps:
            nm = (p.get("name") or p["id"]).split()[-1]
            asset_name[("player", p["id"])] = nm

    # ---- face-validity readout -------------------------------------------
    print(f"\n{'name':<20}{'cur':>5}{'rk':>3} -> {'med':>5}{'rk':>3}{'shift':>6}"
          f"{'p20':>6}{'p80':>6}{'win%':>6}{'top3%':>7}{'top5%':>7}  keys")
    for r in rows:
        arrow = f"+{r['rankShift']}" if r['rankShift'] > 0 else str(r['rankShift'])
        ktag = "" if r["keysGoal"] == "win" else f"[{r['keysGoal']}] "
        knames = ", ".join(asset_name.get((k["kind"], k["id"]), k["id"])
                           for k in r["keys"]) or "-"
        print(f"{r['name'][:19]:<20}{r['current']:>5}{r['curRank']:>3} -> "
              f"{r['median']:>5.0f}{r['projRank']:>3}{arrow:>6}"
              f"{r['p20']:>6.0f}{r['p80']:>6.0f}{r['winPct']:>5.1f}%"
              f"{r['top3Pct']:>6.1f}%{r['top5Pct']:>6.1f}%  {ktag}{knames}")

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
