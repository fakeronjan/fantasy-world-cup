"""Pre-launch balance analysis for Fantasy World Cup 2026.

Answers three questions about the locked $60 / 12-pick / Option A configuration:
  Q1 - Optimal draft: do top random rosters converge on the same picks?
  Q2 - Composition: which team/position mixes dominate?
  Q3 - Lock-in vs comeback: is the late tournament still in play?

Approach
--------
1. Run ~300 Monte Carlo tournaments using the existing simulate_2026 engine,
   but forked so we also snapshot per-asset points at the end of the group
   stage (before any knockouts).
2. For each sim we cache per-asset points (TEAM and PLAYER) for both
   "group-stage-only" and "full tournament" timelines.
3. Generate ~500 random VALID rosters (12 picks, sum(basePrice) <= 60,
   mix of teams + players) and score every roster against every sim.
4. Report findings for each question.

Usage
-----
  ./venv/bin/python scripts/analyze_launch_balance.py [--runs 300] [--rosters 500]
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# Reuse the existing engine
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from simulate_2026 import (  # noqa: E402
    ADVANCEMENT_ORDER,
    BONUS_BY_ROUND,
    BUDGET,
    WEIGHTS,
    _determine_advancers,
    _record_match,
    _winner_loser,
    build_team_indexes,
    load_match_schedule,
    load_seed,
    score_asset_points,
    simulate_match,
)

ROSTER_CAP = 12


# ---------------------------------------------------------------------------
# Forked tournament runner that snapshots after group stage
# ---------------------------------------------------------------------------

def run_tournament_with_group_snapshot(teams, matches, by_fdid, players_by_team):
    """Same as simulate_2026.run_tournament but also captures a snapshot of
    team_stats + player counters at the moment the group stage ends.

    Returns (group_snapshot, full_snapshot) where each snapshot is a dict
    with keys: team_stats, player_goals, player_assists, player_wins_played,
    player_cs_played.

    Note: for the group_snapshot we set every team's final_round to "group"
    so the team-bonus accumulator yields 0 advancement bonus. Wins/draws/CS
    accrued during group stage are scored as normal.
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

    # Group-stage matches
    group_matches = [m for m in matches if (m.get("stage") or "GROUP_STAGE") == "GROUP_STAGE"]
    for m in group_matches:
        ta_fd = (m.get("homeTeam") or {}).get("id")
        tb_fd = (m.get("awayTeam") or {}).get("id")
        ta = by_fdid.get(ta_fd)
        tb = by_fdid.get(tb_fd)
        if not (ta and tb):
            continue
        team_group[ta["id"]] = m.get("group")
        team_group[tb["id"]] = m.get("group")
        ga, gb, _ = simulate_match(ta, tb, ko=False)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=True)

    # SNAPSHOT after group stage. Deep-copy the accumulators so later
    # knockout updates don't bleed into the group snapshot.
    group_snapshot = {
        "team_stats": {tid: dict(s) for tid, s in team_stats.items()},
        "player_goals": dict(player_goals),
        "player_assists": dict(player_assists),
        "player_wins_played": dict(player_wins_played),
        "player_cs_played": dict(player_cs_played),
    }

    # Continue into knockouts (same as simulate_2026.run_tournament)
    by_slug = {t["id"]: t for t in teams}
    advancer_ids = _determine_advancers(team_stats, team_group)
    advancers = [by_slug[tid] for tid in advancer_ids if tid in by_slug]
    for t in advancers:
        team_stats[t["id"]]["final_round"] = "R32"

    current = list(advancers)
    losers_at = {}

    # R32 -> R16
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
        team_stats[winner["id"]]["final_round"] = "R16"
        losers_at[loser["id"]] = "R32"
        next_round.append(winner)
    current = next_round

    semis_losers = []
    for round_label, next_label in [("R16", "QF"), ("QF", "SF"), ("SF", "F"), ("F", "W")]:
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
            team_stats[winner["id"]]["final_round"] = next_label
            losers_at[loser["id"]] = round_label
            if round_label == "SF":
                semis_losers.append(loser)
            next_round.append(winner)
        current = next_round

    # 3rd-place
    if len(semis_losers) == 2:
        ta, tb = semis_losers
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        _record_match(ta, tb, ga, gb, team_stats, player_goals, player_assists,
                      player_wins_played, player_cs_played,
                      players_by_team, is_group=False)

    full_snapshot = {
        "team_stats": team_stats,
        "player_goals": dict(player_goals),
        "player_assists": dict(player_assists),
        "player_wins_played": dict(player_wins_played),
        "player_cs_played": dict(player_cs_played),
    }
    return group_snapshot, full_snapshot


