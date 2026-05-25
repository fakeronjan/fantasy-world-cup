"""Per-team expected-value + fair-price analysis.

Runs the simulator N times. For each team, tracks:
  - P(reach round R) for R in {R32, R16, QF, SF, F, W}
  - E[total points | unconditional]
  - E[total points | reach round R]
  - E[remaining future points | at round R = (E[total | reach R+1]) since 'at round R' means they just earned the R bonus]

Translates to "fair MV at 2 pts/$" so we can compare against the current
draft price and the transfer-market price at each stage.

Run from project root:
  ./venv/bin/python scripts/analyze_team_ev.py --runs 5000
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_2026 import (
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
    load_seed, build_team_indexes, load_match_schedule,
    run_tournament, score_asset_points,
)
from collections import defaultdict as _dd

TARGET_ROI = 2.0   # fair MV anchor - same as reprice.py


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--focus", default="france,spain,england,argentina,brazil,portugal,germany,morocco,cape-verde,canada",
                    help="comma-separated team slugs to deep-dive")
    args = ap.parse_args()

    random.seed(args.seed)

    teams, players = load_seed()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = _dd(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)

    print(f"Loading match schedule…", file=sys.stderr)
    matches = load_match_schedule()
    print(f"Running {args.runs} simulations…", file=sys.stderr)

    # Per team per run: (final_round, total_pts, total_wins, total_draws)
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
                "fr":     stats["final_round"],
                "pts":    team_pts[tid],
                "wins":   stats["wins"],
                "draws":  stats["draws"],
            })

    print("\n" + "=" * 130, file=sys.stderr)

    focus_teams = [t.strip() for t in args.focus.split(",")]

    # Helpers for the per-stage calculation
    # Cumulative bonuses earned by round R_idx (inclusive)
    def cumulative_bonuses(R_idx):
        return sum(BONUS_BY_ROUND[r]
                   for r in ADVANCEMENT_ORDER[1:R_idx + 1]
                   if r in BONUS_BY_ROUND)

    # Max possible total points from "currently at round R" onwards. This
    # assumes the team wins every subsequent KO match → champion path.
    # At R32 (just advanced), potential future = 5 KO wins × 3 + bonuses[R16..W]
    def potential_future(R_idx):
        rounds_to_play = ADVANCEMENT_ORDER[R_idx + 1:]  # R32_idx=1 → R16, QF, SF, F, W
        max_wins = sum(1 for r in rounds_to_play if r in BONUS_BY_ROUND)
        future_bonuses = sum(BONUS_BY_ROUND[r] for r in rounds_to_play if r in BONUS_BY_ROUND)
        return max_wins * WEIGHTS["team_win"] + future_bonuses

    for slug in focus_teams:
        team = by_slug.get(slug)
        if not team:
            print(f"  {slug}: not found")
            continue
        runs = team_runs[slug]
        n = len(runs)
        if not n:
            continue
        price = team["basePrice"]
        e_total_uncond = sum(r["pts"] for r in runs) / n
        roi_uncond = e_total_uncond / price if price else 0

        print(f"\n{team['name']} (paid ${price}, unconditional E[total]={e_total_uncond:.1f}, ROI={roi_uncond:.2f})")
        print("-" * 130)
        print(f"  {'Stage':<8} {'P(reach)':>10}  {'Accrued (mean)':>15}  {'Potential future':>17}  {'Expected future':>17}  {'Fair MV':>9}")
        print(f"  {'':<8} {'':>10}  {'group + ko':>15}  {'(if win out)':>17}  {'(EV remaining)':>17}  {'= EF/2':>9}")
        print("-" * 130)

        # "Draft" row - pre-tournament view
        print(f"  {'draft':<8} {'100%':>10}  {0:>15.1f}  {potential_future(0):>17}  {e_total_uncond:>17.1f}  ${round(e_total_uncond/TARGET_ROI):>5}")

        for R in ["R32", "R16", "QF", "SF", "F", "W"]:
            R_idx = ADVANCEMENT_ORDER.index(R)
            reached = [r for r in runs if ADVANCEMENT_ORDER.index(r["fr"]) >= R_idx]
            reach_n = len(reached)
            if reach_n == 0:
                print(f"  {R:<8} {'0%':>10}  {' - ':>15}  {potential_future(R_idx):>17}  {' - ':>17}  {' - ':>9}")
                continue
            reach_p = reach_n / n
            # Group_pts derivation: KO wins to reach R = R_idx - 1 (we win each KO match
            # to advance through R-1; at R32 we just advanced from group, no KO win yet).
            ko_wins_to_R = max(0, R_idx - 1)
            mean_total_wins = sum(r["wins"] for r in reached) / reach_n
            mean_total_draws = sum(r["draws"] for r in reached) / reach_n
            mean_group_wins = max(0, mean_total_wins - ko_wins_to_R)
            # KO has no draws, so all draws are group draws
            mean_group_pts = mean_group_wins * WEIGHTS["team_win"] + mean_total_draws * WEIGHTS["team_draw"]
            accrued_at_R = mean_group_pts + ko_wins_to_R * WEIGHTS["team_win"] + cumulative_bonuses(R_idx)
            mean_total_at_R = sum(r["pts"] for r in reached) / reach_n
            expected_future_at_R = max(0, mean_total_at_R - accrued_at_R)
            fair_mv = round(expected_future_at_R / TARGET_ROI)
            pot_fut = potential_future(R_idx)
            print(f"  {R:<8} {reach_p*100:>9.0f}%  {accrued_at_R:>15.1f}  {pot_fut:>17}  {expected_future_at_R:>17.1f}  ${fair_mv:>5}")

    print()
    print("=" * 130)
    print("Column definitions:")
    print("  Accrued        - mean points already banked at the moment they reach this round")
    print("                   (group wins/draws + KO wins to get here + cumulative bonuses)")
    print("  Potential      - MAX future points if they win every remaining match (champion path)")
    print("  Expected       - mean future points actually realized from this round onward (across sims)")
    print("  Fair MV        - Expected future ÷ 2 (the transfer-market 'fair' price at TARGET_ROI=2)")


if __name__ == "__main__":
    main()
