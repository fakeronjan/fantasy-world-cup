"""Transfer-aware strategy analysis: does capping total buys at 3 (or 5)
prevent the "save up + buy both finalists" exploit, or does it still
dominate?

For each candidate buy cap, runs 4 strategies through the same N
tournament simulations:
  - HOLD: never voluntarily transfers (only auto-sells happen)
  - AGGRESSIVE: 1 buy per window, picks best survivor each round
  - WAR_CHEST: draft cheap, save buys for SF/F windows
  - FINALIST_EXPLOIT: minimal draft, save ALL buys for F window only

Strategies operate with PAST info only (who advanced, who was eliminated,
who scored). They DON'T see future outcomes. The one exception: at the F
window, the 2 finalists are known (the SF winners), so "buy both
finalists" is an information-fair move.

Auto-sells (on elimination) refund 25% of last MV and do NOT count
toward the buy cap. Only voluntary buys count.

Run from project root:
  ./venv/bin/python scripts/analyze_buy_caps.py --runs 500
"""
from __future__ import annotations

import argparse
import copy
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_2026 import (
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
    load_seed, build_team_indexes, load_match_schedule,
    simulate_match, _record_match, _winner_loser, _determine_advancers,
    pick_lineup, attribute_goals, attribute_assists,
)

TARGET_ROI       = 2.0
VIG_BUY          = 1.10
VIG_SELL         = 0.90
ELIM_REFUND_RATE = 0.25
BUDGET           = 60
ROSTER_CAP       = 12

KO_WINDOWS = ["R32", "R16", "QF", "SF", "F"]   # transfer windows (post-group, post-R32, etc.)


def rhu(x):  # round-half-up
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Tournament sim with per-round event tracking
# ---------------------------------------------------------------------------

def run_tournament_tracked(teams, matches, by_fdid, players_by_team):
    """Same as run_tournament but additionally returns per-round earnings:
       team_round_pts[team_id][round_label] = pts earned during that round
       (group, R32, R16, QF, SF, F).
       Also returns final_round per team.
    """
    team_stats = {t["id"]: dict(
        wins=0, draws=0, losses=0,
        goals_for=0, goals_against=0,
        clean_sheets=0,
        final_round="group",
        group_pts=0, group_gd=0, group_gf=0,
    ) for t in teams}
    player_goals = defaultdict(int)
    player_assists = defaultdict(int)
    player_wins_played = defaultdict(int)
    player_cs_played = defaultdict(int)
    team_group = {}

    # Per-round earnings buckets
    team_round_pts = {t["id"]: defaultdict(int) for t in teams}
    # player rounds we'll handle similarly if needed; for buy-cap analysis,
    # team points are the main driver since the F bonus is the biggest swing

    # ---- group stage ----
    group_matches = [m for m in matches if (m.get("stage") or "GROUP_STAGE") == "GROUP_STAGE"]
    for m in group_matches:
        ta_fd = (m.get("homeTeam") or {}).get("id")
        tb_fd = (m.get("awayTeam") or {}).get("id")
        ta = by_fdid.get(ta_fd); tb = by_fdid.get(tb_fd)
        if not (ta and tb): continue
        team_group[ta["id"]] = m.get("group")
        team_group[tb["id"]] = m.get("group")
        ga, gb, _ = simulate_match(ta, tb, ko=False)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=True)
        # Attribute win/draw pts to "group" bucket
        if ga > gb:
            team_round_pts[ta["id"]]["group"] += WEIGHTS["team_win"]
        elif gb > ga:
            team_round_pts[tb["id"]]["group"] += WEIGHTS["team_win"]
        else:
            team_round_pts[ta["id"]]["group"] += WEIGHTS["team_draw"]
            team_round_pts[tb["id"]]["group"] += WEIGHTS["team_draw"]

    # ---- advancers ----
    by_slug = {t["id"]: t for t in teams}
    advancer_ids = _determine_advancers(team_stats, team_group)
    advancers = [by_slug[tid] for tid in advancer_ids if tid in by_slug]
    for t in advancers:
        team_stats[t["id"]]["final_round"] = "R32"
        team_round_pts[t["id"]]["R32"] += BONUS_BY_ROUND["R32"]  # advancement bonus

    # ---- knockouts ----
    current = list(advancers)
    knockout_pairs = [("R32", "R16"), ("R16", "QF"), ("QF", "SF"), ("SF", "F"), ("F", "W")]
    finalists = []  # captured at SF resolution
    for round_label, next_label in knockout_pairs:
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
            # Win pts go to the round's bucket (the round of the match)
            team_round_pts[winner["id"]][round_label] += WEIGHTS["team_win"]
            team_stats[winner["id"]]["final_round"] = next_label
            # Advancement bonus to the NEXT round goes into next_label bucket
            if next_label in BONUS_BY_ROUND:
                team_round_pts[winner["id"]][next_label] += BONUS_BY_ROUND[next_label]
            next_round.append(winner)
        current = next_round
        if round_label == "SF":
            finalists = [t["id"] for t in current]
    # 3rd-place — simulate but doesn't affect bonuses we care about for caps

    return {
        "team_stats":     team_stats,
        "team_round_pts": team_round_pts,
        "finalists":      finalists,
    }