# ---------------------------------------------------------------------------
# Roster generation
# ---------------------------------------------------------------------------

def asset_price(asset):
    return asset.get("currentPrice", asset.get("basePrice", 0))


def generate_random_roster(teams, players, rng, max_attempts=2000):
    """Build a valid 12-pick roster: sum(price) <= 60, mix of teams + players.

    Strategy: shuffle the combined pool, walk greedily; if we hit 12 picks
    within budget, return; else reshuffle and retry. Within a few tries this
    almost always succeeds since most assets are cheap ($3-$10).
    """
    all_assets = (
        [("team", t["id"], asset_price(t), t) for t in teams] +
        [("player", p["id"], asset_price(p), p) for p in players]
    )
    for _ in range(max_attempts):
        pool = all_assets[:]
        rng.shuffle(pool)
        picks = []
        used_ids = set()
        spent = 0
        for kind, aid, price, _asset in pool:
            if len(picks) >= ROSTER_CAP:
                break
            key = (kind, aid)
            if key in used_ids:
                continue
            if spent + price > BUDGET:
                continue
            picks.append(key)
            used_ids.add(key)
            spent += price
        if len(picks) == ROSTER_CAP and spent <= BUDGET:
            return picks, spent
    raise RuntimeError("Could not build a valid roster after many attempts")


# ---------------------------------------------------------------------------
# Roster scoring
# ---------------------------------------------------------------------------

