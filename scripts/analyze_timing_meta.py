"""Does 'draft players early, pivot to teams late' dominate?

Reuses simulate_2026's match/attribution/bracket primitives but tracks points
split by GROUP vs KNOCKOUT per asset, so we can model the live rules that the
old buy-cap sim predates:
  - forward-only scoring (seller KEEPS points earned while held)
  - forward-looking transfer pricing (MV = E[remaining pts]/2), 10% vig
  - 25%-of-purchase-price auto-sell on elimination
  - 3 voluntary buys TOTAL across the tournament

Strategies (all drafted to ~$60 / 12 picks, evaluated on the SAME sims):
  HOLD_TEAMS    - team-heavy draft, never transfer
  HOLD_BALANCED - mix, never transfer
  HOLD_PLAYERS  - player-heavy draft, never transfer
  HARVEST_PIVOT - player-heavy draft, then at the post-group window sell
                  depleted players + use 3 buys on the strongest survivors

Usage: ./venv/bin/python scripts/analyze_timing_meta.py [--runs N] [--rosters K]
"""
from __future__ import annotations
import argparse, random, statistics, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simulate_2026 as S
from simulate_2026 import (
    load_seed, load_match_schedule, build_team_indexes,
    simulate_match, attribute_goals, attribute_assists, pick_lineup,
    _determine_advancers, _winner_loser, WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER,
)

BUDGET = 60
ROSTER_CAP = 12
VIG_BUY, VIG_SELL = 1.10, 0.90
ELIM_REFUND = 0.25
MAX_BUYS = 3


def player_match_points(pid, pos, goals, assists, won, cs):
    if   pos == "GK":  cs_rate = WEIGHTS["player_clean_sheet_gk"]
    elif pos == "DEF": cs_rate = WEIGHTS["player_clean_sheet_def"]
    else:              cs_rate = WEIGHTS["player_clean_sheet_other"]
    return (WEIGHTS["player_goal"] * goals + WEIGHTS["player_assist"] * assists
            + WEIGHTS["player_win_share"] * won + cs_rate * cs)


