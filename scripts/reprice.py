"""Forward-looking market reprice for the transfer market.

Runs after each round transition (group → R32 → R16 → QF → SF → F). For
every surviving asset, warm-starts the Monte Carlo from the current
bracket state, runs N trials, and computes:

  marketValue = round(mean(future_pts) / TARGET_ROI)  # whole dollars
  buyPrice    = round(marketValue * 1.10)             # 10% premium, rounded
  sellPrice   = round(marketValue * 0.90)             # 10% discount, rounded
  # …with a forced minimum \$1 spread on either side so the market always
  # has a real bid-ask gap even at low MVs.

A player's forward value is his EXPECTED GOALS/GAME x his expected remaining
appearances (which grows with how deep his team is projected to run). Goals/game
is an empirical-Bayes blend of the preseason prior (position x basePrice) with
his OWN group-stage goal COUNT - see build_player_rates + RATE_PRIOR_GAMES. There
is no team-share and no cap, so a prolific scorer is rewarded for the number of
goals he scored, not a fraction of his team's total. Assists use the prior rate
only; win-share + clean-sheet points come from the simulated lineups.

Eliminated assets settle at:
  marketValue = 0
  buyPrice    = N/A (cannot buy eliminated assets)
  Auto-sell refund = 25% of each holder's PURCHASE PRICE, applied per-pick in
  auto_sell_eliminated_picks(). (The per-asset sellPrice still written below is
  a legacy display field and no longer drives the refund amount.)

This is the *pricing engine* only - Firestore I/O and UI wiring come in
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
    WEIGHTS, BONUS_BY_ROUND, ADVANCEMENT_ORDER, POS_WEIGHTS, ASSIST_WEIGHTS,
    simulate_match, _winner_loser, pick_lineup,
    load_seed, build_team_indexes,
)
from _fwc_lib import (
    advancer_slugs_for_round, round_fully_seeded,
    eliminated_slugs as compute_eliminated_slugs,
)

# Forward-looking pricing constants
TARGET_ROI       = 2.0    # target points per dollar (cap-anchored)
VIG_BUY          = 1.10   # buy at 10% premium
VIG_SELL         = 0.90   # sell at 10% discount
ELIM_REFUND_RATE = 0.25   # eliminated picks refund 25% of PURCHASE PRICE
                          # (what each holder paid), not market value. Chosen
                          # for clarity + per-user fairness: "get 25% of what
                          # you paid back." Applied per-pick in
                          # auto_sell_eliminated_picks().

# --- Per-player rate pricing (results-to-date drive forward player value) ---
# A player's forward value is his EXPECTED GOALS/GAME x his expected remaining
# appearances (which grows with how far his team is projected to advance). Goals/
# game is an empirical-Bayes blend of the preseason prior (position x basePrice)
# and his OWN group-stage goal COUNT -- NOT a share of his team's goals. So a
# prolific scorer is rewarded for the number of goals he scored, never capped at
# a fraction of the team total. Assists use the prior rate only (the in-tournament
# signal we trust is goals); win-share + clean-sheet points still come from the
# simulated lineups/results.
#
#   goals/game_i   = (group_goals_i + m * prior_gpg_i) / (GROUP_GAMES + m)
#   assists/game_i = prior_apg_i
#   prior_gpg_i    = POS_WEIGHTS[pos] * basePrice_i * GOAL_PRIOR_SCALE
#   prior_apg_i    = ASSIST_WEIGHTS[pos] * basePrice_i * ASSIST_PRIOR_SCALE
GROUP_GAMES        = 3      # group matches per team = the observed window
RATE_PRIOR_GAMES   = 3.0    # EB strength of the preseason prior, in pseudo-games
                            # (higher -> trust basePrice; lower -> trust this
                            # tournament's goals). Exposed via --form-m.
GOAL_PRIOR_SCALE   = 0.020  # (POS_WEIGHTS x basePrice) -> prior goals/game
ASSIST_PRIOR_SCALE = 0.015  # (ASSIST_WEIGHTS x basePrice) -> prior assists/game
FORM_M             = RATE_PRIOR_GAMES   # back-compat alias for the CLI default


def round_half_up(x: float) -> int:
    """Round to nearest integer, halves go UP. (Python's default round()
    uses banker's rounding which surprises non-engineers - e.g. 2.5→2.
    For a fantasy game UI, we want 2.5→3 always.)"""
    return math.floor(x + 0.5)


def derive_market_prices(mean_fp: float, market_value: int) -> tuple[int, int]:
    """Compute (buyPrice, sellPrice) from EXPECTED POINTS directly, so
    points-per-dollar stays flat across the whole price range.

    The +/-10% vig is applied in the POINTS domain (buy = xPts*1.1/ROI, sell =
    xPts*0.9/ROI) and only then rounded. The old version added a flat +/-$1 to
    the rounded marketValue, which was a ~50% markup on a $2 asset but ~11% on a
    $9 one - so cheap assets were value traps and premium assets the only good
    buy. Applying the vig before rounding removes that bias. Clamp so
    buy >= marketValue >= sell >= 1; the cheapest assets may collapse to a zero
    spread, which is an acceptable edge for $1-2 fillers."""
    buy  = max(market_value, round_half_up(mean_fp * VIG_BUY  / TARGET_ROI))
    sell = max(1, min(market_value, round_half_up(mean_fp * VIG_SELL / TARGET_ROI)))
    return buy, sell


def liquidation_price(last_market_value: int) -> int:
    """Refund paid out when an asset is eliminated. Rounded to whole dollar."""
    return max(0, round_half_up(last_market_value * ELIM_REFUND_RATE))


def build_player_rates(players_by_team, goals_by_id, m=RATE_PRIOR_GAMES):
    """Per-player expected goals/game and assists/game.

    goals/game = (group_goals + m * prior_gpg) / (GROUP_GAMES + m)   [EB blend]
    assists/game = prior_apg                                         [prior only]

    Both priors come from the preseason model (POS_WEIGHTS / ASSIST_WEIGHTS x
    basePrice). There is NO team-share normalization and NO cap: a player's own
    goal COUNT drives his rate, so a prolific scorer outranks a teammate-diluted
    one. Pass an empty goals_by_id to recover the pure-prior rates (dry runs)."""
    gpg, apg = {}, {}
    for tid, squad in players_by_team.items():
        for p in squad:
            pid = p["id"]
            pos = p.get("position", "?")
            base = max(1, p.get("basePrice", 1))
            prior_gpg = POS_WEIGHTS.get(pos, 1.0) * base * GOAL_PRIOR_SCALE
            obs = goals_by_id.get(pid, 0) or 0
            gpg[pid] = (obs + m * prior_gpg) / (GROUP_GAMES + m)
            apg[pid] = ASSIST_WEIGHTS.get(pos, 1.0) * base * ASSIST_PRIOR_SCALE
    return gpg, apg


def simulate_remaining(advancers, start_round, players_by_team):
    """Simulate from `start_round` onward, treating `advancers` as the field at
    that round. Tracks per-player APPEARANCES (games played), win-share games,
    and clean-sheet games for FUTURE matches only (group stage skipped). Goals
    and assists are NOT simulated here - they're computed analytically from each
    player's rate x his appearances in score_future_points(), which is the whole
    point of the rate model. Match outcomes + lineups draw the main `random`
    stream, so team prices stay pinned regardless of the player rate settings."""
    team_stats = {t["id"]: dict(
        wins=0, draws=0, losses=0,
        goals_for=0, goals_against=0,
        clean_sheets=0,
        final_round=start_round,
        group_pts=0, group_gd=0, group_gf=0,
    ) for t in advancers}
    player_apps        = defaultdict(int)
    player_wins_played = defaultdict(int)
    player_cs_played   = defaultdict(int)

    def record(ta, tb, ga, gb):
        sa, sb = team_stats[ta["id"]], team_stats[tb["id"]]
        sa["goals_for"] += ga; sa["goals_against"] += gb
        sb["goals_for"] += gb; sb["goals_against"] += ga
        a_won, b_won = ga > gb, gb > ga
        if a_won:   sa["wins"] += 1; sb["losses"] += 1
        elif b_won: sb["wins"] += 1; sa["losses"] += 1
        else:       sa["draws"] += 1; sb["draws"] += 1
        cs_a, cs_b = (gb == 0), (ga == 0)
        lineup_a = pick_lineup(ta, players_by_team)
        lineup_b = pick_lineup(tb, players_by_team)
        for pid in lineup_a:
            player_apps[pid] += 1
            if a_won: player_wins_played[pid] += 1
            if cs_a:  player_cs_played[pid]   += 1
        for pid in lineup_b:
            player_apps[pid] += 1
            if b_won: player_wins_played[pid] += 1
            if cs_b:  player_cs_played[pid]   += 1

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
            record(ta, tb, ga, gb)
            team_stats[winner["id"]]["final_round"] = next_label
            if round_label == "SF":
                semis_losers.append(loser)
            next_round.append(winner)
        current = next_round

    # 3rd-place match (real WC has one - counts for player stats)
    if len(semis_losers) == 2:
        ta, tb = semis_losers
        ga, gb, pen = simulate_match(ta, tb, ko=True)
        record(ta, tb, ga, gb)

    return {
        "team_stats":         team_stats,
        "player_apps":        dict(player_apps),
        "player_wins_played": dict(player_wins_played),
        "player_cs_played":   dict(player_cs_played),
    }


def score_future_points(result, advancers, start_round, players_by_team,
                        gpg, apg):
    """Compute future-points for each surviving asset.

    Players: goals = rate x appearances (goals/game * games played this run),
    same for assists; win-share + clean-sheet from the simulated lineups. This
    is the rate model: a player's goal points scale with HIS expected goals/game
    and how many games his team is projected to play, never a share of the team.

    Bonuses for `start_round` itself are NOT counted (those were earned by
    advancing into that round, before this transfer window). Only bonuses for
    rounds strictly after start_round count as future."""
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
    apps = result["player_apps"]
    for tid, ps in players_by_team.items():
        if tid not in alive_ids:
            continue
        for p in ps:
            pid = p["id"]
            pos = p.get("position")
            if   pos == "GK":  cs_rate = WEIGHTS["player_clean_sheet_gk"]
            elif pos == "DEF": cs_rate = WEIGHTS["player_clean_sheet_def"]
            else:              cs_rate = WEIGHTS["player_clean_sheet_other"]
            games = apps.get(pid, 0)
            player_pts[pid] = (
                WEIGHTS["player_goal"]       * gpg.get(pid, 0.0) * games
                + WEIGHTS["player_assist"]    * apg.get(pid, 0.0) * games
                + WEIGHTS["player_win_share"] * result["player_wins_played"].get(pid, 0)
                + cs_rate                     * result["player_cs_played"].get(pid, 0)
            )
    return team_pts, player_pts


def reprice(advancers, start_round, players_by_team, runs=1000, seed=42,
            goals_by_id=None, assists_by_id=None, form_m=FORM_M):
    """Run N trials. Return per-asset price dict:
       {asset_id: {kind, name, meanFuturePoints, marketValue, buyPrice, sellPrice}}.

    goals_by_id keys player ids to group-stage goals; it drives each player's
    expected goals/game (empirical-Bayes blend with the preseason prior).
    assists_by_id is accepted for back-compat but unused - assists use the prior
    rate only (the in-tournament signal we trust is goals). Omit goals_by_id for
    a pure-prior dry-run."""
    random.seed(seed)

    gpg, apg = build_player_rates(players_by_team, goals_by_id or {}, m=form_m)

    team_acc   = defaultdict(float)
    player_acc = defaultdict(float)

    for _ in range(runs):
        # Bracket pairings are FIFA-determined in real life. Until we
        # read the actual fixtures, randomize per-trial to get a fair
        # expectation across plausible bracket positions.
        shuffled = list(advancers)
        random.shuffle(shuffled)
        result = simulate_remaining(shuffled, start_round, players_by_team)
        tp, pp = score_future_points(result, shuffled, start_round,
                                     players_by_team, gpg, apg)
        for k, v in tp.items(): team_acc[k]   += v
        for k, v in pp.items(): player_acc[k] += v

    prices = {}
    alive_ids = {t["id"] for t in advancers}

    for t in advancers:
        mean_fp = team_acc[t["id"]] / runs
        market_value = max(1, round_half_up(mean_fp / TARGET_ROI))
        buy, sell = derive_market_prices(mean_fp, market_value)
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
            buy, sell = derive_market_prices(mean_fp, market_value)
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
    """Read real advancers from Firestore by reading the `start_round` BRACKET.

    A team is contesting `start_round` iff it's slotted into one of that
    round's fixtures. We deliberately do NOT key off finalRound: a team's
    finalRound only reaches `start_round` after it has *played* a
    start_round match, so at the transition edge (round just completed, next
    round not yet played) finalRound would identify nobody. The upstream feed
    fills the next round's fixture slots as soon as the bracket is decided,
    which is exactly when this should fire."""
    matches = [m.to_dict() or {} for m in db.collection("matches").stream()]
    slugs = advancer_slugs_for_round(matches, start_round)
    if not slugs:
        return []
    advancers = []
    for tdoc in db.collection("teams").stream():
        if tdoc.id in slugs:
            advancers.append({**(tdoc.to_dict() or {}), "id": tdoc.id})
    return advancers


def write_prices_to_firestore(db, prices, advancers, all_teams, round_label, elim_slugs):
    """Persist marketValue/buyPrice/sellPrice to each team + player doc.
    Eliminated assets get marketValue=0 + a legacy display sellPrice.

    `elim_slugs` is the explicit set of team slugs being eliminated this round.
    Teams that are neither advancing (in `prices`) nor in `elim_slugs` are left
    untouched - that protects beaten semifinalists who still have a 3rd-place
    match to play, and teams whose bracket slot hasn't been seeded yet.

    NOTE: the actual auto-sell refund is now per-pick (25% of each holder's
    purchasePrice, in auto_sell_eliminated_picks), NOT this per-asset value.
    The per-asset liquidationValue/sellPrice is kept only as a display field;
    it's captured once on first elimination and left alone on reruns.

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
        elif tid not in elim_slugs:
            # Not advancing this round, but not being eliminated either - still
            # has a 3rd-place match, or its bracket slot isn't seeded yet.
            # Leave the doc exactly as-is.
            continue
        elif t.get("liquidationValue") is not None:
            n_elim_existing += 1
        else:
            # Newly eliminated - capture liquidation value FROM LAST KNOWN MV
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
                "eliminated":       True,
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
        elif p.get("teamId") not in elim_slugs:
            # Player's team is still alive / awaiting 3rd-place / unseeded.
            continue
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
    credit currentBudget with ELIM_REFUND_RATE of what THAT holder paid for
    the pick (its purchasePrice). Records each auto-sell as a transaction row
    so users can see what happened.

    Refund is per-pick on purchase price (not the asset's market value), so two
    holders of the same eliminated team get back a fraction of their own cost.

    Idempotent: a pick is only auto-sold once because removing it from the
    roster means there's nothing to process on the next run."""
    from datetime import datetime, timezone

    # Eliminated asset id -> its doc (we need totalPoints/goals/assists to bank
    # the forward points the holder earned while they held the pick).
    elim_teams = {}
    for tdoc in db.collection("teams").stream():
        t = tdoc.to_dict() or {}
        if t.get("eliminated") or t.get("marketValue") == 0:
            elim_teams[tdoc.id] = t
    elim_players = {}
    for pdoc in db.collection("players").stream():
        p = pdoc.to_dict() or {}
        if p.get("eliminated") or p.get("marketValue") == 0:
            elim_players[pdoc.id] = p

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

        # Refund = ELIM_REFUND_RATE of each pick's own purchase price.
        # Also BANK the forward points/tiebreaker the holder earned while they
        # held the pick (so eliminations keep, not forfeit, what they scored).
        sells_record = []
        exited_records = []
        total_refund = 0
        banked_pts = 0
        banked_tb = 0
        for pick in eliminated_picks:
            paid = pick.get("purchasePrice", 0) or 0
            refund = round_half_up(paid * ELIM_REFUND_RATE)
            total_refund += refund
            asset = (elim_teams if pick["kind"] == "team" else elim_players)[pick["assetId"]]
            asset_pts = int(asset.get("totalPoints", 0))
            asset_tb = (int(asset.get("goalsFor", 0)) if pick["kind"] == "team"
                        else int(asset.get("goals", 0)) + int(asset.get("assists", 0)))
            fwd_pts = max(0, asset_pts - int(pick.get("pointsAtPurchase", 0) or 0))
            fwd_tb  = max(0, asset_tb  - int(pick.get("tbAtPurchase", 0) or 0))
            banked_pts += fwd_pts
            banked_tb  += fwd_tb
            sells_record.append({
                "kind":      pick["kind"],
                "assetId":   pick["assetId"],
                "paidPrice": paid,
                "soldAt":    refund,
                "reason":    "auto-sell-elimination",
            })
            # History record so the UI can show this pick grayed-out with the
            # points it retained for the holder.
            exited_records.append({
                "kind":          pick["kind"],
                "assetId":       pick["assetId"],
                "points":        fwd_pts,
                "purchasePrice": paid,
                "exitReason":    "eliminated",
                "exitAt":        now,
            })

        new_budget = (u.get("currentBudget") or 0) + total_refund
        udoc.reference.set({
            "roster":           surviving_picks,
            "currentBudget":    new_budget,
            "bankedPoints":     int(u.get("bankedPoints", 0) or 0) + banked_pts,
            "bankedTiebreaker": int(u.get("bankedTiebreaker", 0) or 0) + banked_tb,
            "exitedPicks":      (u.get("exitedPicks") or []) + exited_records,
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
    ap.add_argument("--form-m", type=float, default=FORM_M,
                    help="EB strength of the preseason prior in pseudo-games "
                         "(small=trust this tournament's goals, "
                         "large=trust preseason price).")
    ap.add_argument("--form-json", default=None,
                    help="Dry-run only: path to a JSON map of results-to-date "
                         "({playerId: {goals, assists}} or the dumped state) so "
                         "form effects can be previewed without Firestore.")
    args = ap.parse_args()

    teams, players = load_seed()
    by_slug, by_fdid = build_team_indexes(teams)
    players_by_team = defaultdict(list)
    for p in players:
        players_by_team[p["teamId"]].append(p)
    print(f"Loaded {len(teams)} teams, {len(players)} players from seed")
    print(f"Warm-start round: {args.from_round}, runs: {args.runs}\n")

    # Results-to-date drive each player's forward goal/assist share.
    goals_by_id, assists_by_id = {}, {}

    # Live mode: read real advancers from Firestore.
    # Dry-run mode: synthesize advancers from seed.
    db = None
    elim_slugs = set()
    if args.write:
        from _fwc_lib import firestore_client
        db = firestore_client()
        for pdoc in db.collection("players").stream():
            pd = pdoc.to_dict() or {}
            goals_by_id[pdoc.id]   = pd.get("goals", 0) or 0
            assists_by_id[pdoc.id] = pd.get("assists", 0) or 0
        matches = [m.to_dict() or {} for m in db.collection("matches").stream()]
        if not round_fully_seeded(matches, args.from_round):
            print(f"The {args.from_round} bracket is not fully seeded yet "
                  f"(some fixtures have no teams assigned). Wait until the feed "
                  f"fills every slot, then re-run. Aborting.")
            return
        advancers = live_advancers(db, args.from_round)
        if not advancers:
            print(f"No teams slotted into {args.from_round} fixtures. "
                  f"Has the bracket been seeded? Aborting.")
            return
        advancer_ids = {t["id"] for t in advancers}
        champ = next((t["id"] for t in advancers
                      if (t.get("finalRound") == "W")), None)
        elim_slugs = compute_eliminated_slugs(matches, advancer_ids, champ)
        print(f"Live mode: {len(advancers)} teams advanced to {args.from_round}; "
              f"{len(elim_slugs)} eliminated")
    else:
        advancers = hypothetical_advancers(teams, args.from_round)
        if args.form_json:
            import json as _json
            raw = _json.loads(Path(args.form_json).read_text())
            recs = raw.get("players", raw) if isinstance(raw, dict) else raw
            it = recs.values() if isinstance(recs, dict) else recs
            for r in it:
                pid = r.get("id")
                if pid is None:
                    continue
                goals_by_id[pid]   = r.get("goals", 0) or 0
                assists_by_id[pid] = r.get("assists", 0) or 0
            print(f"Loaded results-to-date for {len(goals_by_id)} players "
                  f"from {args.form_json}")
    print(f"Player rate blend: prior strength m={args.form_m} pseudo-games\n")
    print(f"Hypothetical {args.from_round} field ({len(advancers)} teams):")
    for t in advancers[:8]:
        print(f"  ${t['basePrice']:>2}  {t['name']}")
    if len(advancers) > 8:
        print(f"  ... ({len(advancers) - 8} more)\n")
    else:
        print()

    prices = reprice(advancers, args.from_round, players_by_team,
                     runs=args.runs, seed=args.seed,
                     goals_by_id=goals_by_id, assists_by_id=assists_by_id,
                     form_m=args.form_m)

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
        write_prices_to_firestore(db, prices, advancers, teams, args.from_round, elim_slugs)
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