def score_roster(picks, team_pts, player_pts):
    total = 0
    for kind, aid in picks:
        if kind == "team":
            total += team_pts.get(aid, 0)
        else:
            total += player_pts.get(aid, 0)
    return total


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def spearman_corr(xs, ys):
    """Spearman rank correlation."""
    def ranks(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx = ranks(xs)
    ry = ranks(ys)
    return pearson_corr(rx, ry)


def pearson_corr(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=300)
    ap.add_argument("--rosters", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260606)
    args = ap.parse_args()

    print(f"=== Fantasy World Cup 2026 - Launch Balance Analysis ===")
    print(f"  runs={args.runs}  rosters={args.rosters}  seed={args.seed}")
    print(f"  budget=${BUDGET}  roster_cap={ROSTER_CAP}")

    rng = random.Random(args.seed)
    # Seed the global random so the tournament sim is also deterministic
    random.seed(args.seed)

    teams, players = load_seed()
    matches = load_match_schedule()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)

    teams_by_id = {t["id"]: t for t in teams}
    players_by_id = {p["id"]: p for p in players}

    print(f"\nLoaded {len(teams)} teams, {len(players)} players, {len(matches)} matches.")

    # ----- Run sims -----
    print(f"\nRunning {args.runs} simulations (with group-stage snapshot)...")
    t0 = time.time()
    sims = []  # list of (team_pts_group, player_pts_group, team_pts_full, player_pts_full)
    for run in range(args.runs):
        if run % 50 == 0 and run > 0:
            elapsed = time.time() - t0
            eta = elapsed * (args.runs - run) / run
            print(f"  [{run}/{args.runs}] elapsed {elapsed:.1f}s, eta {eta:.0f}s")
        gsnap, fsnap = run_tournament_with_group_snapshot(
            teams, matches, by_fdid, players_by_team
        )
        t_pts_g, p_pts_g = score_asset_points(
            teams, gsnap["team_stats"], players_by_team,
            gsnap["player_goals"], gsnap["player_assists"],
            gsnap["player_wins_played"], gsnap["player_cs_played"],
        )
        t_pts_f, p_pts_f = score_asset_points(
            teams, fsnap["team_stats"], players_by_team,
            fsnap["player_goals"], fsnap["player_assists"],
            fsnap["player_wins_played"], fsnap["player_cs_played"],
        )
        sims.append((t_pts_g, p_pts_g, t_pts_f, p_pts_f))
    print(f"  Done in {time.time() - t0:.1f}s")

    # ----- Generate random valid rosters -----
    print(f"\nGenerating {args.rosters} random valid rosters...")
    rosters = []
    costs = []
    t0 = time.time()
    while len(rosters) < args.rosters:
        picks, cost = generate_random_roster(teams, players, rng)
        rosters.append(picks)
        costs.append(cost)
    print(f"  Done in {time.time() - t0:.1f}s. Mean cost ${sum(costs) / len(costs):.1f}")

    # ----- Score every roster against every sim (group + full) -----
    print(f"\nScoring {len(rosters)} rosters x {len(sims)} sims (both timelines)...")
    t0 = time.time()
    full_scores = [[0.0] * len(sims) for _ in rosters]
    group_scores = [[0.0] * len(sims) for _ in rosters]
    for s_idx, (t_pts_g, p_pts_g, t_pts_f, p_pts_f) in enumerate(sims):
        for r_idx, picks in enumerate(rosters):
            full_scores[r_idx][s_idx] = score_roster(picks, t_pts_f, p_pts_f)
            group_scores[r_idx][s_idx] = score_roster(picks, t_pts_g, p_pts_g)
    print(f"  Done in {time.time() - t0:.1f}s")

    full_means = [statistics.mean(s) for s in full_scores]
    group_means = [statistics.mean(s) for s in group_scores]

    # ====================================================================
    # Q1 - Optimal draft analysis
    # ====================================================================
    print("\n" + "=" * 80)
    print("  Q1 - OPTIMAL DRAFT ANALYSIS")
    print("=" * 80)

    ranked = sorted(range(len(rosters)), key=lambda i: -full_means[i])
    top10 = ranked[:10]
    bottom1 = ranked[-1]

    print(f"\n  Best roster mean EP : {full_means[ranked[0]]:.1f}")
    print(f"  10th-best mean EP   : {full_means[ranked[9]]:.1f}")
    print(f"  Worst valid roster  : {full_means[bottom1]:.1f}")
    print(f"  Best - 10th gap     : {full_means[ranked[0]] - full_means[ranked[9]]:.1f}")
    print(f"  Best - worst spread : {full_means[ranked[0]] - full_means[bottom1]:.1f}")
    print(f"  Spread / best ratio : {(full_means[ranked[0]] - full_means[bottom1]) / full_means[ranked[0]] * 100:.0f}%")

    # Meta convergence: how many distinct picks appear across the top 10?
    pick_counter = Counter()
    for i in top10:
        for pick in rosters[i]:
            pick_counter[pick] += 1
    most_common = pick_counter.most_common(15)
    overlap_size = len(pick_counter)  # distinct picks across all top-10
    print(f"\n  Meta convergence:")
    print(f"    Distinct picks across top-10 rosters: {overlap_size} (out of max 120)")
    print(f"    Most-shared picks (count >= 3):")
    shared_count = 0
    for pick, count in most_common:
        if count >= 3:
            shared_count += 1
            kind, aid = pick
            asset = teams_by_id[aid] if kind == "team" else players_by_id[aid]
            label = asset.get("name", aid)
            price = asset_price(asset)
            print(f"      {count:>2}/10  {kind:<6}  ${price:>2}  {label}")
    if shared_count == 0:
        print(f"      (none - top rosters share no asset 3+ times)")

    # ====================================================================
    # Q2 - Composition mix
    # ====================================================================
    print("\n" + "=" * 80)
    print("  Q2 - COMPOSITION / POSITION MIX")
    print("=" * 80)

    # Bin by (teams, GK, DEF, MID, FWD)
    bin_scores = defaultdict(list)
    for i, picks in enumerate(rosters):
        team_n = sum(1 for kind, _ in picks if kind == "team")
        gk_n = sum(1 for kind, aid in picks
                    if kind == "player" and players_by_id[aid].get("position") == "GK")
        def_n = sum(1 for kind, aid in picks
                    if kind == "player" and players_by_id[aid].get("position") == "DEF")
        mid_n = sum(1 for kind, aid in picks
                    if kind == "player" and players_by_id[aid].get("position") == "MID")
        fwd_n = sum(1 for kind, aid in picks
                    if kind == "player" and players_by_id[aid].get("position") == "FWD")
        key = (team_n, gk_n, def_n, mid_n, fwd_n)
        bin_scores[key].append(full_means[i])

    bin_stats = []
    for key, vals in bin_scores.items():
        if len(vals) >= 10:
            bin_stats.append((key, len(vals), statistics.mean(vals)))
    bin_stats.sort(key=lambda x: -x[2])

    print(f"\n  Full 5-tuple bins (teams/GK/DEF/MID/FWD) with >=10 rosters: {len(bin_stats)}")
    if bin_stats:
        print(f"    {'T':>3} {'GK':>3} {'DEF':>4} {'MID':>4} {'FWD':>4}   {'n':>4}  {'mean EP':>8}")
        for key, n, mean in bin_stats[:10]:
            t, gk, d, m, f = key
            print(f"    {t:>3} {gk:>3} {d:>4} {m:>4} {f:>4}   {n:>4}  {mean:>8.1f}")

    # Coarser bins: by team_count alone, and by team_count + FWD_count
    by_team_count = defaultdict(list)
    by_team_fwd = defaultdict(list)
    for i, picks in enumerate(rosters):
        team_n = sum(1 for kind, _ in picks if kind == "team")
        fwd_n = sum(1 for kind, aid in picks
                     if kind == "player" and players_by_id[aid].get("position") == "FWD")
        by_team_count[team_n].append(full_means[i])
        by_team_fwd[(team_n, fwd_n)].append(full_means[i])

    print(f"\n  Mean EP by team_count (n >= 10):")
    print(f"    {'teams':>5}  {'n':>4}  {'mean':>7}  {'p25':>6}  {'p75':>6}")
    for t_n in sorted(by_team_count.keys()):
        vals = by_team_count[t_n]
        if len(vals) < 10:
            continue
        s = sorted(vals)
        print(f"    {t_n:>5}  {len(vals):>4}  {statistics.mean(vals):>7.1f}  "
               f"{s[len(s)//4]:>6.1f}  {s[(3*len(s))//4]:>6.1f}")

    fwd_bins = [(key, vals) for key, vals in by_team_fwd.items() if len(vals) >= 10]
    fwd_bins.sort(key=lambda x: -statistics.mean(x[1]))
    print(f"\n  Mean EP by (team_count, FWD_count) with n >= 10 - top 10:")
    print(f"    {'T':>3} {'FWD':>4}  {'n':>4}  {'mean EP':>8}")
    for (t_n, fwd_n), vals in fwd_bins[:10]:
        print(f"    {t_n:>3} {fwd_n:>4}  {len(vals):>4}  {statistics.mean(vals):>8.1f}")
    print(f"  Bottom 5:")
    for (t_n, fwd_n), vals in fwd_bins[-5:]:
        print(f"    {t_n:>3} {fwd_n:>4}  {len(vals):>4}  {statistics.mean(vals):>8.1f}")

    # Special checks:
    print(f"\n  Special checks (means of bins, ignoring n>=10 cutoff if small):")
    # All teams vs all players
    all_team_rosters = [i for i, picks in enumerate(rosters)
                         if sum(1 for k, _ in picks if k == "team") == 12]
    all_player_rosters = [i for i, picks in enumerate(rosters)
                         if sum(1 for k, _ in picks if k == "team") == 0]
    if all_team_rosters:
        m = statistics.mean(full_means[i] for i in all_team_rosters)
        print(f"    All teams      ({len(all_team_rosters)} rosters): mean EP {m:.1f}")
    else:
        print(f"    All teams: 0 rosters (random sampling didn't hit 12-team mix at budget)")
    if all_player_rosters:
        m = statistics.mean(full_means[i] for i in all_player_rosters)
        print(f"    All players    ({len(all_player_rosters)} rosters): mean EP {m:.1f}")
    else:
        print(f"    All players: 0 rosters")

    # "No GK" rosters
    no_gk_rosters = [i for i, picks in enumerate(rosters)
                       if all(not (k == "player" and players_by_id[a].get("position") == "GK")
                              for k, a in picks)]
    if no_gk_rosters:
        m = statistics.mean(full_means[i] for i in no_gk_rosters)
        print(f"    No GK          ({len(no_gk_rosters)} rosters): mean EP {m:.1f}")
    with_gk_rosters = [i for i in range(len(rosters)) if i not in set(no_gk_rosters)]
    if with_gk_rosters:
        m = statistics.mean(full_means[i] for i in with_gk_rosters)
        print(f"    With >=1 GK    ({len(with_gk_rosters)} rosters): mean EP {m:.1f}")

    # DEF-heavy vs DEF-light - compare top quartile of winners to bottom quartile by DEF count
    winners_top_q = ranked[: len(ranked) // 4]
    losers_bot_q = ranked[-len(ranked) // 4:]
    def avg_pos_count(idxs, pos):
        return statistics.mean(
            sum(1 for k, a in rosters[i]
                if k == "player" and players_by_id[a].get("position") == pos)
            for i in idxs
        )
    def avg_team_count(idxs):
        return statistics.mean(
            sum(1 for k, _ in rosters[i] if k == "team") for i in idxs
        )
    print(f"\n  Top-quartile (winners) avg composition:")
    print(f"    teams={avg_team_count(winners_top_q):.2f}  GK={avg_pos_count(winners_top_q, 'GK'):.2f}  "
          f"DEF={avg_pos_count(winners_top_q, 'DEF'):.2f}  "
          f"MID={avg_pos_count(winners_top_q, 'MID'):.2f}  "
          f"FWD={avg_pos_count(winners_top_q, 'FWD'):.2f}")
    print(f"  Bottom-quartile (losers) avg composition:")
    print(f"    teams={avg_team_count(losers_bot_q):.2f}  GK={avg_pos_count(losers_bot_q, 'GK'):.2f}  "
          f"DEF={avg_pos_count(losers_bot_q, 'DEF'):.2f}  "
          f"MID={avg_pos_count(losers_bot_q, 'MID'):.2f}  "
          f"FWD={avg_pos_count(losers_bot_q, 'FWD'):.2f}")

    # ====================================================================
    # Q3 - Lock-in vs comeback
    # ====================================================================
    print("\n" + "=" * 80)
    print("  Q3 - LOCK-IN vs COMEBACK")
    print("=" * 80)

    # Spearman correlation between group-stage-only and full-tournament
    # roster scores. Compute per-sim and average; also compute on the means.
    per_sim_rho = []
    for s_idx in range(len(sims)):
        xs = [group_scores[r][s_idx] for r in range(len(rosters))]
        ys = [full_scores[r][s_idx] for r in range(len(rosters))]
        per_sim_rho.append(spearman_corr(xs, ys))
    rho_on_means = spearman_corr(group_means, full_means)

    print(f"\n  Spearman rho (group-only roster pts vs full-tournament roster pts):")
    print(f"    Mean rho across sims : {statistics.mean(per_sim_rho):.3f}")
    print(f"    Median rho           : {statistics.median(per_sim_rho):.3f}")
    print(f"    Min / max rho        : {min(per_sim_rho):.3f} / {max(per_sim_rho):.3f}")
    print(f"    Rho on cross-sim means: {rho_on_means:.3f}")

    # Fraction of total tournament points scored during group stage
    # (averaged across sims). Use the all-asset total since not every asset
    # is in every roster.
    group_fracs = []
    for s_idx, (t_pts_g, p_pts_g, t_pts_f, p_pts_f) in enumerate(sims):
        total_group = sum(t_pts_g.values()) + sum(p_pts_g.values())
        total_full = sum(t_pts_f.values()) + sum(p_pts_f.values())
        if total_full > 0:
            group_fracs.append(total_group / total_full)
    print(f"\n  Group-stage share of total tournament points (asset universe):")
    print(f"    Mean : {statistics.mean(group_fracs) * 100:.1f}%")
    print(f"    p25  : {sorted(group_fracs)[len(group_fracs) // 4] * 100:.1f}%")
    print(f"    p75  : {sorted(group_fracs)[(3 * len(group_fracs)) // 4] * 100:.1f}%")

    # Same fraction but restricted to the 500 rosters' scores (more
    # representative of what contestants actually feel)
    roster_group_fracs = []
    for s_idx in range(len(sims)):
        total_group = sum(group_scores[r][s_idx] for r in range(len(rosters)))
        total_full = sum(full_scores[r][s_idx] for r in range(len(rosters)))
        if total_full > 0:
            roster_group_fracs.append(total_group / total_full)
    print(f"\n  Group-stage share of points across the 500 rosters:")
    print(f"    Mean : {statistics.mean(roster_group_fracs) * 100:.1f}%")
    print(f"    p25  : {sorted(roster_group_fracs)[len(roster_group_fracs) // 4] * 100:.1f}%")
    print(f"    p75  : {sorted(roster_group_fracs)[(3 * len(roster_group_fracs)) // 4] * 100:.1f}%")

    print("\n=== End of analysis ===")


if __name__ == "__main__":
    main()
