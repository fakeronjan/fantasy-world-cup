"""Monte Carlo simulation of WC 2026 with 15 mock contestants.

Each run:
  1. Simulates all 72 group-stage matches via Poisson goal-sampling
     weighted by team prices (proxy for strength).
  2. Computes standings, advances top 2 + 8 best 3rd-place to R32.
  3. Simulates knockouts (R32 → R16 → QF → SF → F + 3rd-place match).
     Ties go to ET (lower goal rate) then 50/50 penalties.
  4. Distributes goals to players within the scoring team's draftable
     squad, weighted by position + price.
  5. Computes fantasy points per asset using the locked scoring weights.
  6. Scores each of 15 contestants by aggregating their roster's points.

Across 1000 runs:
  - Records per-contestant point distribution (mean, median, p25, p75,
    min, max, win-rate).
  - Identifies systematically-favored strategies.

Usage:
  ./venv/bin/python scripts/simulate_2026.py [--runs N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SEED_TEAMS_PATH   = ROOT / "docs" / "data" / "seed_teams.json"
SEED_PLAYERS_PATH = ROOT / "docs" / "data" / "seed_players.json"
MATCHES_CACHE     = ROOT / "data" / "wc2026_matches_cache.json"

# Locked scoring weights (2026-05-25 — Deep Data tier active)
WEIGHTS = {
    "team_win": 4, "team_draw": 1,
    "bonus_r32": 2, "bonus_r16": 3, "bonus_qf": 5, "bonus_sf": 8,
    "bonus_final": 12, "bonus_champion": 20,
    "player_goal": 5,
    "player_assist": 3,           # restored after Deep Data unlocked assist data
    "player_win_share": 1,        # lineup-based — only players who played
    "player_clean_sheet_gk": 5,   # lineup-based
    "player_clean_sheet_def": 2,  # FPL-style: defenders earn CS bonus
    "player_clean_sheet_other": 0,  # MID/FWD/Unknown earn nothing for CS
}

# Match simulation lineup parameters
LINEUP_STARTERS = 11
LINEUP_SUBS_USED = 3   # avg substitutes that come on per match

ADVANCEMENT_ORDER = ["group", "R32", "R16", "QF", "SF", "F", "W"]
BONUS_BY_ROUND = {
    "R32": WEIGHTS["bonus_r32"], "R16": WEIGHTS["bonus_r16"],
    "QF":  WEIGHTS["bonus_qf"],  "SF":  WEIGHTS["bonus_sf"],
    "F":   WEIGHTS["bonus_final"], "W": WEIGHTS["bonus_champion"],
}

# Match simulation parameters
BASE_GOALS_PER_MATCH = 2.6  # historical WC avg ≈ 2.5-2.7
ET_GOALS_PER_MATCH   = 0.6  # extra time has fewer goals


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_seed():
    teams = json.loads(SEED_TEAMS_PATH.read_text())
    players = json.loads(SEED_PLAYERS_PATH.read_text())
    return teams, players


def load_match_schedule():
    """Fetch (or load from cache) the WC 2026 match schedule."""
    if MATCHES_CACHE.exists():
        return json.loads(MATCHES_CACHE.read_text())
    # Fetch from football-data.org
    env_path = Path("/Users/ronjan/My Drive/~RJ/fakeronjan/Power Rankings/soccer club/.env")
    key = os.environ.get("FOOTBALL_DATA_KEY")
    if not key and env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("FOOTBALL_DATA_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("FOOTBALL_DATA_KEY not found")
    req = Request(
        "https://api.football-data.org/v4/competitions/WC/matches",
        headers={"X-Auth-Token": key},
    )
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    matches = data.get("matches", [])
    MATCHES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_CACHE.write_text(json.dumps(matches, indent=2, ensure_ascii=False))
    return matches


# ---------------------------------------------------------------------------
# Map football-data team names to our team slugs
# ---------------------------------------------------------------------------

def build_team_indexes(teams):
    by_slug = {t["id"]: t for t in teams}
    by_fdid = {t["fdId"]: t for t in teams if t.get("fdId") is not None}
    return by_slug, by_fdid


# ---------------------------------------------------------------------------
# Match outcome simulation
# ---------------------------------------------------------------------------

def poisson_sample(lam: float) -> int:
    """Knuth's algorithm — sample from Poisson(lam)."""
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def match_lambdas(team_a, team_b, base=BASE_GOALS_PER_MATCH):
    """Return (lambda_a, lambda_b) for Poisson goal sampling.
    Uses sqrt of prices to soften the favorite's edge."""
    pa = max(1, team_a["basePrice"])
    pb = max(1, team_b["basePrice"])
    sa = math.sqrt(pa)
    sb = math.sqrt(pb)
    share = sa / (sa + sb)
    return base * share, base * (1 - share)


def simulate_match(team_a, team_b, ko=False, base=None):
    """Returns (goals_a, goals_b, went_to_pens, pen_winner).
    For knockouts: if tied at 90, simulate ET; if still tied, 50/50 pens.
    pen_winner is 'a' or 'b' or None."""
    if base is None:
        base = BASE_GOALS_PER_MATCH
    la, lb = match_lambdas(team_a, team_b, base)
    ga = poisson_sample(la)
    gb = poisson_sample(lb)
    pen_winner = None
    if ko and ga == gb:
        # Extra time
        la_et, lb_et = match_lambdas(team_a, team_b, ET_GOALS_PER_MATCH)
        et_a = poisson_sample(la_et)
        et_b = poisson_sample(lb_et)
        ga += et_a; gb += et_b
        if ga == gb:
            pen_winner = "a" if random.random() < 0.5 else "b"
    return ga, gb, pen_winner


