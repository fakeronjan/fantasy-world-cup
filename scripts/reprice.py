"""Forward-looking market reprice for the transfer market.

Runs after each round transition (group → R32 → R16 → QF → SF → F). For
every surviving asset, warm-starts the Monte Carlo from the current
bracket state, runs N trials, and computes:

  marketValue = round(mean(future_pts) / TARGET_ROI)  # whole dollars
  buyPrice    = round(marketValue * 1.10)             # 10% premium, rounded
  sellPrice   = round(marketValue * 0.90)             # 10% discount, rounded
  # …with a forced minimum \$1 spread on either side so the market always
  # has a real bid-ask gap even at low MVs.

Eliminated assets settle at:
  marketValue = 0
  sellPrice   = round(last_marketValue * 0.25)        # 25% liquidation
  buyPrice    = N/A (cannot buy eliminated assets)

This is the *pricing engine* only — Firestore I/O and UI wiring come in
the next pass. Run --dry-run to see prices for a hypothetical state.

Usage:
  # Dry-run (hypothetical state from seed prices):
  ./venv/bin/python scripts/reprice.py --from-round R32 --runs 1000

  # Live (read advancers from Firestore + write prices back):
  GOOGLE_APPLICATION_CREDENTIALS=...sa.json \\
    ./venv/bin/python scripts/reprice.py --from-round R32 --write

Roadmap:
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
ELIM_REFUND_RATE = 0.25   # 25% liquidation on eliminated picks
                          # (Lower than it intuitively sounds because most of
                          # an asset's value has already been realized as
                          # points before it's eliminated — refund is a small
                          # consolation, not a recovery.)


def round_half_up(x: float) -> int:
    """Round to nearest integer, halves go UP. (Python's default round()
    uses banker's rounding which surprises non-engineers — e.g. 2.5→2.
    For a fantasy game UI, we want 2.5→3 always.)"""
    return math.floor(x + 0.5)


def derive_market_prices(market_value: int) -> tuple[int, int]:
    """Compute (buyPrice, sellPrice) from a rounded marketValue.
    Both are rounded to whole dollars from MV ± 10% vig, with a forced
    minimum $1 spread either side so the market never has a zero gap."""
    buy  = max(market_value + 1, round_half_up(market_value * VIG_BUY))
    sell = max(1, min(market_value - 1, round_half_up(market_value * VIG_SELL)))
    return buy, sell


def liquidation_price(last_market_value: int) -> int:
    """Refund paid out when an asset is eliminated. Rounded to whole dollar."""
    return max(0, round_half_up(last_market_value * ELIM_REFUND_RATE))


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
        market_value = max(1, round_half_up(mean_fp / TARGET_ROI))
        buy, sell = derive_market_prices(market_value)
        prices[t["id"]] = {
            "kind": "team",
            "name": t["name"],
            "meanFuturePoints": round(mean_fp, 2),
            "marketValue": market_value,
            "buyPrice":    buy,
            "sellPrice":   sell,
        }

    for tid, ps in players_by_team.items():
        if tid not in alive_ids:
            continue
        for p in ps:
            mean_fp = player_acc[p["id"]] / runs
            market_value = max(1, round_half_up(mean_fp / TARGET_ROI))
            buy, sell = derive_market_prices(market_value)
            prices[p["id"]] = {
                "kind": "player",
                "name": p["name"],
                "team": tid,
                "meanFuturePoints": round(mean_fp, 2),
                "marketValue": market_value,
                "buyPrice":    buy,
                "sellPrice":   sell,
            }

    return prices


def hypothetical_advancers(teams, start_round):
    """For dry-runs, pick a plausible field of advancers based on team
    price (proxy for strength). Top N teams advance to each round."""
    sorted_teams = sorted(teams, key=lambda t: -t["basePrice"])
    n_by_round = {"R32": 32, "R16": 16, "QF": 8, "SF": 4, "F": 2, "W": 1}
    return sorted_teams[:n_by_round[start_round]]


def live_advancers(db, start_round):
    """Read real advancers from Firestore. A team is alive at `start_round`
    iff eliminated=False AND final_round == start_round."""
    advancers = []
    for tdoc in db.collection("teams").stream():
        t = tdoc.to_dict() or {}
        if t.get("eliminated"):
            continue
        if t.get("finalRound") != start_round:
            continue
        advancers.append({**t, "id": tdoc.id})
    return advancers


def write_prices_to_firestore(db, prices, advancers, all_teams, round_label):
    """Persist marketValue/buyPrice/sellPrice to each team + player doc.
    Eliminated assets get marketValue=0 + sellPrice = liquidationValue.

    liquidationValue is captured ONCE per asset on first detection of
    elimination — subsequent reruns leave it alone. This guarantees that
    auto-sell refunds users the same amount no matter how many times we
    reprice after an elimination event.

    Also appends to priceHistory[] on each asset (one entry per round) so
    the roster page can show each held pick's price arc over time.
    """
    alive_ids = {t["id"] for t in advancers}

    # Teams
    teams_batch = db.batch()
    n_alive = n_elim_new = n_elim_existing = 0
    for tdoc in db.collection("teams").stream():
        tid = tdoc.id
        t = tdoc.to_dict() or {}
        if tid in prices:
            p = prices[tid]
            new_hist = list(t.get("priceHistory") or [])
            # Append a snapshot for this round, unless we already have one
            # (idempotent across reruns within the same round).
            if not any(h.get("round") == round_label for h in new_hist):
                new_hist.append({
                    "round":       round_label,
                    "marketValue": p["marketValue"],
                    "buyPrice":    p["buyPrice"],
                    "sellPrice":   p["sellPrice"],
                })
            teams_batch.set(tdoc.reference, {
                "marketValue":      p["marketValue"],
                "buyPrice":         p["buyPrice"],
                "sellPrice":        p["sellPrice"],
                "currentPrice":     p["marketValue"],  # back-compat alias
                "meanFuturePoints": p["meanFuturePoints"],
                "priceHistory":     new_hist,
            }, merge=True)
            n_alive += 1
        elif t.get("liquidationValue") is not None:
            n_elim_existing += 1
        else:
            # Newly eliminated — capture liquidation value FROM LAST KNOWN MV
            # and append a final priceHistory entry marking the exit.
            last_mv = t.get("marketValue") or t.get("currentPrice") or t.get("basePrice", 0)
            liquidation = liquidation_price(last_mv)
            new_hist = list(t.get("priceHistory") or [])
            if not any(h.get("round") == f"{round_label}-out" for h in new_hist):
                new_hist.append({
                    "round":       f"{round_label}-out",
                    "marketValue": 0,
                    "buyPrice":    None,
                    "sellPrice":   liquidation,
                })
            teams_batch.set(tdoc.reference, {
                "marketValue":      0,
                "liquidationValue": liquidation,   # frozen; never updated again
                "buyPrice":         None,
                "sellPrice":        liquidation,
                "currentPrice":     0,
                "priceHistory":     new_hist,
            }, merge=True)
            n_elim_new += 1
    teams_batch.commit()
    print(f"  teams: {n_alive} repriced, {n_elim_new} newly eliminated, {n_elim_existing} already-eliminated")

    # Players (inherit team's alive/eliminated state)
    players_batch = db.batch()
    n_p_alive = n_p_elim_new = n_p_elim_existing = 0
    for pdoc in db.collection("players").stream():
        pid = pdoc.id
        p = pdoc.to_dict() or {}
        if pid in prices:
            pr = prices[pid]
            new_hist = list(p.get("priceHistory") or [])
            if not any(h.get("round") == round_label for h in new_hist):
                new_hist.append({
                    "round":       round_label,
                    "marketValue": pr["marketValue"],
                    "buyPrice":    pr["buyPrice"],
                    "sellPrice":   pr["sellPrice"],
                })
            players_batch.set(pdoc.reference, {
                "marketValue":      pr["marketValue"],
                "buyPrice":         pr["buyPrice"],
                "sellPrice":        pr["sellPrice"],
                "currentPrice":     pr["marketValue"],
                "meanFuturePoints": pr["meanFuturePoints"],
                "priceHistory":     new_hist,
            }, merge=True)
            n_p_alive += 1
        elif p.get("liquidationValue") is not None:
            n_p_elim_existing += 1
        else:
            last_mv = p.get("marketValue") or p.get("currentPrice") or p.get("basePrice", 0)
            liquidation = liquidation_price(last_mv)
            new_hist = list(p.get("priceHistory") or [])
            if not any(h.get("round") == f"{round_label}-out" for h in new_hist):
                new_hist.append({
                    "round":       f"{round_label}-out",
                    "marketValue": 0,
                    "buyPrice":    None,
                    "sellPrice":   liquidation,
                })
            players_batch.set(pdoc.reference, {
                "marketValue":      0,
                "liquidationValue": liquidation,
                "buyPrice":         None,
                "sellPrice":        liquidation,
                "currentPrice":     0,
                "eliminated":       True,
                "priceHistory":     new_hist,
            }, merge=True)
            n_p_elim_new += 1
    players_batch.commit()
    print(f"  players: {n_p_alive} repriced, {n_p_elim_new} newly eliminated, {n_p_elim_existing} already-eliminated")


def snapshot_user_values(db, round_label):
    """After repricing, snapshot every user's total roster $-value into
    user.valueByRound[round_label] = currentBudget + sum(currentPrice of held picks).

    Used by the roster page to render a "total value over time" chart.
    Idempotent: overwrites the value for this round_label if rerun."""
    teams_cache = {d.id: d.to_dict() for d in db.collection("teams").stream()}
    players_cache = {d.id: d.to_dict() for d in db.collection("players").stream()}

    n_users = 0
    for udoc in db.collection("users").stream():
        u = udoc.to_dict() or {}
        roster = u.get("roster") or []
        budget = u.get("currentBudget") or 0
        held_value = 0
        for pick in roster:
            cache = teams_cache if pick["kind"] == "team" else players_cache
            asset = cache.get(pick["assetId"]) or {}
            held_value += int(asset.get("currentPrice") or 0)
        total = int(budget) + held_value

        vbr = dict(u.get("valueByRound") or {})
        vbr[round_label] = total
        udoc.reference.set({"valueByRound": vbr}, merge=True)
        n_users += 1
    print(f"  snapshot: wrote valueByRound[{round_label}] for {n_users} users")


def auto_sell_eliminated_picks(db):
    """For every user, remove picks that reference an eliminated asset and
    credit currentBudget with the asset's liquidationValue. Records each
    auto-sell as a transaction row so users can see what happened.

    Idempotent: a pick is only auto-sold once because removing it from the
    roster means there's nothing to process on the next run."""
    from datetime import datetime, timezone

    # Build lookup of all eliminated assets and their liquidation values
    elim_teams = {}
    for tdoc in db.collection("teams").stream():
        t = tdoc.to_dict() or {}
        if t.get("eliminated") or t.get("marketValue") == 0:
            elim_teams[tdoc.id] = t.get("liquidationValue", 0)
    elim_players = {}
    for pdoc in db.collection("players").stream():
        p = pdoc.to_dict() or {}
        if p.get("eliminated") or p.get("marketValue") == 0:
            elim_players[pdoc.id] = p.get("liquidationValue", 0)

    n_users_affected = 0
    n_picks_sold = 0
    total_refund_paid = 0
    now = datetime.now(timezone.utc).isoformat()

    for udoc in db.collection("users").stream():
        u = udoc.to_dict() or {}
        roster = u.get("roster") or []
        if not roster:
            continue

        eliminated_picks = []
        surviving_picks = []
        for pick in roster:
            elim_map = elim_teams if pick["kind"] == "team" else elim_players
            if pick["assetId"] in elim_map:
                eliminated_picks.append(pick)
            else:
                surviving_picks.append(pick)

        if not eliminated_picks:
            continue

        # Compute total refund
        sells_record = []
        total_refund = 0
        for pick in eliminated_picks:
            elim_map = elim_teams if pick["kind"] == "team" else elim_players
            refund = elim_map[pick["assetId"]] or 0
            total_refund += refund
            sells_record.append({
                "kind":      pick["kind"],
                "assetId":   pick["assetId"],
                "paidPrice": pick.get("purchasePrice", 0),
                "soldAt":    refund,
                "reason":    "auto-sell-elimination",
            })

        new_budget = (u.get("currentBudget") or 0) + total_refund
        udoc.reference.set({
            "roster":        surviving_picks,
            "currentBudget": new_budget,
        }, merge=True)

        tx_ref = db.collection("transactions").document()
        tx_ref.set({
            "uid":       udoc.id,
            "round":     "auto-sell",
            "timestamp": now,
            "sells":     sells_record,
            "buys":      [],
            "type":      "auto-sell-elimination",
        })

        n_users_affected += 1
        n_picks_sold += len(eliminated_picks)
        total_refund_paid += total_refund

    print(f"  auto-sell: dropped {n_picks_sold} eliminated picks across {n_users_affected} users (${total_refund_paid} total refunds)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-round", default="R32",
                    choices=["R32", "R16", "QF", "SF", "F"])
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top", type=int, default=20,
                    help="Show top N priced assets in dry-run output")
    ap.add_argument("--write", action="store_true",
                    help="Read live state from Firestore + persist prices back")
    ap.add_argument("--skip-auto-sell", action="store_true",
                    help="With --write, skip the auto-sell step that removes "
                         "eliminated picks from user rosters and refunds "
                         "liquidation values to currentBudget. Useful for "
                         "price-only repricing without touching users.")
    args = ap.parse_args()

    teams, players = load_seed()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)
    print(f"Loaded {len(teams)} teams, {len(players)} players from seed")
    print(f"Warm-start round: {args.from_round}, runs: {args.runs}\n")

    # Live mode: read real advancers from Firestore.
    # Dry-run mode: synthesize advancers from seed.
    db = None
    if args.write:
        from _fwc_lib import firestore_client
        db = firestore_client()
        advancers = live_advancers(db, args.from_round)
        if not advancers:
            print(f"No teams with finalRound={args.from_round} in Firestore. "
                  f"Has the previous round been ingested? Aborting.")
            return
        print(f"Live mode: {len(advancers)} teams advanced to {args.from_round}")
    else:
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

    if args.write and db is not None:
        print(f"\nWriting prices to Firestore...")
        write_prices_to_firestore(db, prices, advancers, teams, args.from_round)
        if not args.skip_auto_sell:
            print(f"Auto-selling eliminated picks from user rosters...")
            auto_sell_eliminated_picks(db)
        # Snapshot user totals AFTER auto-sell so the chart reflects the
        # post-elimination state (refund credited, dead picks removed).
        print(f"Snapshotting user roster values for round {args.from_round}...")
        snapshot_user_values(db, args.from_round)
        print("Done.")
    elif not args.write:
        print(f"\n[Dry-run only. Pass --write to persist to Firestore.]")


if __name__ == "__main__":
    main()