# ---------------------------------------------------------------------------
# Per-team E[remaining points] at each round (used by pricing)
# Computed once across many sims; reused for buy/sell price calculation.
# ---------------------------------------------------------------------------

def compute_mv_table(teams, matches, by_fdid, players_by_team, runs=300):
    """Returns {team_id: {round_about_to_start: marketValue}}
    where marketValue = E[future_pts | reached this round] / TARGET_ROI."""
    # For each team, track per-run: (final_round_reached, total_points_after_round_R)
    # Specifically we want E[remaining at round R] = E[total | reach R] - accrued_to_R

    runs_data = defaultdict(list)  # team_id -> list of {fr, round_pts_dict}
    for _ in range(runs):
        result = run_tournament_tracked(teams, matches, by_fdid, players_by_team)
        for tid, stats in result["team_stats"].items():
            total = sum(result["team_round_pts"][tid].values())
            runs_data[tid].append({
                "fr": stats["final_round"],
                "total": total,
                "round_pts": dict(result["team_round_pts"][tid]),
            })

    mv = {}
    for tid, runs_list in runs_data.items():
        mv[tid] = {}
        for R in KO_WINDOWS:  # the round about to start
            R_idx = ADVANCEMENT_ORDER.index(R)
            reached = [r for r in runs_list if ADVANCEMENT_ORDER.index(r["fr"]) >= R_idx]
            if not reached:
                mv[tid][R] = 0
                continue
            # accrued_at_R = sum of pts earned in rounds <= R
            mean_total = mean(r["total"] for r in reached)
            mean_accrued = mean(
                sum(r["round_pts"].get(rr, 0)
                    for rr in ADVANCEMENT_ORDER[:R_idx + 1])
                for r in reached
            )
            future = max(0, mean_total - mean_accrued)
            mv[tid][R] = max(1, rhu(future / TARGET_ROI))
    return mv


def buy_price(mv_val):
    if mv_val <= 0: return 0
    return max(mv_val + 1, rhu(mv_val * VIG_BUY))


def sell_price(mv_val):
    if mv_val <= 0: return 0
    return max(1, min(mv_val - 1, rhu(mv_val * VIG_SELL)))


# ---------------------------------------------------------------------------
# Strategy harness
# ---------------------------------------------------------------------------

