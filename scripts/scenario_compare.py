"""Compare pricing scenarios side-by-side via Monte Carlo simulation.

Defines several "what if we re-priced like this?" scenarios, runs each
through the simulator, and prints per-tier ROI + Teams-vs-Players balance.
No production data is touched.

Run from project root:
  ./venv/bin/python scripts/scenario_compare.py --runs 500
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_2026 import (
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
    load_seed, build_team_indexes, load_match_schedule,
    run_tournament, score_asset_points,
    build_controlled_contestants,
)


# Scenario definitions. Each is a function (teams, players) → (new_teams, new_players).
# "current" leaves prices untouched.

def scenario_current(teams, players):
    return copy.deepcopy(teams), copy.deepcopy(players)

# Option A - LIGHT FLATTEN
# Top teams come down modestly; top players track similarly.
# Goal: top-tier ROI ~1.7, modest shift toward "$1 → 2 pts" without
# making top picks a no-brainer.
def scenario_A(teams, players):
    t = copy.deepcopy(teams); p = copy.deepcopy(players)
    for x in t:
        if x["basePrice"] == 12: x["basePrice"] = 10
        elif x["basePrice"] == 9: x["basePrice"] = 8
        # mid and cheap untouched
    for x in p:
        if x["basePrice"] == 9: x["basePrice"] = 8
        elif x["basePrice"] == 8: x["basePrice"] = 7
        elif x["basePrice"] == 7: x["basePrice"] = 6
        # $6 and below untouched
    return t, p

# Option B - MODERATE FLATTEN
# Bigger top-tier compression; mid stays.
def scenario_B(teams, players):
    t = copy.deepcopy(teams); p = copy.deepcopy(players)
    for x in t:
        if x["basePrice"] == 12: x["basePrice"] = 9
        elif x["basePrice"] == 9: x["basePrice"] = 7
        elif x["basePrice"] == 6 and x.get("tier", 0) == 2: x["basePrice"] = 5
    for x in p:
        if x["basePrice"] == 9: x["basePrice"] = 7
        elif x["basePrice"] == 8: x["basePrice"] = 6
        elif x["basePrice"] == 7: x["basePrice"] = 6
        # $6 and below untouched
    return t, p

# Option C - STRICT 2 pts/$ at draft (top heavy compression)
def scenario_C(teams, players):
    t = copy.deepcopy(teams); p = copy.deepcopy(players)
    for x in t:
        if x["basePrice"] == 12: x["basePrice"] = 8
        elif x["basePrice"] == 9: x["basePrice"] = 7
        elif x["basePrice"] == 6: x["basePrice"] = 5
    for x in p:
        if x["basePrice"] == 9: x["basePrice"] = 7
        elif x["basePrice"] == 8: x["basePrice"] = 6
        elif x["basePrice"] == 7: x["basePrice"] = 6
        elif x["basePrice"] == 6: x["basePrice"] = 5
    return t, p


SCENARIOS = [
    ("CURRENT",    scenario_current),
    ("A (light)",  scenario_A),
    ("B (moderate)", scenario_B),
    ("C (strict)", scenario_C),
]


def run_scenario(name, teams, players, matches, runs, seed):
    random.seed(seed)
    _, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)

    # Track per-asset points across all runs
    team_pts_runs = defaultdict(list)
    player_pts_runs = defaultdict(list)

    # Track controlled-strategy roster scores for the Teams vs Players gap
    contestants = build_controlled_contestants(teams, players)
    contestant_scores = defaultdict(list)

    for _ in range(runs):
        result = run_tournament(teams, matches, by_fdid, players_by_team)
        team_pts, player_pts = score_asset_points(
            teams, result["team_stats"], players_by_team,
            result["player_goals"], result["player_assists"],
            result["player_wins_played"], result["player_cs_played"],
        )
        for tid, pts in team_pts.items():
            team_pts_runs[tid].append(pts)
        for pid, pts in player_pts.items():
            player_pts_runs[pid].append(pts)

        for c in contestants:
            score = 0
            for kind, asset_id in c["picks"]:
                score += team_pts.get(asset_id, 0) if kind == "team" else player_pts.get(asset_id, 0)
            contestant_scores[c["name"]].append(score)

    return {
        "team_pts_runs": team_pts_runs,
        "player_pts_runs": player_pts_runs,
        "contestant_scores": contestant_scores,
        "teams": teams,
        "players": players,
    }


def print_scenario_summary(name, scenarios_data):
    print(f"\n{'='*100}")
    print(f"  SCENARIO: {name}")
    print(f"{'='*100}")

    data = scenarios_data[name]
    teams = data["teams"]
    players = data["players"]

    # Per-tier ROI for teams
    print("\n  TEAMS (per-price ROI):")
    print(f"    {'price':>5} {'n_assets':>9} {'E[pts]':>8} {'ROI':>6}")
    by_price = defaultdict(list)
    for t in teams:
        avg_pts = mean(data["team_pts_runs"][t["id"]]) if data["team_pts_runs"][t["id"]] else 0
        by_price[t["basePrice"]].append(avg_pts)
    for price in sorted(by_price.keys(), reverse=True):
        pts_list = by_price[price]
        avg_pts = mean(pts_list)
        roi = avg_pts / price if price else 0
        print(f"    ${price:>4} {len(pts_list):>9} {avg_pts:>8.1f} {roi:>6.2f}")

    # Per-tier ROI for players
    print("\n  PLAYERS (per-price ROI):")
    print(f"    {'price':>5} {'n_assets':>9} {'E[pts]':>8} {'ROI':>6}")
    by_price = defaultdict(list)
    for p in players:
        avg_pts = mean(data["player_pts_runs"][p["id"]]) if data["player_pts_runs"][p["id"]] else 0
        by_price[p["basePrice"]].append(avg_pts)
    for price in sorted(by_price.keys(), reverse=True):
        pts_list = by_price[price]
        avg_pts = mean(pts_list)
        roi = avg_pts / price if price else 0
        print(f"    ${price:>4} {len(pts_list):>9} {avg_pts:>8.1f} {roi:>6.2f}")

    # Strategy head-to-head
    print("\n  STRATEGY MEANS (controlled archetypes):")
    cs = data["contestant_scores"]
    by_pattern = {}
    for label, scores in cs.items():
        if not scores: continue
        by_pattern[label] = mean(scores)
    # Aggregate Teams vs Players
    team_names = [n for n in by_pattern if "Team" in n or "team" in n.lower()]
    player_names = [n for n in by_pattern if "Player" in n or "player" in n.lower()]
    if team_names: print(f"    All TEAMS avg:   {mean(by_pattern[n] for n in team_names):>6.1f}")
    if player_names: print(f"    All PLAYERS avg: {mean(by_pattern[n] for n in player_names):>6.1f}")
    # Show each archetype
    for label in sorted(by_pattern.keys()):
        score = by_pattern[label]
        budget = 60
        roi = score / budget
        print(f"    {label:<55} {score:>6.1f} ({roi:.2f} pts/$)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    teams_base, players_base = load_seed()
    matches = load_match_schedule()

    scenarios_data = {}
    for name, fn in SCENARIOS:
        teams, players = fn(teams_base, players_base)
        print(f"\n[Running {name} - {args.runs} sims]", file=sys.stderr)
        scenarios_data[name] = run_scenario(name, teams, players, matches, args.runs, args.seed)

    for name, _ in SCENARIOS:
        print_scenario_summary(name, scenarios_data)


if __name__ == "__main__":
    main()
