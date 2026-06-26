"""Honesty check for the projection strength model (read-only).

The Monte Carlo projection engine (simulate_2026.py) sets each side's Poisson
goal rate from sqrt(basePrice) shares of a 2.6-goal baseline. Before we surface
projections to users we need to know: does that coarse price-as-strength model
actually retrodict the 60 finished group matches? Or is it garbage we'd be
dressing up as a prediction?

This script pulls finished GROUP_STAGE matches + team prices from live
Firestore, computes the model's ANALYTIC outcome probabilities per match
(independent Poisson, no sampling), and scores them against reality:

  - Outcome calibration (binned P(favorite wins) vs empirical)
  - Multiclass Brier + log-loss vs two baselines (uniform 1/3; pick-fav)
  - Expected vs actual goals (total, and favorite/underdog split)
  - Retrodicted group points: model expected pts per team vs actual, Spearman

Writes nothing back. Usage:
  GOOGLE_APPLICATION_CREDENTIALS=.../key.json ./venv/bin/python scripts/validate_model_honesty.py
"""
from __future__ import annotations
import math, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client
# Import the SHIPPED strength model so this check always tests exactly what the
# projection engine uses (no drift between validator and simulator).
from simulate_2026 import BASE_GOALS_PER_MATCH, PRICE_EXPONENT
from simulate_2026 import match_lambdas as _sim_match_lambdas


def match_lambdas(pa, pb, base=BASE_GOALS_PER_MATCH):
    """Wrap simulate_2026.match_lambdas (which takes team dicts)."""
    return _sim_match_lambdas({"basePrice": pa}, {"basePrice": pb}, base)


def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def outcome_probs(la, lb, kmax=12):
    """P(home win), P(draw), P(away win) under independent Poisson."""
    pa = [poisson_pmf(i, la) for i in range(kmax + 1)]
    pb = [poisson_pmf(j, lb) for j in range(kmax + 1)]
    pH = pD = pA = 0.0
    for i in range(kmax + 1):
        for j in range(kmax + 1):
            p = pa[i] * pb[j]
            if i > j: pH += p
            elif i == j: pD += p
            else: pA += p
    s = pH + pD + pA
    return pH / s, pD / s, pA / s


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx = sum(rx) / n; my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return num / (dx * dy) if dx and dy else 0.0