def run_staged(teams, matches, by_fdid, players_by_team, pos_by_pid):
    """One tournament. Returns per-asset GROUP vs KNOCKOUT points + advanced flag."""
    tg = defaultdict(int); tk = defaultdict(int)          # team group / knockout pts
    pg = defaultdict(int); pk = defaultdict(int)          # player group / knockout pts
    advanced = set()
    stats = {t["id"]: dict(wins=0, draws=0, group_pts=0, group_gd=0, group_gf=0) for t in teams}
    team_group = {}

    def record(ta, tb, ga, gb, is_group):
        bucket_t = tg if is_group else tk
        bucket_p = pg if is_group else pk
        # team result pts
        if ga > gb:   bucket_t[ta["id"]] += WEIGHTS["team_win"]
        elif gb > ga: bucket_t[tb["id"]] += WEIGHTS["team_win"]
        else:
            bucket_t[ta["id"]] += WEIGHTS["team_draw"]; bucket_t[tb["id"]] += WEIGHTS["team_draw"]
        if is_group:
            sa, sb = stats[ta["id"]], stats[tb["id"]]
            sa["group_gf"] += ga; sa["group_gd"] += ga - gb
            sb["group_gf"] += gb; sb["group_gd"] += gb - ga
            if ga > gb:   sa["group_pts"] += 3
            elif gb > ga: sb["group_pts"] += 3
            else:         sa["group_pts"] += 1; sb["group_pts"] += 1
        cs_a, cs_b = (gb == 0), (ga == 0)
        # player scoring this match
        lna, lnb = pick_lineup(ta, players_by_team), pick_lineup(tb, players_by_team)
        sc_a = attribute_goals(ta, ga, players_by_team)
        sc_b = attribute_goals(tb, gb, players_by_team)
        gcount = defaultdict(int); acount = defaultdict(int)
        for p in sc_a: gcount[p["id"]] += 1
        for p in sc_b: gcount[p["id"]] += 1
        as_a = attribute_assists(ta, sum(1 for _ in sc_a if random.random() < 0.6),
                                 players_by_team, {p["id"] for p in sc_a})
        as_b = attribute_assists(tb, sum(1 for _ in sc_b if random.random() < 0.6),
                                 players_by_team, {p["id"] for p in sc_b})
        for p in as_a: acount[p["id"]] += 1
        for p in as_b: acount[p["id"]] += 1
        a_won, b_won = ga > gb, gb > ga
        # everyone who scored/assisted/played on each side
        for side, lineup, won, cs in ((ta, lna, a_won, cs_a), (tb, lnb, b_won, cs_b)):
            for pid in (set(gcount) | set(acount) | lineup):
                # only credit players actually on this team
                if pid not in {p["id"] for p in players_by_team.get(side["id"], [])}:
                    continue
                played = pid in lineup
                pts = player_match_points(
                    pid, pos_by_pid.get(pid, "?"),
                    gcount.get(pid, 0), acount.get(pid, 0),
                    1 if (won and played) else 0, 1 if (cs and played) else 0)
                bucket_p[pid] += pts

    # group stage
    for m in [m for m in matches if (m.get("stage") or "GROUP_STAGE") == "GROUP_STAGE"]:
        ta = by_fdid.get((m.get("homeTeam") or {}).get("id"))
        tb = by_fdid.get((m.get("awayTeam") or {}).get("id"))
        if not (ta and tb): continue
        team_group[ta["id"]] = m.get("group"); team_group[tb["id"]] = m.get("group")
        ga, gb, _ = simulate_match(ta, tb, ko=False)
        record(ta, tb, ga, gb, True)

    by_slug = {t["id"]: t for t in teams}
    adv_ids = _determine_advancers(stats, team_group)
    advancers = [by_slug[t] for t in adv_ids if t in by_slug]
    advanced = set(a["id"] for a in advancers)
    # knockout bonuses: credit bonus for each round REACHED, to knockout bucket
    final_round = {a["id"]: "R32" for a in advancers}
    for a in advancers: tk[a["id"]] += BONUS_BY_ROUND["R32"]

    current = list(advancers)
    for rnd, nxt in [("R32", "R16"), ("R16", "QF"), ("QF", "SF"), ("SF", "F"), ("F", "W")]:
        nextr = []
        for i in range(0, len(current), 2):
            if i + 1 >= len(current): nextr.append(current[i]); continue
            ta, tb = current[i], current[i + 1]
            ga, gb, pen = simulate_match(ta, tb, ko=True)
            record(ta, tb, ga, gb, False)
            w, _ = _winner_loser(ta, tb, ga, gb, pen)
            final_round[w["id"]] = nxt
            tk[w["id"]] += BONUS_BY_ROUND.get(nxt, 0)
            nextr.append(w)
        current = nextr

    return tg, tk, pg, pk, advanced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--rosters", type=int, default=80)
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()
    random.seed(args.seed)

    teams, players = load_seed()
    matches = load_match_schedule()
    _, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players: players_by_team[p["teamId"]].append(p)
    pos_by_pid = {p["id"]: p.get("position", "?") for p in players}
    bp_team = {t["id"]: t["basePrice"] for t in teams}
    bp_player = {p["id"]: p["basePrice"] for p in players}
    team_of_player = {p["id"]: p["teamId"] for p in players}

    # ---- Pass A: run sims, store realized per-asset stage points ----
    sims = []
    sum_tk = defaultdict(float); cnt_adv_team = defaultdict(int)
    sum_pk = defaultdict(float); cnt_adv_player = defaultdict(int)
    tot_tg = tot_tk = tot_pg = tot_pk = 0.0
    for _ in range(args.runs):
        tg, tk, pg, pk, adv = run_staged(teams, matches, by_fdid, players_by_team, pos_by_pid)
        sims.append((tg, tk, pg, pk, adv))
        for t in teams:
            tid = t["id"]
            if tid in adv:
                sum_tk[tid] += tk.get(tid, 0); cnt_adv_team[tid] += 1
        for p in players:
            pid = p["id"]
            if team_of_player[pid] in adv:
                sum_pk[pid] += pk.get(pid, 0); cnt_adv_player[pid] += 1
        tot_tg += sum(tg.values()); tot_tk += sum(tk.values())
        tot_pg += sum(pg.values()); tot_pk += sum(pk.values())

    R = args.runs
    # forward-looking MV at post-group window = round(E[remaining]/2), conditional on alive
    mv_team = {t["id"]: (sum_tk[t["id"]] / cnt_adv_team[t["id"]] / 2.0) if cnt_adv_team[t["id"]] else 0.0 for t in teams}
    mv_player = {p["id"]: (sum_pk[p["id"]] / cnt_adv_player[p["id"]] / 2.0) if cnt_adv_player[p["id"]] else 0.0 for p in players}
    buy_team = {k: max(1, round(v * VIG_BUY)) for k, v in mv_team.items()}
    sell_player = {k: max(0, round(v * VIG_SELL)) for k, v in mv_player.items()}

    print("=" * 74)
    print(f"  LEAGUE-WIDE STAGE SPLIT  (mean per tournament, {R} sims)")
    print("=" * 74)
    print(f"  TEAMS  : group {tot_tg/R:6.0f}   knockout {tot_tk/R:6.0f}   -> {tot_tk/(tot_tg+tot_tk):.0%} of team pts are knockout")
    print(f"  PLAYERS: group {tot_pg/R:6.0f}   knockout {tot_pk/R:6.0f}   -> {tot_pk/(tot_pg+tot_pk):.0%} of player pts are knockout")
    print(f"  Group-stage point pool team:player = {tot_tg/(tot_tg+tot_pg):.0%}:{tot_pg/(tot_tg+tot_pg):.0%}")
    print(f"  Full-tourney  point pool team:player = {(tot_tg+tot_tk)/(tot_tg+tot_tk+tot_pg+tot_pk):.0%}:"
          f"{(tot_pg+tot_pk)/(tot_tg+tot_tk+tot_pg+tot_pk):.0%}")

    # ---- random roster builders ----
    def rand_roster(pool_kinds):
        """pool_kinds: list of ('team'|'player', id, price) candidates."""
        for _ in range(200):
            pool = pool_kinds[:]; random.shuffle(pool)
            picks, spent = [], 0
            for kind, id_, price in pool:
                if len(picks) >= ROSTER_CAP: break
                if spent + price <= BUDGET:
                    picks.append((kind, id_)); spent += price
            if spent >= BUDGET - 4 and len(picks) >= 8:   # use most of the budget
                return picks
        return picks

    team_pool   = [("team", t["id"], t["basePrice"]) for t in teams]
    player_pool = [("player", p["id"], p["basePrice"]) for p in players]
    def archetype_rosters(mix):  # mix = fraction players
        out = []
        for _ in range(args.rosters):
            if mix >= 0.99:   pool = player_pool
            elif mix <= 0.01: pool = team_pool
            else:
                # weighted blend: enough of each so the greedy fill lands ~mix
                pool = player_pool * 3 + team_pool * 1 if mix > 0.5 else player_pool * 1 + team_pool * 3
            out.append(rand_roster(pool))
        return out

    drafts = {
        "HOLD_TEAMS":    archetype_rosters(0.0),
        "HOLD_BALANCED": archetype_rosters(0.5),
        "HOLD_PLAYERS":  archetype_rosters(1.0),
    }

    def cost(roster):
        return sum((bp_team if k == "team" else bp_player)[i] for k, i in roster)

    def score_hold(roster, sim):
        tg, tk, pg, pk, adv = sim
        s = 0
        for k, i in roster:
            if k == "team": s += tg.get(i, 0) + tk.get(i, 0)
            else:           s += pg.get(i, 0) + pk.get(i, 0)
        return s

    def group_score(roster, sim):
        tg, tk, pg, pk, adv = sim
        return sum((tg if k == "team" else pg).get(i, 0) for k, i in roster)

    def score_pivot(roster, sim):
        """Player-heavy draft -> auto-sell eliminated, then 3 buys on strongest survivors."""
        tg, tk, pg, pk, adv = sim
        banked = 0
        budget = BUDGET - cost(roster)
        held = []           # picks held since draft: (kind,id)
        for k, i in roster:
            alive = (i in adv) if k == "team" else (team_of_player[i] in adv)
            if not alive:   # auto-sell on elimination: bank group pts + 25% refund
                banked += (tg if k == "team" else pg).get(i, 0)
                budget += round(ELIM_REFUND * (bp_team if k == "team" else bp_player)[i])
            else:
                held.append((k, i))
        # candidate teams to buy: alive, not held, strongest by MV
        held_ids = {i for _, i in held}
        targets = sorted([t["id"] for t in teams if t["id"] in adv and t["id"] not in held_ids],
                         key=lambda tid: mv_team[tid], reverse=True)
        sellable = sorted([(k, i) for (k, i) in held if k == "player"],
                          key=lambda ki: sell_player[ki[1]])   # sell lowest-MV first
        bought = []
        buys = 0
        for tid in targets:
            if buys >= MAX_BUYS: break
            price = buy_team[tid]
            # make room (cap) + funds by selling depleted players
            while (len(held) + len(bought) >= ROSTER_CAP or budget < price) and sellable:
                sk, si = sellable.pop(0)
                if (sk, si) in held:
                    held.remove((sk, si)); banked += pg.get(si, 0); budget += sell_player[si]
            if budget >= price and len(held) + len(bought) < ROSTER_CAP:
                budget -= price; bought.append(tid); buys += 1
            else:
                break
        s = banked
        for k, i in held:   # held since draft -> full (group+knockout)
            s += (tg.get(i, 0) + tk.get(i, 0)) if k == "team" else (pg.get(i, 0) + pk.get(i, 0))
        for tid in bought:  # bought at window -> knockout only (forward)
            s += tk.get(tid, 0)
        return s

    # ---- evaluate ----
    print()
    print("=" * 74)
    print(f"  STRATEGY EV  ({args.rosters} random drafts/archetype x {R} sims)")
    print("=" * 74)
    print(f"  {'strategy':16}{'mean':>7}{'median':>8}{'p25':>7}{'p75':>7}{'avg$':>7}  {'after-group mean':>16}")
    results = {}
    def eval_strategy(name, roster_list, scorer):
        means = []; postg = []; allfinals = []
        for roster in roster_list:
            finals = [scorer(roster, sim) for sim in sims]
            means.append(statistics.mean(finals))
            postg.append(statistics.mean(group_score(roster, sim) for sim in sims))
            allfinals.extend(finals)
        results[name] = allfinals
        avgcost = statistics.mean(cost(r) for r in roster_list)
        print(f"  {name:16}{statistics.mean(means):>7.1f}{statistics.median(allfinals):>8.0f}"
              f"{_pct(allfinals,25):>7.0f}{_pct(allfinals,75):>7.0f}{avgcost:>7.1f}  {statistics.mean(postg):>16.1f}")

    eval_strategy("HOLD_TEAMS",    drafts["HOLD_TEAMS"],    score_hold)
    eval_strategy("HOLD_BALANCED", drafts["HOLD_BALANCED"], score_hold)
    eval_strategy("HOLD_PLAYERS",  drafts["HOLD_PLAYERS"],  score_hold)
    eval_strategy("HARVEST_PIVOT", drafts["HOLD_PLAYERS"],  score_pivot)   # same drafts as HOLD_PLAYERS

    # head-to-head: pivot vs holding the same player-heavy draft, per (roster,sim)
    wins = total = 0; deltas = []
    for roster in drafts["HOLD_PLAYERS"]:
        for sim in sims:
            d = score_pivot(roster, sim) - score_hold(roster, sim)
            deltas.append(d); total += 1; wins += (d > 0)
    print()
    print(f"  PIVOT vs HOLD (same player-heavy draft): pivot wins {wins/total:.0%} of (draft,sim) cases, "
          f"mean edge {statistics.mean(deltas):+.1f} pts (median {statistics.median(deltas):+.0f})")


def _pct(xs, q):
    xs = sorted(xs); k = (len(xs) - 1) * q / 100
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


if __name__ == "__main__":
    main()
