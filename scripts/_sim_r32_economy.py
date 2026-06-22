"""READ-ONLY what-if: simulate the rest of the group stage, then show what the
R32 reprice + economy would look like. Writes NOTHING to Firestore.

- Seeds standings with the REAL finished group results, simulates the remaining
  group games (Poisson-by-price, the production model), and determines advancers
  with the SAME _determine_advancers (top-2 per group + 8 best thirds).
- Runs many draws to get each team's P(advance) and the expected economy
  (every user's budget after eliminated picks auto-sell at 25% of purchase
  price), then reprices the expected top-32 field with the production engine.

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=...sa.json \\
    ./venv/bin/python scripts/_sim_r32_economy.py [--draws 3000] [--reprice-runs 1000]
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client
from simulate_2026 import simulate_match, _determine_advancers
from reprice import reprice as compute_reprice, ELIM_REFUND_RATE, round_half_up, TARGET_ROI


def play_group_stage(by_slug, group_matches):
    """One draw: real results for FINISHED games, simulated for the rest.
    Returns (team_stats, team_group) shaped for _determine_advancers."""
    ts = {tid: dict(group_pts=0, group_gd=0, group_gf=0) for tid in by_slug}
    tg = {}
    for m in group_matches:
        t1, t2, grp = m.get("team1Id"), m.get("team2Id"), m.get("group")
        if not (t1 and t2 and t1 in by_slug and t2 in by_slug):
            continue
        tg[t1] = grp; tg[t2] = grp
        if m.get("status") == "FINISHED" and m.get("score1") is not None:
            ga, gb = m["score1"], m["score2"]
        else:
            ga, gb, _ = simulate_match(by_slug[t1], by_slug[t2])
        ts[t1]["group_gf"] += ga; ts[t1]["group_gd"] += ga - gb
        ts[t2]["group_gf"] += gb; ts[t2]["group_gd"] += gb - ga
        if ga > gb:   ts[t1]["group_pts"] += 3
        elif gb > ga: ts[t2]["group_pts"] += 3
        else:         ts[t1]["group_pts"] += 1; ts[t2]["group_pts"] += 1
    return ts, tg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3000)
    ap.add_argument("--reprice-runs", type=int, default=1000)
    args = ap.parse_args()
    random.seed(42)

    db = firestore_client()
    teams = {d.id: {**(d.to_dict() or {}), "id": d.id} for d in db.collection("teams").stream()}
    players = [{**(d.to_dict() or {}), "id": d.id} for d in db.collection("players").stream()]
    users = [{**(d.to_dict() or {}), "uid": d.id} for d in db.collection("users").stream()]
    matches = [d.to_dict() or {} for d in db.collection("matches").stream()]
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p.get("teamId")].append(p)

    group_matches = [m for m in matches if m.get("round") == "group"]
    done = sum(1 for m in group_matches if m.get("status") == "FINISHED")
    print(f"Group matches: {done}/{len(group_matches)} finished; "
          f"simulating the remaining {len(group_matches)-done} over {args.draws} draws\n")

    # --- Monte Carlo: advancement frequency + expected economy ---------------
    adv_count = defaultdict(int)
    user_budget_sum = defaultdict(float)
    user_elim_picks_sum = defaultdict(float)
    for _ in range(args.draws):
        ts, tg = play_group_stage(teams, group_matches)
        advancers = set(_determine_advancers(ts, tg))
        for tid in advancers:
            adv_count[tid] += 1
        for u in users:
            refund = 0; elim = 0
            for pick in (u.get("roster") or []):
                tid = pick["assetId"] if pick["kind"] == "team" else \
                    (next((p for p in players if p["id"] == pick["assetId"]), {}) or {}).get("teamId")
                if tid is not None and tid not in advancers:
                    refund += round_half_up((pick.get("purchasePrice", 0) or 0) * ELIM_REFUND_RATE)
                    elim += 1
            user_budget_sum[u["uid"]] += (u.get("currentBudget", 0) or 0) + refund
            user_elim_picks_sum[u["uid"]] += elim

    n = args.draws
    p_adv = {tid: adv_count[tid] / n for tid in teams}

    # --- Economy summary ------------------------------------------------------
    exp_budget = {u["uid"]: user_budget_sum[u["uid"]] / n for u in users}
    cur_budget = {u["uid"]: (u.get("currentBudget", 0) or 0) for u in users}
    vals = sorted(exp_budget.values())
    avg = sum(vals) / len(vals)
    cur_avg = sum(cur_budget.values()) / len(cur_budget)
    avg_elim = sum(user_elim_picks_sum.values()) / n / len(users)
    print("=" * 64)
    print("ECONOMY after the R32 auto-sell (expected over draws)")
    print("=" * 64)
    print(f"  users: {len(users)}")
    print(f"  avg manager cash now:           ${cur_avg:5.1f}")
    print(f"  avg manager cash post-auto-sell:${avg:5.1f}   (+${avg-cur_avg:.1f} from 25% refunds)")
    print(f"  cash range post-auto-sell:      ${vals[0]:.0f} … ${vals[-1]:.0f}")
    print(f"  median:                         ${vals[len(vals)//2]:.0f}")
    print(f"  avg picks auto-sold per manager: {avg_elim:.1f}")

    # --- Advancement buckets --------------------------------------------------
    locks = [t for t in teams if p_adv[t] >= 0.95]
    bubble = [t for t in teams if 0.4 <= p_adv[t] < 0.95]
    longshot = [t for t in teams if p_adv[t] < 0.4]
    print("\n" + "=" * 64)
    print(f"ADVANCEMENT: {len(locks)} near-locks (P≥95%), {len(bubble)} on the bubble, "
          f"{len(longshot)} likely out")
    print("=" * 64)
    print("  Bubble teams (P 40–95%):")
    for t in sorted(bubble, key=lambda t: -p_adv[t]):
        print(f"    {p_adv[t]*100:5.1f}%  {teams[t].get('name', t)}")

    # --- Reprice the EXPECTED top-32 field ------------------------------------
    field_ids = sorted(teams, key=lambda t: -p_adv[t])[:32]
    field = [teams[t] for t in field_ids]
    print("\n" + "=" * 64)
    print(f"REPRICE — expected R32 field (top 32 by P(advance)), {args.reprice_runs} sims")
    print("=" * 64)
    prices = compute_reprice(field, "R32", players_by_team, runs=args.reprice_runs, seed=42)
    tlist = sorted([p for p in prices.values() if p["kind"] == "team"],
                   key=lambda p: -p["marketValue"])
    plist = sorted([p for p in prices.values() if p["kind"] == "player"],
                   key=lambda p: -p["marketValue"])
    print(f"\n  TOP 15 TEAMS            {'E[pts]':>7} {'Sell':>5} {'MV':>4} {'Buy':>5}")
    for p in tlist[:15]:
        print(f"    {p['name'][:20]:<20} {p['meanFuturePoints']:>7.1f} "
              f"${p['sellPrice']:>3} ${p['marketValue']:>2} ${p['buyPrice']:>3}")
    print(f"\n  TOP 15 PLAYERS         {'E[pts]':>7} {'Sell':>5} {'MV':>4} {'Buy':>5}")
    for p in plist[:15]:
        print(f"    {p['name'][:20]:<20} {p['meanFuturePoints']:>7.1f} "
              f"${p['sellPrice']:>3} ${p['marketValue']:>2} ${p['buyPrice']:>3}")
    mvs = [p["marketValue"] for p in tlist]
    print(f"\n  team MV range: ${min(mvs)}…${max(mvs)};  "
          f"players priced: {len(plist)} (of {len(players)})")

    # --- Does pricing track expected points? ---------------------------------
    # marketValue = round(E[pts] / TARGET_ROI), so pts-per-$ should sit near
    # TARGET_ROI. Integer rounding at low MV distorts that; measure the spread.
    def _pearson(xs, ys):
        n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
        cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
        vx = sum((x-mx)**2 for x in xs); vy = sum((y-my)**2 for y in ys)
        return cov/((vx*vy)**0.5) if vx and vy else 0.0

    def quality(items, label):
        E  = [p["meanFuturePoints"] for p in items]
        MV = [p["marketValue"] for p in items]
        ppd_mv  = [p["meanFuturePoints"]/p["marketValue"] for p in items if p["marketValue"]]
        ppd_buy = [p["meanFuturePoints"]/p["buyPrice"] for p in items if p["buyPrice"]]
        print(f"\n  {label} (n={len(items)}):")
        print(f"    corr(E[pts], MV)          = {_pearson(E, MV):.3f}  (1.0 = perfect)")
        print(f"    pts/$ at MV : avg {sum(ppd_mv)/len(ppd_mv):.2f}  "
              f"(target {TARGET_ROI})  range {min(ppd_mv):.2f}–{max(ppd_mv):.2f}")
        print(f"    pts/$ at BUY: avg {sum(ppd_buy)/len(ppd_buy):.2f}  "
              f"range {min(ppd_buy):.2f}–{max(ppd_buy):.2f}")
        # best/worst value at the buy price - where rounding makes bargains/traps
        ranked = sorted(items, key=lambda p: -(p["meanFuturePoints"]/p["buyPrice"]) if p["buyPrice"] else 0)
        bargain = ranked[0]; trap = ranked[-1]
        print(f"    best value : {bargain['name'][:18]:<18} {bargain['meanFuturePoints']:.1f}pts @ "
              f"buy ${bargain['buyPrice']} = {bargain['meanFuturePoints']/bargain['buyPrice']:.2f} pts/$")
        print(f"    worst value: {trap['name'][:18]:<18} {trap['meanFuturePoints']:.1f}pts @ "
              f"buy ${trap['buyPrice']} = {trap['meanFuturePoints']/trap['buyPrice']:.2f} pts/$")

    print("\n" + "=" * 64)
    print("PRICING QUALITY — does MV/buy track expected points?")
    print("=" * 64)
    quality(tlist, "Teams")
    quality(plist, "Players")
    print("\n[what-if only — nothing written to Firestore]")


if __name__ == "__main__":
    main()