def main():
    db = firestore_client()

    price = {}
    for t in db.collection("teams").stream():
        td = t.to_dict() or {}
        price[t.id] = td.get("basePrice") or 1

    matches = []
    for m in db.collection("matches").stream():
        md = m.to_dict() or {}
        if (md.get("stage") or "GROUP_STAGE") != "GROUP_STAGE":
            continue
        if md.get("status") != "FINISHED":
            continue
        t1, t2 = md.get("team1Id"), md.get("team2Id")
        s1, s2 = md.get("score1"), md.get("score2")
        if t1 not in price or t2 not in price or s1 is None or s2 is None:
            continue
        matches.append((t1, t2, int(s1), int(s2)))

    n = len(matches)
    print(f"Finished group matches usable: {n}\n")
    if n == 0:
        sys.exit("No finished group matches with prices + scores found.")

    # ---- Per-match model probs + accumulators -----------------------------
    brier_model = brier_unif = 0.0
    ll_model = ll_unif = ll_fav = 0.0
    correct_argmax = correct_fav = 0
    exp_goals_tot = act_goals_tot = 0.0
    exp_fav = exp_dog = act_fav = act_dog = 0.0
    # calibration bins on P(favorite wins)
    cal = defaultdict(lambda: [0.0, 0, 0])  # bin -> [sum_p, n, fav_wins]
    # retrodicted group points
    exp_pts = defaultdict(float); act_pts = defaultdict(float)

    EPS = 1e-12
    for (t1, t2, s1, s2) in matches:
        p1, p2 = price[t1], price[t2]
        la, lb = match_lambdas(p1, p2)
        pH, pD, pA = outcome_probs(la, lb)

        # actual outcome one-hot (H, D, A)
        if s1 > s2: y = (1, 0, 0); res = "H"
        elif s1 == s2: y = (0, 1, 0); res = "D"
        else: y = (0, 0, 1); res = "A"
        pv = (pH, pD, pA)

        brier_model += sum((pv[k] - y[k]) ** 2 for k in range(3))
        brier_unif += sum((1/3 - y[k]) ** 2 for k in range(3))
        ll_model += -math.log(max(EPS, pv[y.index(1)]))
        ll_unif += -math.log(1/3)
        # pick-fav baseline: 1-eps on the higher-priced team's win, tiny elsewhere
        fav_is_1 = p1 >= p2
        favp = (max(p1, p2)); # unused
        fav_probs = (0.6, 0.2, 0.2) if fav_is_1 else (0.2, 0.2, 0.6)
        ll_fav += -math.log(max(EPS, fav_probs[y.index(1)]))

        if max(pv) == pv[y.index(1)]:
            correct_argmax += 1

        # favorite framing
        p_fav_win = pH if fav_is_1 else pA
        fav_actually_won = (res == "H" and fav_is_1) or (res == "A" and not fav_is_1)
        if fav_actually_won: correct_fav += 1
        b = min(9, int(p_fav_win * 10))
        cal[b][0] += p_fav_win; cal[b][1] += 1; cal[b][2] += 1 if fav_actually_won else 0

        # goals
        exp_goals_tot += la + lb; act_goals_tot += s1 + s2
        if fav_is_1:
            exp_fav += la; exp_dog += lb; act_fav += s1; act_dog += s2
        else:
            exp_fav += lb; exp_dog += la; act_fav += s2; act_dog += s1

        # expected group points (3*P(win)+1*P(draw)) vs actual
        exp_pts[t1] += 3*pH + 1*pD; exp_pts[t2] += 3*pA + 1*pD
        act_pts[t1] += 3*y[0] + 1*y[1]; act_pts[t2] += 3*y[2] + 1*y[1]

    # ---- Report -----------------------------------------------------------
    print("OUTCOME PREDICTION (lower Brier / log-loss = better)")
    print(f"  {'model':<22}Brier={brier_model/n:.3f}   logloss={ll_model/n:.3f}")
    print(f"  {'baseline: uniform 1/3':<22}Brier={brier_unif/n:.3f}   logloss={ll_unif/n:.3f}")
    print(f"  {'baseline: pick-favorite':<22}{'':14}logloss={ll_fav/n:.3f}")
    print(f"  model picks correct (argmax incl. draw): {correct_argmax}/{n} = {100*correct_argmax/n:.0f}%")
    print(f"  favorite (by price) actually won        : {correct_fav}/{n} = {100*correct_fav/n:.0f}%")
    skill = 100 * (1 - (ll_model/n) / (ll_unif/n))
    print(f"  log-loss skill vs uniform: {skill:+.0f}%  "
          f"({'adds signal' if skill > 0 else 'WORSE THAN COIN FLIP'})")

    print("\nCALIBRATION  (does P(fav wins) match reality?)")
    print(f"  {'pred bin':>10} {'n':>4} {'pred':>6} {'actual':>7}")
    for b in sorted(cal):
        sp, cnt, w = cal[b]
        if cnt == 0: continue
        print(f"  {b*10:>3}-{b*10+10:<6} {cnt:>4} {100*sp/cnt:>5.0f}% {100*w/cnt:>6.0f}%")

    print("\nGOALS (model vs actual, per match avg)")
    print(f"  total : model {exp_goals_tot/n:.2f}   actual {act_goals_tot/n:.2f}")
    print(f"  favorite scored : model {exp_fav/n:.2f}   actual {act_fav/n:.2f}")
    print(f"  underdog scored : model {exp_dog/n:.2f}   actual {act_dog/n:.2f}")

    teams = list(exp_pts.keys())
    rho = spearman([exp_pts[t] for t in teams], [act_pts[t] for t in teams])
    print("\nRETRODICTED GROUP POINTS (model expected vs actual, by team)")
    print(f"  Spearman rho = {rho:.3f}   (n={len(teams)} teams)")
    paired = sorted(teams, key=lambda t: -act_pts[t])
    print(f"  {'team':<26}{'exp':>6}{'act':>6}")
    for t in paired[:6] + ["..."] + paired[-4:]:
        if t == "...":
            print("  ...")
            continue
        print(f"  {t:<26}{exp_pts[t]:>6.1f}{act_pts[t]:>6.0f}")


if __name__ == "__main__":
    main()