def draft_balanced(teams):
    """Balanced 12-pick draft using current prices. Approximates a typical
    user's first roster: a top team, some mid teams, fill cheap, no
    players for simplicity (this is team-only analysis)."""
    by_price = sorted(teams, key=lambda t: -t["basePrice"])
    picks = []
    spent = 0
    # 1 top, 2 next, fill cheap
    for t in by_price[:1] + by_price[6:11] + by_price[20:26]:
        if len(picks) >= ROSTER_CAP: break
        if spent + t["basePrice"] > BUDGET: continue
        picks.append({"team_id": t["id"], "purchasePrice": t["basePrice"]})
        spent += t["basePrice"]
    # Top up with cheapest if short
    cheap = [t for t in sorted(teams, key=lambda x: x["basePrice"])
             if t["id"] not in {p["team_id"] for p in picks}]
    for t in cheap:
        if len(picks) >= ROSTER_CAP: break
        if spent + t["basePrice"] > BUDGET: continue
        picks.append({"team_id": t["id"], "purchasePrice": t["basePrice"]})
        spent += t["basePrice"]
    return picks, BUDGET - spent


def draft_minimal(teams):
    """Cheapest 12 teams. Maximizes leftover budget."""
    by_price = sorted(teams, key=lambda t: x["basePrice"] if (x := t) else 0)
    picks = []
    spent = 0
    for t in sorted(teams, key=lambda x: x["basePrice"]):
        if len(picks) >= ROSTER_CAP: break
        picks.append({"team_id": t["id"], "purchasePrice": t["basePrice"]})
        spent += t["basePrice"]
    return picks, BUDGET - spent


def strategy_hold(state):
    """Never voluntarily transfer."""
    return [], []


def strategy_aggressive(state):
    """Each window: use 1 buy if available. Buy the un-owned alive team
    with the highest E[future] / buy_price ratio. Sell lowest-MV pick to
    fund if needed."""
    if state["buys_left"] <= 0: return [], []
    owned_ids = {p["team_id"] for p in state["roster"]}
    alive_unowned = [
        (tid, state["mv_table"][tid][state["window"]])
        for tid in state["alive_teams"]
        if tid not in owned_ids and state["mv_table"][tid][state["window"]] > 0
    ]
    if not alive_unowned: return [], []
    # Pick highest MV (proxy for highest E[future])
    alive_unowned.sort(key=lambda x: -x[1])
    target_tid, target_mv = alive_unowned[0]
    target_buy = buy_price(target_mv)
    # Sell lowest-MV held team if needed for budget
    sells = []
    budget_after_sells = state["budget"]
    if budget_after_sells < target_buy:
        held_alive = [(p["team_id"], state["mv_table"][p["team_id"]][state["window"]])
                      for p in state["roster"]
                      if p["team_id"] in state["alive_teams"]]
        held_alive.sort(key=lambda x: x[1])
        for tid, mv_v in held_alive:
            sells.append(tid)
            budget_after_sells += sell_price(mv_v)
            if budget_after_sells >= target_buy: break
    if budget_after_sells < target_buy:
        return [], []  # can't afford even after selling
    return sells, [target_tid]


def strategy_war_chest(state):
    """Don't transfer until SF window. At SF, identify the 4 SF teams and
    buy as many as budget + buys allow. At F, buy any remaining finalist."""
    if state["window"] not in ("SF", "F"):
        return [], []

    owned_ids = {p["team_id"] for p in state["roster"]}

    if state["window"] == "SF":
        # The 4 alive teams are the SF participants
        sf_teams = list(state["alive_teams"].keys())
        candidates = [
            (tid, state["mv_table"][tid]["SF"])
            for tid in sf_teams if tid not in owned_ids
        ]
        candidates.sort(key=lambda x: -x[1])  # best first
        return _buy_within_constraints(state, candidates)

    if state["window"] == "F":
        # The 2 alive teams are the finalists — known info
        finalists = list(state["alive_teams"].keys())
        candidates = [
            (tid, state["mv_table"][tid]["F"])
            for tid in finalists if tid not in owned_ids
        ]
        return _buy_within_constraints(state, candidates)
    return [], []


def strategy_finalist_exploit(state):
    """Save EVERYTHING for F window. At F: buy both finalists."""
    if state["window"] != "F": return [], []
    owned_ids = {p["team_id"] for p in state["roster"]}
    finalists = list(state["alive_teams"].keys())
    candidates = [
        (tid, state["mv_table"][tid]["F"])
        for tid in finalists if tid not in owned_ids
    ]
    return _buy_within_constraints(state, candidates)