# ---------------------------------------------------------------------------
# Goal attribution
# ---------------------------------------------------------------------------

POS_WEIGHTS = {"FWD": 4.0, "MID": 2.0, "DEF": 1.0, "GK": 0.1, "?": 1.0}
ASSIST_WEIGHTS = {"FWD": 2.0, "MID": 3.0, "DEF": 1.0, "GK": 0.1, "?": 1.0}


def attribute_goals(team, num_goals, players_by_team):
    """Pick N draftable players to credit each goal."""
    if num_goals == 0:
        return []
    candidates = players_by_team.get(team["id"], [])
    if not candidates:
        return []
    weights = [max(0.01, POS_WEIGHTS.get(p.get("position", "?"), 1.0) * p.get("basePrice", 1))
                for p in candidates]
    return random.choices(candidates, weights=weights, k=num_goals)


def attribute_assists(team, num_assists, players_by_team, scorer_ids):
    """Pick assist credits. Midfielders weighted higher. Avoid same player as scorer."""
    if num_assists == 0:
        return []
    candidates = [p for p in players_by_team.get(team["id"], []) if p["id"] not in scorer_ids]
    if not candidates:
        return []
    weights = [max(0.01, ASSIST_WEIGHTS.get(p.get("position", "?"), 1.0) * p.get("basePrice", 1))
                for p in candidates]
    return random.choices(candidates, weights=weights, k=num_assists)


def pick_lineup(team, players_by_team):
    """Pick a probable starting XI + subs-in for one match.
    Weighted by price + position (GK always starts 1, outfield by price).
    Returns set of player IDs who 'played' (eligible for win-share/CS)."""
    candidates = players_by_team.get(team["id"], [])
    if not candidates:
        return set()
    gks = [p for p in candidates if p.get("position") == "GK"]
    outfield = [p for p in candidates if p.get("position") != "GK"]
    played = set()
    # 1 GK — weighted by price
    if gks:
        w = [max(0.1, p.get("basePrice", 1)) for p in gks]
        chosen = random.choices(gks, weights=w, k=1)[0]
        played.add(chosen["id"])
    # 10 outfield starters
    if outfield:
        w = [max(0.1, p.get("basePrice", 1)) for p in outfield]
        # Sample without replacement, weighted
        idx_pool = list(range(len(outfield)))
        for _ in range(min(LINEUP_STARTERS - 1, len(outfield))):
            total = sum(w[i] for i in idx_pool)
            if total <= 0: break
            r = random.uniform(0, total)
            cum = 0
            for i, idx in enumerate(idx_pool):
                cum += w[idx]
                if r <= cum:
                    played.add(outfield[idx]["id"])
                    idx_pool.pop(i)
                    break
        # Subs come on — sample more from remaining pool
        for _ in range(min(LINEUP_SUBS_USED, len(idx_pool))):
            total = sum(w[i] for i in idx_pool)
            if total <= 0: break
            r = random.uniform(0, total)
            cum = 0
            for i, idx in enumerate(idx_pool):
                cum += w[idx]
                if r <= cum:
                    played.add(outfield[idx]["id"])
                    idx_pool.pop(i)
                    break
    return played


# ---------------------------------------------------------------------------
# Tournament simulation
# ---------------------------------------------------------------------------

