"""Evaluate the LIVE transfer pricing model on the current WC:
  1. Does forward-looking MV actually make players DROP and teams HOLD at the
     post-group window? By how much? (draft basePrice vs E[remaining]/2)
  2. If the pricing is fair, WHY does the harvest-pivot still have an edge?
     Decompose: baseline vs no-elim-refund vs no-capital-injection.

Reuses analyze_timing_meta.run_staged. Read-only.
"""
from __future__ import annotations
import argparse, random, statistics, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_timing_meta import run_staged
from simulate_2026 import load_seed, load_match_schedule, build_team_indexes

BUDGET, ROSTER_CAP, BUY_VIG, SELL_VIG, MAX_BUYS = 60, 12, 1.10, 0.90, 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1500)
    ap.add_argument("--rosters", type=int, default=60)
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args(); random.seed(args.seed)

    teams, players = load_seed()
    matches = load_match_schedule()
    _, by_fdid = build_team_indexes(teams)
    pbt = defaultdict(list)
    for p in players: pbt[p["teamId"]].append(p)
    pos = {p["id"]: p.get("position", "?") for p in players}
    base_t = {t["id"]: t["basePrice"] for t in teams}
    base_p = {p["id"]: p["basePrice"] for p in players}
    top = {p["id"]: p["teamId"] for p in players}

    sims = []
    sum_tk = defaultdict(float); cnt_t = defaultdict(int)
    sum_pk = defaultdict(float); cnt_p = defaultdict(int)
    for _ in range(args.runs):
        tg, tk, pg, pk, adv = run_staged(teams, matches, by_fdid, pbt, pos)
        sims.append((tg, tk, pg, pk, adv))
        for t in teams:
            if t["id"] in adv: sum_tk[t["id"]] += tk.get(t["id"], 0); cnt_t[t["id"]] += 1
        for p in players:
            if top[p["id"]] in adv: sum_pk[p["id"]] += pk.get(p["id"], 0); cnt_p[p["id"]] += 1
    mv_t = {t["id"]: (sum_tk[t["id"]]/cnt_t[t["id"]]/2.0) if cnt_t[t["id"]] else 0.0 for t in teams}
    mv_p = {p["id"]: (sum_pk[p["id"]]/cnt_p[p["id"]]/2.0) if cnt_p[p["id"]] else 0.0 for p in players}
    buy_t = {k: max(round(v)+1, round(v*BUY_VIG)) for k, v in mv_t.items()}
    sell_p = {k: max(1, round(v*SELL_VIG)) for k, v in mv_p.items()}
    padv = {p["id"]: cnt_p[p["id"]]/args.runs for p in players}   # P(player's team advances)
    tadv = {t["id"]: cnt_t[t["id"]]/args.runs for t in teams}

    # ---- 1. price asymmetry (only assets that survive groups are priceable) ----
    print("="*76); print("  POST-GROUP PRICE ASYMMETRY  (draft basePrice -> forward MV)"); print("="*76)
    def agg(ids, base, mv, survp):
        d = [base[i] for i in ids]; m = [mv[i] for i in ids]
        return statistics.mean(d), statistics.mean(m), statistics.mean(m)/statistics.mean(d), statistics.mean(survp[i] for i in ids)
    pl_ids = [p["id"] for p in players]; tm_ids = [t["id"] for t in teams]
    bd, bm, br, bs = agg(pl_ids, base_p, mv_p, padv)
    print(f"  PLAYERS (mean): draft ${bd:.2f} -> MV ${bm:.2f}  = {br:.0%} of draft   (avg P(survive groups) {bs:.0%})")
    bd, bm, br, bs = agg(tm_ids, base_t, mv_t, tadv)
    print(f"  TEAMS   (mean): draft ${bd:.2f} -> MV ${bm:.2f}  = {br:.0%} of draft   (avg P(advance) {bs:.0%})")
    print("\n  By team tier (advancers' MV vs draft):")
    for tier in (1, 2, 3, 4, 5):
        ids = [t["id"] for t in teams if t.get("tier") == tier]
        if not ids: continue
        d = statistics.mean(base_t[i] for i in ids); m = statistics.mean(mv_t[i] for i in ids)
        print(f"    tier {tier}: draft ${d:.1f} -> MV ${m:.1f}  ({m/d:.0%})   P(adv) {statistics.mean(tadv[i] for i in ids):.0%}")
    print("\n  Examples (draft -> MV / sell|buy):")
    ex = ["france","spain","england","brazil","morocco","mexico"]
    for tid in ex:
        if tid in mv_t: print(f"    {tid:10} team   ${base_t[tid]:>2} -> MV ${mv_t[tid]:>4.1f}  buy ${buy_t[tid]:>2}   ({mv_t[tid]/base_t[tid]:.0%} of draft)")
    star_ids = sorted(players, key=lambda p: -p["basePrice"])[:6]
    for p in star_ids:
        i = p["id"]; print(f"    {p['name'][:18]:10} player ${base_p[i]:>2} -> MV ${mv_p[i]:>4.1f}  sell ${sell_p[i]:>2}   ({mv_p[i]/base_p[i]:.0%} of draft)")

    # ---- rosters + pivot variants ----
    player_kinds = [("player", p["id"]) for p in players]
    def rand_roster():
        for _ in range(200):
            pool = player_kinds[:]; random.shuffle(pool)
            picks, spent = [], 0
            for k, i in pool:
                if len(picks) >= ROSTER_CAP: break
                if spent + base_p[i] <= BUDGET: picks.append((k, i)); spent += base_p[i]
            if spent >= BUDGET-5 and len(picks) >= 8: return picks
        return picks
    rosters = [rand_roster() for _ in range(args.rosters)]
    def cost(r): return sum(base_p[i] for _, i in r)

    def score_hold(r, sim):
        tg, tk, pg, pk, adv = sim
        return sum(pg.get(i,0)+pk.get(i,0) for _, i in r)

    def score_pivot(r, sim, refund_rate, allow_leftover):
        tg, tk, pg, pk, adv = sim
        banked = 0.0
        budget = (BUDGET - cost(r)) if allow_leftover else 0.0
        held = []
        for k, i in r:
            if top[i] not in adv:                       # eliminated in groups
                banked += pg.get(i,0)
                budget += refund_rate * base_p[i]
            else: held.append((k,i))
        held_ids = {i for _,i in held}
        targets = sorted([t["id"] for t in teams if t["id"] in adv and t["id"] not in held_ids],
                         key=lambda tid: mv_t[tid], reverse=True)
        sellable = sorted(held, key=lambda ki: sell_p[ki[1]])
        bought, buys = [], 0
        for tid in targets:
            if buys >= MAX_BUYS: break
            price = buy_t[tid]
            while (len(held)+len(bought) >= ROSTER_CAP or budget < price) and sellable:
                sk, si = sellable.pop(0)
                if (sk,si) in held: held.remove((sk,si)); banked += pg.get(si,0); budget += sell_p[si]
            if budget >= price and len(held)+len(bought) < ROSTER_CAP:
                budget -= price; bought.append(tid); buys += 1
            else: break
        s = banked
        for _, i in held: s += pg.get(i,0)+pk.get(i,0)
        for tid in bought: s += tk.get(tid,0)
        return s

    def edge(refund_rate, allow_leftover):
        ds = [score_pivot(r, sim, refund_rate, allow_leftover) - score_hold(r, sim)
              for r in rosters for sim in sims]
        return statistics.mean(ds), sum(1 for d in ds if d > 0)/len(ds)

    print()
    print("="*76); print("  WHY PRICING DOESN'T FULLY SELF-SOLVE: pivot edge decomposition"); print("="*76)
    for label, rr, lev in [
        ("baseline (25% refund + leftover budget)", 0.25, True),
        ("NO elim refund (leftover budget only)",   0.00, True),
        ("NO injected capital (fund only by selling alive players)", 0.00, False),
    ]:
        m, w = edge(rr, lev)
        print(f"  {label:58} edge {m:+6.1f}  (pivot wins {w:.0%})")
    print("\n  (If killing the refund collapses the edge -> the forward-looking pricing")
    print("   IS neutralizing the sell-players channel; the 25% elim refund is the leak.)")


if __name__ == "__main__":
    main()