def _buy_within_constraints(state, candidates):
    """Buy from `candidates` (list of (id, mv)) within buys_left and budget.
    Sells lowest-MV held teams if needed to fund. Returns (sells, buys)."""
    sells, buys = [], []
    budget = state["budget"]
    buys_left = state["buys_left"]
    roster_size = len(state["roster"])

    # Pre-sort sell candidates (lowest MV first)
    held_alive_sellable = sorted(
        [(p["team_id"], state["mv_table"][p["team_id"]][state["window"]])
         for p in state["roster"]
         if p["team_id"] in state["alive_teams"]],
        key=lambda x: x[1]
    )

    for tid, mv_v in candidates:
        if buys_left <= 0: break
        bp = buy_price(mv_v)
        if bp <= 0: continue
        # Make room (roster cap)
        while roster_size >= ROSTER_CAP and held_alive_sellable:
            s_id, s_mv = held_alive_sellable.pop(0)
            if s_id in sells: continue
            sells.append(s_id)
            budget += sell_price(s_mv)
            roster_size -= 1
        # Fund the buy
        while budget < bp and held_alive_sellable:
            s_id, s_mv = held_alive_sellable.pop(0)
            if s_id in sells: continue
            sells.append(s_id)
            budget += sell_price(s_mv)
            roster_size -= 1
        if budget < bp: continue
        buys.append(tid)
        budget -= bp
        buys_left -= 1
        roster_size += 1
    return sells, buys


# ---------------------------------------------------------------------------
# Strategy evaluator: walk through windows applying decisions, tally points
# ---------------------------------------------------------------------------