def run_tournament(teams, matches, by_fdid, players_by_team):
    """One run. Returns dict with per-team stats + per-player goal counts."""
    # Per-team accumulators
    team_stats = {t["id"]: dict(
        wins=0, draws=0, losses=0,
        goals_for=0, goals_against=0,
        clean_sheets=0,
        final_round="group",
        group_pts=0, group_gd=0, group_gf=0,
    ) for t in teams}
    # Per-player accumulators
    player_goals = defaultdict(int)
    player_assists = defaultdict(int)
    player_wins_played = defaultdict(int)
    player_cs_played = defaultdict(int)
    # Track which group each team is in
    team_group = {}

    # Group-stage matches
    group_matches = [m for m in matches if (m.get("stage") or "GROUP_STAGE") == "GROUP_STAGE"]
    for m in group_matches:
        ta_fd = (m.get("homeTeam") or {}).get("id")
        tb_fd = (m.get("awayTeam") or {}).get("id")
        ta = by_fdid.get(ta_fd)
        tb = by_fdid.get(tb_fd)
        if not (ta and tb):
            continue
        team_group[ta["id"]] = m.get("group")
        team_group[tb["id"]] = m.get("group")

        ga, gb, _ = simulate_match(ta, tb, ko=False)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=True)

    # Compute group standings, determine advancers
    by_slug = {t["id"]: t for t in teams}
    advancer_ids = _determine_advancers(team_stats, team_group)
    advancers = [by_slug[tid] for tid in advancer_ids if tid in by_slug]
    for t in advancers:
        team_stats[t["id"]]["final_round"] = "R32"

    # Knockouts — simplified pairing by overall rank among advancers
    current = list(advancers)
    knockout_rounds = ["R16", "QF", "SF", "F"]
    losers_at = {}  # team_id → round they lost in

    # R32 → R16
    round_label = "R32"
    next_round = []
    for i in range(0, len(current), 2):
        if i + 1 >= len(current):
            next_round.append(current[i]); continue
        ta, tb = current[i], current[i + 1]
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        winner, loser = _winner_loser(ta, tb, ga, gb, pen)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=False)
        team_stats[winner["id"]]["final_round"] = "R16"
        losers_at[loser["id"]] = "R32"
        next_round.append(winner)
    current = next_round

    # Subsequent rounds
    semis_losers = []  # for 3rd-place match
    final_loser = None
    champion = None
    for round_label, next_label in [("R16", "QF"), ("QF", "SF"), ("SF", "F"), ("F", "W")]:
        next_round = []
        for i in range(0, len(current), 2):
            if i + 1 >= len(current):
                next_round.append(current[i]); continue
            ta, tb = current[i], current[i + 1]
            ga, gb, pen = simulate_match(ta, tb, ko=True)
            winner, loser = _winner_loser(ta, tb, ga, gb, pen)
            _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                          player_wins_played, player_cs_played,
                          players_by_team, is_group=False)
            team_stats[winner["id"]]["final_round"] = next_label
            losers_at[loser["id"]] = round_label
            if round_label == "SF":
                semis_losers.append(loser)
            if round_label == "F":
                final_loser = loser
                champion = winner
            next_round.append(winner)
        current = next_round

    # 3rd-place match
    if len(semis_losers) == 2:
        ta, tb = semis_losers
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=False)
        # 3rd-place doesn't change final_round (both stay at SF)

    return {
        "team_stats": team_stats,
        "player_goals": dict(player_goals),
        "player_assists": dict(player_assists),
        "player_wins_played": dict(player_wins_played),
        "player_cs_played": dict(player_cs_played),
        "champion_id": champion["id"] if champion else None,
    }


def _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                   player_wins_played, player_cs_played,
                   players_by_team, is_group):
    sa = team_stats[ta["id"]]
    sb = team_stats[tb["id"]]
    sa["goals_for"] += ga; sa["goals_against"] += gb
    sb["goals_for"] += gb; sb["goals_against"] += ga
    if is_group:
        sa["group_gf"] += ga; sa["group_gd"] += ga - gb
        sb["group_gf"] += gb; sb["group_gd"] += gb - ga
    if ga > gb:
        sa["wins"] += 1; sb["losses"] += 1
        if is_group: sa["group_pts"] += 3
    elif gb > ga:
        sb["wins"] += 1; sa["losses"] += 1
        if is_group: sb["group_pts"] += 3
    else:
        sa["draws"] += 1; sb["draws"] += 1
        if is_group:
            sa["group_pts"] += 1; sb["group_pts"] += 1
    cs_a = (gb == 0)
    cs_b = (ga == 0)
    if cs_a: sa["clean_sheets"] += 1
    if cs_b: sb["clean_sheets"] += 1

    # Pick effective lineups for this match (lineup-based scoring)
    lineup_a = pick_lineup(ta, players_by_team)
    lineup_b = pick_lineup(tb, players_by_team)

    # Goals + assists
    scorers_a = attribute_goals(ta, ga, players_by_team)
    scorers_b = attribute_goals(tb, gb, players_by_team)
    for p in scorers_a: player_goals[p["id"]] += 1
    for p in scorers_b: player_goals[p["id"]] += 1
    # Assists: ~60% of goals have an assist
    assists_a = attribute_assists(ta, sum(1 for _ in scorers_a if random.random() < 0.6),
                                    players_by_team, {p["id"] for p in scorers_a})
    assists_b = attribute_assists(tb, sum(1 for _ in scorers_b if random.random() < 0.6),
                                    players_by_team, {p["id"] for p in scorers_b})
    for p in assists_a: player_assists[p["id"]] += 1
    for p in assists_b: player_assists[p["id"]] += 1

    # Lineup-based win share + CS
    a_won = ga > gb
    b_won = gb > ga
    for pid in lineup_a:
        if a_won: player_wins_played[pid] += 1
        if cs_a:  player_cs_played[pid]   += 1
    for pid in lineup_b:
        if b_won: player_wins_played[pid] += 1
        if cs_b:  player_cs_played[pid]   += 1


def _winner_loser(ta, tb, ga, gb, pen):
    if pen == "a": return ta, tb
    if pen == "b": return tb, ta
    return (ta, tb) if ga > gb else (tb, ta)


