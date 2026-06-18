"""Can pricing alone neutralize the harvest-pivot meta?

Reuses the staged tournament sim from analyze_timing_meta, but makes DRAFT and
TRANSFER pricing configurable, and sweeps two knobs the user proposed:
  - player_draft_mult: players cost MORE at draft (premium for bankable front-load)
  - team_buy_mult:     teams "hold price" -> pivoting INTO them is taxed harder

For each (knob) cell, reports the 4 strategies' mean EV + the pivot edge
(PIVOT - HOLD on the same player-heavy draft). Target cell: archetypes converge
AND pivot edge ~ 0  (no consensus exploit).

Usage: ./venv/bin/python scripts/analyze_pricing_levers.py [--runs N] [--rosters K]
"""
from __future__ import annotations
import argparse, random, statistics, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_timing_meta import run_staged
from simulate_2026 import (load_seed, load_match_schedule, build_team_indexes)

BUDGET, ROSTER_CAP, SELL_VIG, ELIM_REFUND, MAX_BUYS = 60, 12, 0.90, 0.25, 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1500)
    ap.add_argument("--rosters", type=int, default=60)
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()
    random.seed(args.seed)

    teams, players = load_seed()
    matches = load_match_schedule()
    _, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players: players_by_team[p["teamId"]].append(p)
    pos_by_pid = {p["id"]: p.get("position", "?") for p in players}
    base_team = {t["id"]: t["basePrice"] for t in teams}
    base_player = {p["id"]: p["basePrice"] for p in players}
    team_of_player = {p["id"]: p["teamId"] for p in players}

    # ---- generate sims ONCE; pricing knobs only affect the cheap eval layer ----
    sims = []
    sum_tk = defaultdict(float); cnt_t = defaultdict(int)
    sum_pk = defaultdict(float); cnt_p = defaultdict(int)
    for _ in range(args.runs):
        tg, tk, pg, pk, adv = run_staged(teams, matches, by_fdid, players_by_team, pos_by_pid)
        sims.append((tg, tk, pg, pk, adv))
        for t in teams:
            if t["id"] in adv: sum_tk[t["id"]] += tk.get(t["id"], 0); cnt_t[t["id"]] += 1
        for p in players:
            if team_of_player[p["id"]] in adv: sum_pk[p["id"]] += pk.get(p["id"], 0); cnt_p[p["id"]] += 1
    mv_team = {t["id"]: (sum_tk[t["id"]]/cnt_t[t["id"]]/2.0) if cnt_t[t["id"]] else 0.0 for t in teams}
    mv_player = {p["id"]: (sum_pk[p["id"]]/cnt_p[p["id"]]/2.0) if cnt_p[p["id"]] else 0.0 for p in players}
    sell_player = {k: max(0, round(v*SELL_VIG)) for k, v in mv_player.items()}

    def draft_price(kind, id_, pmult):
        base = base_team[id_] if kind == "team" else base_player[id_]
        return base * (pmult if kind == "player" else 1.0)

    def rand_roster(kinds, pmult):
        for _ in range(200):
            pool = kinds[:]; random.shuffle(pool)
            picks, spent = [], 0.0
            for kind, id_ in pool:
                if len(picks) >= ROSTER_CAP: break
                pr = draft_price(kind, id_, pmult)
                if spent + pr <= BUDGET:
                    picks.append((kind, id_)); spent += pr
            if spent >= BUDGET - 5 and len(picks) >= 8:
                return picks
        return picks

    team_kinds   = [("team", t["id"]) for t in teams]
    player_kinds = [("player", p["id"]) for p in players]
    def rosters(mix, pmult):
        out = []
        for _ in range(args.rosters):
            pool = (player_kinds if mix >= .99 else team_kinds if mix <= .01
                    else (player_kinds*3+team_kinds) if mix > .5 else (player_kinds+team_kinds*3))
            out.append(rand_roster(pool, pmult))
        return out

    def cost(roster, pmult):
        return sum(draft_price(k, i, pmult) for k, i in roster)

    def score_hold(roster, sim):
        tg, tk, pg, pk, adv = sim
        return sum((tg.get(i,0)+tk.get(i,0)) if k=="team" else (pg.get(i,0)+pk.get(i,0)) for k,i in roster)

    def score_pivot(roster, sim, pmult, buy_team):
        tg, tk, pg, pk, adv = sim
        banked = 0.0; budget = BUDGET - cost(roster, pmult); held = []
        for k, i in roster:
            alive = (i in adv) if k=="team" else (team_of_player[i] in adv)
            if not alive:
                banked += (tg if k=="team" else pg).get(i,0)
                budget += ELIM_REFUND * draft_price(k, i, pmult)
            else: held.append((k,i))
        held_ids = {i for _,i in held}
        targets = sorted([t["id"] for t in teams if t["id"] in adv and t["id"] not in held_ids],
                         key=lambda tid: mv_team[tid], reverse=True)
        sellable = sorted([(k,i) for (k,i) in held if k=="player"], key=lambda ki: sell_player[ki[1]])
        bought, buys = [], 0
        for tid in targets:
            if buys >= MAX_BUYS: break
            price = buy_team[tid]
            while (len(held)+len(bought) >= ROSTER_CAP or budget < price) and sellable:
                sk, si = sellable.pop(0)
                if (sk,si) in held:
                    held.remove((sk,si)); banked += pg.get(si,0); budget += sell_player[si]
            if budget >= price and len(held)+len(bought) < ROSTER_CAP:
                budget -= price; bought.append(tid); buys += 1
            else: break
        s = banked
        for k,i in held: s += (tg.get(i,0)+tk.get(i,0)) if k=="team" else (pg.get(i,0)+pk.get(i,0))
        for tid in bought: s += tk.get(tid,0)
        return s

    def mean_over(roster_list, scorer):
        return statistics.mean(statistics.mean(scorer(r, sim) for sim in sims) for r in roster_list)

    print("="*78)
    print(f"  PRICING-LEVER SWEEP  ({args.rosters} drafts/cell x {args.runs} sims)")
    print("  player_draft_mult = players cost X at draft;  team_buy_mult = team pivot-in price X")
    print("="*78)
    print(f"  {'P-draft':>8}{'T-buy':>7}  | {'HOLD_T':>7}{'BAL':>7}{'HOLD_P':>8}{'PIVOT':>7} | {'pivot edge':>11}{'spread':>8}")
    print("  " + "-"*74)
    for pmult in (1.0, 1.2, 1.4):
        rT = rosters(0.0, pmult); rB = rosters(0.5, pmult); rP = rosters(1.0, pmult)
        hT = mean_over(rT, score_hold); hB = mean_over(rB, score_hold); hP = mean_over(rP, score_hold)
        for bmult in (1.0, 1.4, 1.8):
            buy_team = {t["id"]: max(1, round(mv_team[t["id"]]*1.10*bmult)) for t in teams}
            piv = mean_over(rP, lambda r, sim: score_pivot(r, sim, pmult, buy_team))
            edge = piv - hP
            spread = max(hT, hB, hP, piv) - min(hT, hB, hP, piv)
            print(f"  {pmult:>8.1f}{bmult:>7.1f}  | {hT:>7.1f}{hB:>7.1f}{hP:>8.1f}{piv:>7.1f} | {edge:>+11.1f}{spread:>8.1f}")


if __name__ == "__main__":
    main()