def evaluate_strategy(strategy_fn, draft_fn, cap, sim_result, mv_table, all_teams_by_id):
    """Returns final score for this strategy under this buy cap, given one
    tournament outcome (sim_result)."""
    roster, budget = draft_fn(list(all_teams_by_id.values()))
    buys_left = cap

    # Group stage points: all draft picks earn group points
    score = 0
    for pick in roster:
        score += sim_result["team_round_pts"][pick["team_id"]].get("group", 0)

    # Walk through each KO window (transfer happens BEFORE that round's matches)
    team_stats = sim_result["team_stats"]
    team_round_pts = sim_result["team_round_pts"]

    for window in KO_WINDOWS:
        # Who's alive at the start of this round? final_round >= window_idx
        window_idx = ADVANCEMENT_ORDER.index(window)
        alive_team_ids = {
            tid for tid, st in team_stats.items()
            if ADVANCEMENT_ORDER.index(st["final_round"]) >= window_idx
        }
        eliminated_team_ids = set(team_stats.keys()) - alive_team_ids

        # First: auto-sell any eliminated picks (refund 25% of last MV)
        new_roster = []
        for pick in roster:
            if pick["team_id"] in eliminated_team_ids:
                # auto-sell at 25% of last MV
                # last MV = MV at the LAST window the team was alive in
                # (We'll approximate: use the current-window MV from BEFORE
                # they were eliminated, which is the MV at their final_round.)
                last_alive_round = team_stats[pick["team_id"]]["final_round"]
                last_alive_idx = ADVANCEMENT_ORDER.index(last_alive_round)
                # The window where they were last alive = max round they reached
                # but the MV table only has KO_WINDOWS entries. Use the
                # corresponding round if it exists; otherwise no refund.
                if last_alive_round in KO_WINDOWS:
                    last_mv = mv_table[pick["team_id"]].get(last_alive_round, 0)
                else:
                    last_mv = 0  # eliminated in group with no transfer history
                refund = max(0, rhu(last_mv * ELIM_REFUND_RATE))
                budget += refund
            else:
                new_roster.append(pick)
        roster = new_roster

        # Build state for strategy
        alive_teams_state = {
            tid: {"mv": mv_table[tid].get(window, 0)}
            for tid in alive_team_ids
        }
        state = {
            "window":         window,
            "roster":         roster,
            "budget":         budget,
            "buys_left":      buys_left,
            "alive_teams":    alive_teams_state,
            "mv_table":       mv_table,
        }

        sells, buys = strategy_fn(state)
        # Apply sells (voluntary)
        for tid in sells:
            for i, pick in enumerate(roster):
                if pick["team_id"] == tid:
                    mv_v = mv_table[tid].get(window, 0)
                    budget += sell_price(mv_v)
                    roster.pop(i)
                    break
        # Apply buys
        for tid in buys:
            if buys_left <= 0: break
            mv_v = mv_table[tid].get(window, 0)
            bp = buy_price(mv_v)
            if bp > budget or len(roster) >= ROSTER_CAP: continue
            roster.append({"team_id": tid, "purchasePrice": bp})
            budget -= bp
            buys_left -= 1

        # Now ROUND `window`'s matches play: earn points for held picks
        for pick in roster:
            score += team_round_pts[pick["team_id"]].get(window, 0)

    return score


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    teams, players = load_seed()
    _, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)
    matches = load_match_schedule()

    by_id = {t["id"]: t for t in teams}

    print(f"Computing MV table from {min(300, args.runs)} pre-sims...", file=sys.stderr)
    mv_table = compute_mv_table(teams, matches, by_fdid, players_by_team,
                                 runs=min(300, args.runs))

    print(f"Running {args.runs} tournament outcomes...", file=sys.stderr)
    sim_results = []
    for i in range(args.runs):
        if i and i % 100 == 0:
            print(f"  sim {i}/{args.runs}", file=sys.stderr)
        sim_results.append(run_tournament_tracked(teams, matches, by_fdid, players_by_team))

    STRATEGIES = [
        ("HOLD",               strategy_hold,              draft_balanced),
        ("AGGRESSIVE",         strategy_aggressive,        draft_balanced),
        ("WAR_CHEST (SF/F)",   strategy_war_chest,         draft_minimal),
        ("FINALIST_EXPLOIT",   strategy_finalist_exploit,  draft_minimal),
    ]
    CAPS = [3, 5, 15]

    print(f"\n{'='*108}")
    print(f"  STRATEGY COMPARISON ACROSS BUY CAPS ({args.runs} sims each)")
    print(f"{'='*108}")
    print(f"\n  {'Strategy':<22}", end="")
    for cap in CAPS:
        print(f"{'cap=' + str(cap) + ' (mean / std / hit-champ%)':>26}", end="")
    print()
    print("  " + "-" * 100)

    for strat_name, strat_fn, draft_fn in STRATEGIES:
        print(f"  {strat_name:<22}", end="")
        for cap in CAPS:
            scores = []
            held_champ = 0
            for sim_r in sim_results:
                score = evaluate_strategy(strat_fn, draft_fn, cap, sim_r,
                                          mv_table, by_id)
                scores.append(score)
                # Did the strategy own the champion at the F window?
                # (champion = the team whose final_round == "W")
                champs = [tid for tid, st in sim_r["team_stats"].items()
                          if st["final_round"] == "W"]
                if not champs: continue
                # Reconstruct final roster (re-run for this metric — costly
                # but simple; can skip if too slow). For now skip detail.
            m = mean(scores)
            s = stdev(scores) if len(scores) > 1 else 0
            print(f"{m:>10.1f} / {s:>5.1f}    ", end="")
        print()

    print()
    print("Reading the table:")
    print("  - mean  = expected total points across N sims")
    print("  - std   = volatility (lower = more consistent)")
    print("  - If FINALIST_EXPLOIT mean ≈ matches or exceeds others at cap=15 but is")
    print("    notably lower at cap=3, then the cap successfully kills the exploit.")
    print("  - We want all 4 strategies clustered (within ~10pts) at the chosen cap.")


if __name__ == "__main__":
    main()