def _determine_advancers(team_stats, team_group):
    """Top 2 from each of 12 groups + 8 best 3rd-place teams."""
    by_group = defaultdict(list)
    for tid, group in team_group.items():
        if group:
            by_group[group].append(tid)

    advancing_ids = []
    third_place_candidates = []
    for group, tids in by_group.items():
        ranked = sorted(tids, key=lambda t: (
            team_stats[t]["group_pts"],
            team_stats[t]["group_gd"],
            team_stats[t]["group_gf"],
        ), reverse=True)
        # top 2 advance
        if len(ranked) >= 1: advancing_ids.append(ranked[0])
        if len(ranked) >= 2: advancing_ids.append(ranked[1])
        if len(ranked) >= 3: third_place_candidates.append(ranked[2])

    # Top 8 third-place teams
    third_place_candidates.sort(key=lambda t: (
        team_stats[t]["group_pts"],
        team_stats[t]["group_gd"],
        team_stats[t]["group_gf"],
    ), reverse=True)
    advancing_ids.extend(third_place_candidates[:8])

    # Need exactly 32 to pair cleanly. If short, fill with next-best teams.
    if len(advancing_ids) < 32:
        # rare; only if some groups had <4 teams
        all_ranked = sorted(team_stats.keys(), key=lambda t: (
            team_stats[t]["group_pts"],
            team_stats[t]["group_gd"],
            team_stats[t]["group_gf"],
        ), reverse=True)
        for t in all_ranked:
            if t not in advancing_ids:
                advancing_ids.append(t)
            if len(advancing_ids) == 32: break

    # Now seed: sort all advancers by group stage points, pair high vs low
    advancing_ids.sort(key=lambda t: (
        team_stats[t]["group_pts"],
        team_stats[t]["group_gd"],
        team_stats[t]["group_gf"],
    ), reverse=True)
    # Build advancers list of team dicts (we need team dicts to call simulate_match)
    advancers = [advancing_ids[i] for i in range(min(32, len(advancing_ids)))]
    # Pair 1v32, 2v31, ..., 16v17 — interleave so the bracket pairs match
    paired = []
    for i in range(len(advancers) // 2):
        paired.append(advancers[i])
        paired.append(advancers[-(i + 1)])
    # Return as list of team dicts
    return paired  # team ids in pair order


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_asset_points(teams_dicts, team_stats, players_by_team,
                        player_goals, player_assists,
                        player_wins_played, player_cs_played):
    """Return (team_pts[id], player_pts[id])."""
    team_pts = {}
    for tid, stats in team_stats.items():
        pts = WEIGHTS["team_win"] * stats["wins"] + WEIGHTS["team_draw"] * stats["draws"]
        fr = stats["final_round"]
        if fr in BONUS_BY_ROUND:
            idx = ADVANCEMENT_ORDER.index(fr)
            for r in ADVANCEMENT_ORDER[1:idx + 1]:
                pts += BONUS_BY_ROUND[r]
        team_pts[tid] = pts

    player_pts = {}
    for tid, ps in players_by_team.items():
        for p in ps:
            pos = p.get("position")
            if   pos == "GK":  cs_rate = WEIGHTS["player_clean_sheet_gk"]
            elif pos == "DEF": cs_rate = WEIGHTS["player_clean_sheet_def"]
            else:                cs_rate = WEIGHTS["player_clean_sheet_other"]
            pts = (
                WEIGHTS["player_goal"]       * player_goals.get(p["id"], 0)
                + WEIGHTS["player_assist"]    * player_assists.get(p["id"], 0)
                + WEIGHTS["player_win_share"] * player_wins_played.get(p["id"], 0)
                + cs_rate                     * player_cs_played.get(p["id"], 0)
            )
            player_pts[p["id"]] = pts
    return team_pts, player_pts


# ---------------------------------------------------------------------------
# Contestants
# ---------------------------------------------------------------------------

BUDGET = 60  # 2026-05-25 final


def _top_up_to_100(picks, teams, players, used_ids):
    """Fill unspent budget by picking the LARGEST price that fits in remaining
    budget (greedy descending). Despite the function name (kept for compat),
    it tops up to BUDGET (currently 50)."""
    teams_by_slug = {t["id"]: t for t in teams}
    players_by_id = {p["id"]: p for p in players}
    cost = sum(
        (teams_by_slug if kind == "team" else players_by_id)[id_]["basePrice"]
        for kind, id_ in picks
    )
    remaining = BUDGET - cost
    if remaining <= 0:
        return picks
    pool = []
    for t in teams:
        if ("team", t["id"]) not in used_ids:
            pool.append(("team", t["id"], t["basePrice"]))
    for p in players:
        if ("player", p["id"]) not in used_ids:
            pool.append(("player", p["id"], p["basePrice"]))
    pool.sort(key=lambda x: -x[2])  # descending
    while remaining > 0 and len(picks) < 12:  # was 20
        # Pick the highest-priced asset that fits
        best = None
        for kind, id_, price in pool:
            if price <= remaining:
                best = (kind, id_, price)
                break
        if best is None: break
        picks.append((best[0], best[1]))
        used_ids.add((best[0], best[1]))
        remaining -= best[2]
        # Remove from pool
        pool = [(k, i, p) for (k, i, p) in pool if (k, i) != (best[0], best[1])]
    return picks


def build_controlled_contestants(teams, players):
    """6 archetypes that vary ONE dimension at a time:
       (teams | players)  ×  (top-heavy | middle | cinderella)
    Lets us cleanly answer:
       a) teams vs players
       b) team distribution effects
       c) player distribution effects"""
    teams_desc   = sorted(teams,   key=lambda x: -x["basePrice"])
    teams_asc    = sorted(teams,   key=lambda x:  x["basePrice"])
    players_desc = sorted(players, key=lambda x: -x["basePrice"])
    players_asc  = sorted(players, key=lambda x:  x["basePrice"])
    teams_mid    = [t for t in teams   if 8 <= t["basePrice"] <= 15]
    players_mid  = [p for p in players if 8 <= p["basePrice"] <= 14]

    def fill_greedy(pool, kind, budget=60, cap=12):
        """Greedy: walk pool in given order, take each that fits."""
        picks, spent = [], 0
        for a in pool:
            if len(picks) >= cap: break
            if spent + a["basePrice"] <= budget:
                picks.append(("team" if kind == "team" else "player", a["id"]))
                spent += a["basePrice"]
            if spent == budget: break
        return picks

    out = []

    # A1. All TEAMS, TOP-heavy (most expensive first)
    out.append({"name": "A. Teams — TOP-heavy",
                "picks": fill_greedy(teams_desc, "team")})

    # A2. All TEAMS, MIDDLE (only $8-15 teams)
    out.append({"name": "B. Teams — MIDDLE ($8-15)",
                "picks": fill_greedy(sorted(teams_mid, key=lambda x: -x["basePrice"]), "team")})

    # A3. All TEAMS, CINDERELLA — capped at $10 to allow $100 spend within 20 picks
    cinderella_teams_desc = sorted([t for t in teams if t["basePrice"] <= 10],
                                     key=lambda x: -x["basePrice"])
    out.append({"name": "C. Teams — CINDERELLA (≤$10)",
                "picks": fill_greedy(cinderella_teams_desc, "team")})

    # B1. All PLAYERS, TOP-heavy
    out.append({"name": "D. Players — TOP-heavy",
                "picks": fill_greedy(players_desc, "player")})

    # B2. All PLAYERS, MIDDLE ($8-14)
    out.append({"name": "E. Players — MIDDLE ($8-14)",
                "picks": fill_greedy(sorted(players_mid, key=lambda x: -x["basePrice"]), "player")})

    # B3. All PLAYERS, CINDERELLA — capped at $7, descending to spend budget faster
    cinderella_players_desc = sorted([p for p in players if p["basePrice"] <= 7],
                                       key=lambda x: -x["basePrice"])
    out.append({"name": "F. Players — CINDERELLA (≤$7)",
                "picks": fill_greedy(cinderella_players_desc, "player")})

    # Top up any contestant that didn't hit $100 (cinderella often runs out of
    # picks before $100 is fully spent). Top-up uses _top_up_to_100 which
    # picks the LARGEST-priced unused asset that fits.
    for c in out:
        used = set(c["picks"])
        c["picks"] = _top_up_to_100(c["picks"], teams, players, used)

    return out


def build_contestants(teams, players):
    """Build 15 mock contestants with $100 rosters spanning the strategy space.
    Returns list of {name, picks: [(kind, id), ...], total_cost, total_picks}.
    """
    teams_by_slug   = {t["id"]: t for t in teams}
    players_by_id   = {p["id"]: p for p in players}
    teams_sorted    = sorted(teams, key=lambda t: -t["basePrice"])
    players_sorted  = sorted(players, key=lambda p: -p["basePrice"])

    def pick_id(kind, id_): return ("team" if kind == "team" else "player", id_)
    def team_id(name): return next(t["id"] for t in teams if t["name"] == name)
    def player_id_by_name(name): return next((p["id"] for p in players if p["name"] == name), None)

    contestants = []

    # ---- TEAMS-ONLY (5) ----

    # 1. Big4 Spender — top 3 teams + cheap fillers
    c1 = [pick_id("team", t["id"]) for t in teams_sorted[:3]]  # ARG, FRA, BRA = 30+28+26 = 84
    # Need $16 more. Fill with cheap.
    remaining = 100 - 84
    for t in [tt for tt in teams_sorted if tt["basePrice"] <= 2 and pick_id("team", tt["id"]) not in c1]:
        if remaining < t["basePrice"]: continue
        if len(c1) >= 20: break
        c1.append(pick_id("team", t["id"]))
        remaining -= t["basePrice"]
        if remaining == 0: break
    contestants.append({"name": "1. Big3 + cheap fillers", "picks": c1})

    # 2. Champion + Field
    c2 = [pick_id("team", team_id("Argentina"))]  # $30
    remaining = 70
    # Add Norway $14 + mid teams + cheap
    for name in ["Norway", "Mexico", "Senegal", "Sweden", "Iran", "Tunisia", "Australia",
                 "Algeria", "Czechia", "Austria", "Paraguay", "Scotland", "Bosnia-Herzegovina"]:
        try:
            t = teams_by_slug[team_id(name)]
        except StopIteration:
            continue
        if remaining >= t["basePrice"] and len(c2) < 20:
            c2.append(pick_id("team", t["id"]))
            remaining -= t["basePrice"]
        if remaining == 0: break
    # Fill leftover with $1s
    if remaining > 0:
        for t in teams:
            if t["basePrice"] == 1 and pick_id("team", t["id"]) not in c2 and remaining > 0 and len(c2) < 20:
                c2.append(pick_id("team", t["id"]))
                remaining -= 1
    contestants.append({"name": "2. Argentina + spread", "picks": c2})

    # 3. Mid Strength — all $8-15 teams
    c3 = []
    remaining = 100
    for t in teams_sorted:
        if 8 <= t["basePrice"] <= 15 and remaining >= t["basePrice"] and len(c3) < 20:
            c3.append(pick_id("team", t["id"]))
            remaining -= t["basePrice"]
    contestants.append({"name": "3. Mid-strength teams only", "picks": c3})

    # 4. Cinderella Spammer — cheap teams, max picks
    c4 = []
    remaining = 100
    cheap_teams = sorted([t for t in teams if t["basePrice"] <= 7], key=lambda t: -t["basePrice"])
    for t in cheap_teams:
        if remaining >= t["basePrice"] and len(c4) < 20:
            c4.append(pick_id("team", t["id"]))
            remaining -= t["basePrice"]
    contestants.append({"name": "4. Cinderella spam (all cheap teams)", "picks": c4})

    # 5. Balanced Teams — 1 top + 2 mid + cheap
    c5 = [pick_id("team", team_id("Argentina"))]  # $30
    remaining = 70
    for name in ["Belgium", "Croatia"]:  # mid
        t = teams_by_slug[team_id(name)]
        c5.append(pick_id("team", t["id"]))
        remaining -= t["basePrice"]
    for name in ["Mexico", "Senegal", "Sweden", "Iran", "Algeria", "Saudi Arabia", "Qatar", "Bosnia-Herzegovina"]:
        try:
            tid = team_id(name)
        except StopIteration:
            continue
        t = teams_by_slug[tid]
        if remaining >= t["basePrice"] and len(c5) < 20:
            c5.append(pick_id("team", tid))
            remaining -= t["basePrice"]
        if remaining == 0: break
    contestants.append({"name": "5. Balanced teams (1 top + 2 mid + spread)", "picks": c5})

    # ---- PLAYERS-ONLY (5) ----

    # 6. All Stars — top 5 superstars
    c6 = []
    remaining = 100
    for p in players_sorted:
        if len(c6) >= 6: break
        if remaining >= p["basePrice"] and p["basePrice"] >= 15:
            c6.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    # Fill any leftover with cheap players
    for p in sorted(players, key=lambda x: x["basePrice"]):
        if pick_id("player", p["id"]) in c6: continue
        if remaining >= p["basePrice"] and len(c6) < 20:
            c6.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
        if remaining == 0: break
    contestants.append({"name": "6. All-stars (top 5-6 superstars)", "picks": c6})

    # 7. Mid Veterans — all $10-15 players
    c7 = []
    remaining = 100
    for p in players_sorted:
        if 10 <= p["basePrice"] <= 15 and remaining >= p["basePrice"] and len(c7) < 20:
            c7.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    contestants.append({"name": "7. Mid-tier players ($10-15)", "picks": c7})

    # 8. Cheap Spam Players
    c8 = []
    remaining = 100
    cheap_players = sorted([p for p in players if p["basePrice"] <= 5], key=lambda p: -p["basePrice"])
    for p in cheap_players:
        if remaining >= p["basePrice"] and len(c8) < 20:
            c8.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    contestants.append({"name": "8. Cheap player spam (≤$5 each)", "picks": c8})

    # 9. Forwards-only
    c9 = []
    remaining = 100
    fwds = sorted([p for p in players if p["position"] == "FWD"], key=lambda p: -p["basePrice"])
    for p in fwds:
        if remaining >= p["basePrice"] and len(c9) < 20:
            c9.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    contestants.append({"name": "9. Forwards only", "picks": c9})

    # 10. GK + DEF only (defensive)
    c10 = []
    remaining = 100
    defs = sorted([p for p in players if p["position"] in ("GK", "DEF")],
                   key=lambda p: -p["basePrice"])
    for p in defs:
        if remaining >= p["basePrice"] and len(c10) < 20:
            c10.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    contestants.append({"name": "10. GKs + Defenders only", "picks": c10})

    # ---- MIX (5) ----

    # 11. Star + Champion: Argentina + top players + cheap teams
    c11 = [pick_id("team", team_id("Argentina"))]
    remaining = 70
    for name in ["Kylian Mbappé", "Lionel Messi"]:
        pid = player_id_by_name(name)
        if pid and remaining >= players_by_id[pid]["basePrice"]:
            c11.append(pick_id("player", pid))
            remaining -= players_by_id[pid]["basePrice"]
    # Add Yamal and cheap teams
    yp = player_id_by_name("Lamine Yamal")
    if yp and remaining >= players_by_id[yp]["basePrice"]:
        c11.append(pick_id("player", yp))
        remaining -= players_by_id[yp]["basePrice"]
    # Cheap teams to round out
    for t in [tt for tt in teams if tt["basePrice"] <= 2]:
        if remaining >= t["basePrice"] and pick_id("team", t["id"]) not in c11 and len(c11) < 20:
            c11.append(pick_id("team", t["id"]))
            remaining -= t["basePrice"]
        if remaining == 0: break
    contestants.append({"name": "11. Star + Champion + spread", "picks": c11})

    # 12. Diversifier — 1 team + 1 player from each rough price band
    c12 = []
    remaining = 100
    # 1 top team + 1 top player + 1 mid + 1 mid + 1 low team + 1 low player + ...
    targets = [
        ("team", "France"), ("player", "Kylian Mbappé"),
        ("team", "Norway"), ("player", "Pedri"),
        ("team", "Mexico"), ("player", "Bukayo Saka"),
        ("team", "Iran"),
    ]
    for kind, name in targets:
        if kind == "team":
            tid = team_id(name)
            price = teams_by_slug[tid]["basePrice"]
            if remaining >= price and len(c12) < 20:
                c12.append(pick_id("team", tid))
                remaining -= price
        else:
            pid = player_id_by_name(name)
            if pid:
                price = players_by_id[pid]["basePrice"]
                if remaining >= price and len(c12) < 20:
                    c12.append(pick_id("player", pid))
                    remaining -= price
    # Fill leftover with cheap teams + players
    for t in sorted(teams, key=lambda x: x["basePrice"]):
        if remaining >= t["basePrice"] and pick_id("team", t["id"]) not in c12 and len(c12) < 20:
            c12.append(pick_id("team", t["id"]))
            remaining -= t["basePrice"]
        if remaining == 0: break
    contestants.append({"name": "12. Diversifier (one per band)", "picks": c12})

    # 13. Mid Mix — mid teams + mid players
    c13 = []
    remaining = 100
    mid_teams = sorted([t for t in teams if 8 <= t["basePrice"] <= 14], key=lambda t: -t["basePrice"])[:3]
    for t in mid_teams:
        if remaining >= t["basePrice"]:
            c13.append(pick_id("team", t["id"])); remaining -= t["basePrice"]
    mid_players = sorted([p for p in players if 8 <= p["basePrice"] <= 14], key=lambda p: -p["basePrice"])
    for p in mid_players:
        if remaining >= p["basePrice"] and len(c13) < 20:
            c13.append(pick_id("player", p["id"]))
            remaining -= p["basePrice"]
    contestants.append({"name": "13. Mid teams + mid players", "picks": c13})

    # 14. Cinderella + Underdog Stars
    c14 = []
    remaining = 100
    cind_teams_names = ["Morocco", "Senegal", "Japan", "Mexico", "Croatia"]
    for name in cind_teams_names:
        try:
            tid = team_id(name)
            t = teams_by_slug[tid]
            if remaining >= t["basePrice"]:
                c14.append(pick_id("team", tid)); remaining -= t["basePrice"]
        except StopIteration: pass
    cind_player_names = ["Achraf Hakimi", "Heung-Min Son", "Heung-min Son", "Luka Modrić",
                          "Yassine Bounou", "Édouard Mendy", "Edouard Mendy"]
    for name in cind_player_names:
        pid = player_id_by_name(name)
        if not pid: continue
        if pid in [p[1] for p in c14]: continue
        price = players_by_id[pid]["basePrice"]
        if remaining >= price and len(c14) < 20:
            c14.append(pick_id("player", pid))
            remaining -= price
    contestants.append({"name": "14. Cinderella teams + Underdog stars", "picks": c14})

    # 15. Anchored Balance: champion + 4-5 stars + minimal fill
    # Designed to hit exactly $100 in ~7 picks
    c15 = [pick_id("team", team_id("Argentina"))]  # $30
    remaining = 70
    for name in ["Jude Bellingham", "Harry Kane", "Bukayo Saka", "Achraf Hakimi"]:
        pid = player_id_by_name(name)
        if pid and remaining >= players_by_id[pid]["basePrice"]:
            c15.append(pick_id("player", pid))
            remaining -= players_by_id[pid]["basePrice"]
    # Top up to $100 with whatever fits — use _top_up later
    contestants.append({"name": "15. Anchored balance (champ + 4 stars)", "picks": c15})

    # Top up any roster that has unspent budget (some strategies hit price
    # ceilings naturally, e.g., the cheap-team spammer runs out of teams)
    for c in contestants:
        used = set(c["picks"])
        c["picks"] = _top_up_to_100(c["picks"], teams, players, used)

    return contestants


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--controlled", action="store_true",
                        help="Use the 6 controlled archetypes instead of 15")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print(f"Loading data...")
    teams, players = load_seed()
    matches = load_match_schedule()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)

    print(f"Loaded {len(teams)} teams, {len(players)} players, {len(matches)} matches.")

    contestants = (build_controlled_contestants(teams, players)
                    if args.controlled else build_contestants(teams, players))
    print(f"\nBuilt {len(contestants)} contestants:")
    for c in contestants:
        cost = 0
        for kind, id_ in c["picks"]:
            asset = by_slug.get(id_) if kind == "team" else next((p for p in players if p["id"] == id_), None)
            if asset:
                cost += asset["basePrice"]
        c["total_cost"] = cost
        c["total_picks"] = len(c["picks"])
        print(f"  {c['name']:<50} {c['total_picks']:>2} picks  ${cost}")

    # Build lookup for asset scoring
    teams_by_slug_dict = {t["id"]: t for t in teams}
    players_by_id_dict = {p["id"]: p for p in players}

    print(f"\nRunning {args.runs} simulations...")
    t0 = time.time()

    contestant_scores = [[] for _ in contestants]
    champion_counts = defaultdict(int)
    # For per-tier ROI: accumulate per-asset point totals across sims
    team_points_acc   = defaultdict(list)   # team_id   → [pts per sim]
    player_points_acc = defaultdict(list)   # player_id → [pts per sim]

    for run in range(args.runs):
        if run % 100 == 0 and run > 0:
            elapsed = time.time() - t0
            eta = elapsed * (args.runs - run) / run
            print(f"  [{run}/{args.runs}] elapsed {elapsed:.0f}s, eta {eta:.0f}s")

        result = run_tournament(teams, matches, by_fdid, players_by_team)
        team_pts, player_pts = score_asset_points(
            teams, result["team_stats"], players_by_team,
            result["player_goals"], result["player_assists"],
            result["player_wins_played"], result["player_cs_played"],
        )
        if result["champion_id"]:
            champion_counts[result["champion_id"]] += 1

        # Per-asset accumulation
        for tid, pts in team_pts.items():
            team_points_acc[tid].append(pts)
        for pid, pts in player_pts.items():
            player_points_acc[pid].append(pts)

        for i, c in enumerate(contestants):
            total = 0
            for kind, id_ in c["picks"]:
                if kind == "team":
                    total += team_pts.get(id_, 0)
                else:
                    total += player_pts.get(id_, 0)
            contestant_scores[i].append(total)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s\n")

    # ---- Output ----
    # Win counts per contestant
    win_counts = [0] * len(contestants)
    for run in range(args.runs):
        scores = [contestant_scores[i][run] for i in range(len(contestants))]
        winner = max(range(len(scores)), key=lambda i: scores[i])
        win_counts[winner] += 1

    print(f"\n{'='*100}")
    print(f"  RESULTS — 15 contestants × {args.runs} simulations")
    print(f"{'='*100}")
    print(f"\n  {'#':<3}{'Contestant':<48}{'mean':>6}{'med':>6}{'p25':>6}{'p75':>6}{'min':>5}{'max':>5}{'win%':>6}")
    # Sort by mean
    ranked = sorted(range(len(contestants)), key=lambda i: -statistics.mean(contestant_scores[i]))
    for rank, i in enumerate(ranked, 1):
        scores = contestant_scores[i]
        scores_sorted = sorted(scores)
        n = len(scores)
        c = contestants[i]
        mean = statistics.mean(scores)
        med = statistics.median(scores)
        p25 = scores_sorted[n // 4]
        p75 = scores_sorted[(3 * n) // 4]
        wins_pct = 100 * win_counts[i] / args.runs
        name = c["name"][:46]
        print(f"  {rank:<3}{name:<48}{mean:>6.1f}{med:>6.0f}{p25:>6.0f}{p75:>6.0f}{min(scores):>5.0f}{max(scores):>5.0f}{wins_pct:>5.1f}%")

    # Champion distribution (top 10)
    print(f"\n  Top 10 most-frequent champions across {args.runs} sims:")
    top_champs = sorted(champion_counts.items(), key=lambda x: -x[1])[:10]
    for cid, cnt in top_champs:
        team = by_slug.get(cid, {})
        print(f"    {team.get('name', cid):<22}  ${team.get('basePrice'):>2}  {cnt:>4} wins  ({100*cnt/args.runs:.1f}%)")

    # ---- Per-tier ROI (the user's question d + e) -----------------------
    print(f"\n\n{'='*80}")
    print("  PER-PRICE-TIER ROI (mean points across all sims, by exact price)")
    print(f"{'='*80}")

    def _tier_report(label, points_acc, asset_lookup):
        print(f"\n  {label}:")
        print(f"    {'price':>6} {'n_assets':>10} {'mean':>7} {'p25':>6} {'p75':>6} {'pts/$':>7} {'break%':>7}")
        # Group assets by price
        by_price = defaultdict(list)
        for aid, pts_list in points_acc.items():
            asset = asset_lookup.get(aid)
            if not asset: continue
            for pts in pts_list:
                by_price[asset["basePrice"]].append(pts)
        for price in sorted(by_price.keys(), reverse=True):
            pts_list = by_price[price]
            n_unique = sum(1 for aid in points_acc
                            if asset_lookup.get(aid, {}).get("basePrice") == price)
            mean = sum(pts_list) / len(pts_list)
            s = sorted(pts_list)
            p25 = s[len(s) // 4]
            p75 = s[(3 * len(s)) // 4]
            roi = mean / price
            breakeven = 100 * sum(1 for p in pts_list if p >= price) / len(pts_list)
            print(f"    ${price:>5} {n_unique:>10} {mean:>7.1f} {p25:>6.0f} {p75:>6.0f} {roi:>7.2f} {breakeven:>6.0f}%")

    _tier_report("TEAMS",   team_points_acc,   teams_by_slug_dict)
    _tier_report("PLAYERS", player_points_acc, players_by_id_dict)

    # ---- Aggregate by broader strategy archetype (question a/b/c) -----
    if args.controlled:
        print(f"\n\n{'='*80}")
        print("  STRATEGY HEAD-TO-HEAD (controlled archetypes)")
        print(f"{'='*80}")
        team_means   = [statistics.mean(contestant_scores[i]) for i in range(3)]
        player_means = [statistics.mean(contestant_scores[i]) for i in range(3, 6)]
        print(f"\n  (a) Teams vs Players (avg across distributions):")
        print(f"        All TEAMS  : {statistics.mean(team_means):>6.1f}")
        print(f"        All PLAYERS: {statistics.mean(player_means):>6.1f}")
        diff = statistics.mean(team_means) - statistics.mean(player_means)
        winner = "TEAMS" if diff > 0 else "PLAYERS"
        print(f"        Winner: {winner} by {abs(diff):.1f} pts ({100*abs(diff)/min(statistics.mean(team_means), statistics.mean(player_means)):.0f}%)")
        print(f"\n  (b) TEAM distribution:    TOP={team_means[0]:.1f}  MID={team_means[1]:.1f}  CINDERELLA={team_means[2]:.1f}")
        print(f"  (c) PLAYER distribution:  TOP={player_means[0]:.1f}  MID={player_means[1]:.1f}  CINDERELLA={player_means[2]:.1f}")


if __name__ == "__main__":
    main()
