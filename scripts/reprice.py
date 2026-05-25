"""Forward-looking market reprice for the transfer market.

Runs after each round transition (group → R32 → R16 → QF → SF → F). For
every surviving asset, warm-starts the Monte Carlo from the current
bracket state, runs N trials, and computes:

  marketValue = mean(future_points_owning_this_asset) / TARGET_ROI
  buyPrice    = ceil(marketValue * 1.10)         # 10% premium over fair
  sellPrice   = floor(marketValue * 0.90)        # 10% discount

Eliminated assets settle at:
  marketValue = 0
  sellPrice   = floor(last_marketValue * 0.40)   # 40% liquidation refund
  buyPrice    = N/A (cannot buy eliminated assets)

This is the *pricing engine* only — Firestore I/O and UI wiring come in
the next pass. Run --dry-run to see prices for a hypothetical state.

Usage:
  ./venv/bin/python scripts/reprice.py --from-round R32 --runs 1000

Roadmap:
  next: read live bracket state from Firestore + persist prices back
  next: wire into transfer.html so users see buy/sell separately
  next: trigger automatically from ingest_results.py when a round ends
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_2026 import (
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
    simulate_match, _record_match, _winner_loser,
    load_seed, build_team_indexes,
)

# Forward-looking pricing constants
TARGET_ROI       = 2.0    # target points per dollar (cap-anchored)
VIG_BUY          = 1.10   # buy at 10% premium
VIG_SELL         = 0.90   # sell at 10% discount
ELIM_REFUND_RATE = 0.40   # 40% liquidation on eliminated picks


def simulate_remaining(advancers, start_round, players_by_team):
    """Simulate from `start_round` onward, treating `advancers` as the
    field at that round. Returns the same shape as run_tournament() but
    accumulators only capture FUTURE events (group-stage skipped)."""
    team_stats = {t["id"]: dict(
        wins=0, draws=0, losses=0,
        goals_for=0, goals_against=0,
        clean_sheets=0,
        final_round=start_round,
        group_pts=0, group_gd=0, group_gf=0,
    ) for t in advancers}
    player_goals       = defaultdict(int)
    player_assists     = defaultdict(int)
    player_wins_played = defaultdict(int)
    player_cs_played   = defaultdict(int)

    current = list(advancers)
    stage_idx = ADVANCEMENT_ORDER.index(start_round)
    remaining_stages = ADVANCEMENT_ORDER[stage_idx:]

    semis_losers = []
    for i in range(len(remaining_stages) - 1):
        round_label = remaining_stages[i]
        next_label  = remaining_stages[i + 1]
        next_round = []
        for j in range(0, len(current), 2):
            if j + 1 >= len(current):
                next_round.append(current[j]); continue
            ta, tb = current[j], current[j + 1]
            ga, gb, pen = simulate_match(ta, tb, ko=True)
            winner, loser = _winner_loser(ta, tb, ga, gb, pen)
            _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                          player_wins_played, player_cs_played,
                          players_by_team, is_group=False)
            team_stats[winner["id"]]["final_round"] = next_label
            if round_label == "SF":
                semis_losers.append(loser)
            next_round.append(winner)
        current = next_round

    # 3rd-place match (real WC has one — counts for player stats)
    if len(semis_losers) == 2:
        ta, tb = semis_losers
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=False)

    return {
        "team_stats":         team_stats,
        "player_goals":       dict(player_goals),
        "player_assists":     dict(player_assists),
        "player_wins_played": dict(player_wins_played),
        "player_cs_played":   dict(player_cs_played),
    }


def score_future_points(result, advancers, start_round, players_by_team):
    """Compute future-points for each surviving asset.

    Bonuses for `start_round` itself are NOT counted (those were earned
    by advancing into that round, before this transfer window). Only
    bonuses for rounds *strictly after* start_round count as future."""
    team_pts = {}
    start_idx = ADVANCEMENT_ORDER.index(start_round)
    for tid, stats in result["team_stats"].items():
        pts = WEIGHTS["team_win"] * stats["wins"] + WEIGHTS["team_draw"] * stats["draws"]
        fr = stats["final_round"]
        if fr in BONUS_BY_ROUND:
            fr_idx = ADVANCEMENT_ORDER.index(fr)
            for r in ADVANCEMENT_ORDER[start_idx + 1:fr_idx + 1]:
                pts += BONUS_BY_ROUND[r]
        team_pts[tid] = pts

    player_pts = {}
    alive_ids = {t["id"] for t in advancers}
    for tid, ps in players_by_team.items():
        if tid not in alive_ids:
            continue
        for p in ps:
            pos = p.get("position")
            if   pos == "GK":  cs_rate = WEIGHTS["player_clean_sheet_gk"]
            elif pos == "DEF": cs_rate = WEIGHTS["player_clean_sheet_def"]
            else:              cs_rate = WEIGHTS["player_clean_sheet_other"]
            pts = (
                WEIGHTS["player_goal"]       * result["player_goals"].get(p["id"], 0)
                + WEIGHTS["player_assist"]    * result["player_assists"].get(p["id"], 0)
                + WEIGHTS["player_win_share"] * result["player_wins_played"].get(p["id"], 0)
                + cs_rate                     * result["player_cs_played"].get(p["id"], 0)
            )
            player_pts[p["id"]] = pts
    return team_pts, player_pts


def reprice(advancers, start_round, players_by_team, runs=1000, seed=42):
    """Run N trials. Return per-asset price dict:
       {asset_id: {kind, name, meanFuturePoints, marketValue, buyPrice, sellPrice}}."""
    random.seed(seed)

    team_acc   = defaultdict(float)
    player_acc = defaultdict(float)

    for _ in range(runs):
        # Bracket pairings are FIFA-determined in real life. Until we
        # read the actual fixtures, randomize per-trial to get a fair
        # expectation across plausible bracket positions.
        shuffled = list(advancers)
        random.shuffle(shuffled)
        result = simulate_remaining(shuffled, start_round, players_by_team)
        tp, pp = score_future_points(result, shuffled, start_round, players_by_team)
        for k, v in tp.items(): team_acc[k]   += v
        for k, v in pp.items(): player_acc[k] += v

    prices = {}
    alive_ids = {t["id"] for t in advancers}

    for t in advancers:
        mean_fp = team_acc[t["id"]] / runs
        mv = mean_fp / TARGET_ROI
        prices[t["id"]] = {
            "kind": "team",
            "name": t["name"],
            "meanFuturePoints": round(mean_fp, 2),
            "marketValue":  max(1, round(mv)),
            "buyPrice":     max(1, math.ceil(mv * VIG_BUY)),
            "sellPrice":    max(1, math.floor(mv * VIG_SELL)),
        }

    for tid, ps in players_by_team.items():
        if tid not in alive_ids:
            continue
        for p in ps:
            mean_fp = player_acc[p["id"]] / runs
            mv = mean_fp / TARGET_ROI
            prices[p["id"]] = {
                "kind": "player",
                "name": p["name"],
                "team": tid,
                "meanFuturePoints": round(mean_fp, 2),
                "marketValue":  max(1, round(mv)),
                "buyPrice":     max(1, math.ceil(mv * VIG_BUY)),
                "sellPrice":    max(1, math.floor(mv * VIG_SELL)),
            }

    return prices


def hypothetical_advancers(teams, start_round):
    """For dry-runs, pick a plausible field of advancers based on team
    price (proxy for strength). Top N teams advance to each round."""
    sorted_teams = sorted(teams, key=lambda t: -t["basePrice"])
    n_by_round = {"R32": 32, "R16": 16, "QF": 8, "SF": 4, "F": 2, "W": 1}
    return sorted_teams[:n_by_round[start_round]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-round", default="R32",
                    choices=["R32", "R16", "QF", "SF", "F"])
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top", type=int, default=20,
                    help="Show top N priced assets in dry-run output")
    args = ap.parse_args()

    teams, players = load_seed()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)
    print(f"Loaded {len(teams)} teams, {len(players)} players")
    print(f"Warm-start round: {args.from_round}, runs: {args.runs}\n")

    advancers = hypothetical_advancers(teams, args.from_round)
    print(f"Hypothetical {args.from_round} field ({len(advancers)} teams):")
    for t in advancers[:8]:
        print(f"  ${t['basePrice']:>2}  {t['name']}")
    if len(advancers) > 8:
        print(f"  ... ({len(advancers) - 8} more)\n")
    else:
        print()

    prices = reprice(advancers, args.from_round, players_by_team,
                     runs=args.runs, seed=args.seed)

    teams_list   = sorted([(p["marketValue"], pid, p) for pid, p in prices.items()
                           if p["kind"] == "team"], reverse=True)
    players_list = sorted([(p["marketValue"], pid, p) for pid, p in prices.items()
                           if p["kind"] == "player"], reverse=True)

    print(f"=== TOP {args.top} TEAMS (by marketValue) ===")
    print(f"  {'Name':<25} {'E[Pts]':>7}  {'Sell':>5} {'MV':>5} {'Buy':>5}")
    for mv, pid, p in teams_list[:args.top]:
        print(f"  {p['name']:<25} {p['meanFuturePoints']:>7.1f}  ${p['sellPrice']:>3}  ${p['marketValue']:>2}  ${p['buyPrice']:>3}")
    print()

    print(f"=== TOP {args.top} PLAYERS (by marketValue) ===")
    print(f"  {'Name':<25} {'E[Pts]':>7}  {'Sell':>5} {'MV':>5} {'Buy':>5}")
    for mv, pid, p in players_list[:args.top]:
        print(f"  {p['name']:<25} {p['meanFuturePoints']:>7.1f}  ${p['sellPrice']:>3}  ${p['marketValue']:>2}  ${p['buyPrice']:>3}")


if __name__ == "__main__":
    main()
