"""Compare uncapped vs 40% floor MV trajectories per team.

The 40% floor rule: MV at round R+1 cannot be lower than 0.60 × MV at
round R. Applied sequentially as a team progresses. Tests what this
does to:
  - The transfer-market MV at each stage
  - The buyer's effective ROI in the transfer market
  - The "is Spain a better buy in transfer than at draft?" issue

Run from project root:
  ./venv/bin/python scripts/analyze_mv_floor.py --runs 2000
"""
from __future__ import annotations

import argparse
import math
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
)

TARGET_ROI = 2.0
VIG_BUY    = 1.10
FLOOR_RATE = 0.60   # MV[R+1] >= 0.60 × MV[R]  →  no drop > 40%


def round_half_up(x):
    return math.floor(x + 0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--focus", default="spain,france,england,germany,morocco,cape-verde",
                    help="comma-separated team slugs")
    args = ap.parse_args()

    random.seed(args.seed)
    teams, players = load_seed()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)

    print(f"Running {args.runs} sims...", file=sys.stderr)
    matches = load_match_schedule()

    team_runs = defaultdict(list)
    for i in range(args.runs):
        if i and i % 500 == 0:
            print(f"  {i}/{args.runs}", file=sys.stderr)
        result = run_tournament(teams, matches, by_fdid, players_by_team)
        team_pts, _ = score_asset_points(
            teams, result["team_stats"], players_by_team,
            result["player_goals"], result["player_assists"],
            result["player_wins_played"], result["player_cs_played"],
        )
        for tid, stats in result["team_stats"].items():
            team_runs[tid].append({
                "fr":    stats["final_round"],
                "pts":   team_pts[tid],
                "wins":  stats["wins"],
                "draws": stats["draws"],
            })

    def cum_bonuses(R_idx):
        return sum(BONUS_BY_ROUND[r] for r in ADVANCEMENT_ORDER[1:R_idx+1] if r in BONUS_BY_ROUND)

    print("\n" + "="*138)
    print("TRANSFER MARKET: UNCAPPED vs 40% FLOOR (MV cannot drop > 40% between rounds)")
    print("="*138)
    print(f"\n{'40% floor rule means MV[next round] >= 0.60 × MV[current round]. Applied iteratively as team progresses.'}")

    for slug in [t.strip() for t in args.focus.split(",")]:
        team = by_slug.get(slug)
        if not team:
            print(f"  {slug}: not found"); continue
        runs = team_runs[slug]
        n = len(runs)
        if not n: continue
        price = team["basePrice"]
        e_total = mean(r["pts"] for r in runs)

        # Compute per-stage uncapped MVs first
        stages = []
        # draft row
        mv_uncapped = round_half_up(e_total / TARGET_ROI)
        stages.append({
            "stage": "draft", "p_reach": 1.0,
            "future": e_total, "mv_unc": mv_uncapped, "mv_cap": mv_uncapped,
        })

        for R in ["R32","R16","QF","SF","F"]:
            R_idx = ADVANCEMENT_ORDER.index(R)
            reached = [r for r in runs if ADVANCEMENT_ORDER.index(r["fr"]) >= R_idx]
            if not reached:
                stages.append({"stage": R, "p_reach": 0, "future": 0, "mv_unc": 0, "mv_cap": 0})
                continue
            reach_p = len(reached) / n
            ko_wins_to_R = max(0, R_idx - 1)
            mean_wins = mean(r["wins"] for r in reached)
            mean_draws = mean(r["draws"] for r in reached)
            mean_group_wins = max(0, mean_wins - ko_wins_to_R)
            mean_group_pts = mean_group_wins * WEIGHTS["team_win"] + mean_draws * WEIGHTS["team_draw"]
            accrued = mean_group_pts + ko_wins_to_R * WEIGHTS["team_win"] + cum_bonuses(R_idx)
            mean_total = mean(r["pts"] for r in reached)
            future = max(0, mean_total - accrued)
            mv_unc = round_half_up(future / TARGET_ROI)
            stages.append({"stage": R, "p_reach": reach_p, "future": future, "mv_unc": mv_unc, "mv_cap": None})

        # Now apply 40% floor sequentially
        for i in range(1, len(stages)):
            prev_cap = stages[i-1]["mv_cap"]
            floor_value = round_half_up(prev_cap * FLOOR_RATE) if prev_cap else 0
            stages[i]["mv_cap"] = max(stages[i]["mv_unc"], floor_value)

        print(f"\n{team['name']} (draft price ${price}, E[total]={e_total:.1f})")
        print("-"*138)
        print(f"  {'Stage':<8} {'P(reach)':>10}  {'Future':>8}  "
              f"{'Uncapped MV':>12}  {'Capped MV':>10}  "
              f"{'Uncapped Buy':>14}  {'Capped Buy':>12}  "
              f"{'Uncap ROI':>12}  {'Cap ROI':>10}  {'Buyer pays':>12}")
        print("-"*138)
        for s in stages:
            stage = s["stage"]
            if s["p_reach"] == 0:
                print(f"  {stage:<8} {'0%':>10}")
                continue
            unc_buy = max(s["mv_unc"]+1, round_half_up(s["mv_unc"]*VIG_BUY)) if s["mv_unc"] > 0 else 0
            cap_buy = max(s["mv_cap"]+1, round_half_up(s["mv_cap"]*VIG_BUY)) if s["mv_cap"] > 0 else 0
            roi_unc = s["future"] / unc_buy if unc_buy else 0
            roi_cap = s["future"] / cap_buy if cap_buy else 0
            cap_overprice = cap_buy - unc_buy
            mark = ""
            if cap_overprice > 0:
                mark = f" (+${cap_overprice})"
            print(f"  {stage:<8} {s['p_reach']*100:>9.0f}%  {s['future']:>8.1f}  "
                  f"${s['mv_unc']:>10}  ${s['mv_cap']:>8}  "
                  f"${unc_buy:>12}  ${cap_buy:>10}  "
                  f"{roi_unc:>12.2f}  {roi_cap:>10.2f}  {mark:>12}")

    print()
    print("="*138)
    print("KEY METRIC - Buyer ROI in transfer market:")
    print("  - 'future' / 'buy price' = pts/$ a transfer buyer expects to earn")
    print("  - Game average is ~1.6 pts/$. Anything above is a good buy, below is a bad buy.")
    print("  - When capped ROI drops well below 1.6, buyers will avoid transfers (kills the market).")
    print("  - The 40% floor mostly pinches at R32 (the biggest MV drop) and rarely later.")


if __name__ == "__main__":
    main()
