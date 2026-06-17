"""Validate the cached CL season harness (Phase 0 sanity check).

Proves the local data is complete and sufficient to drive the scoring/pricing
engine, by reconstructing real artifacts from it:
  - final league-phase table (cross-checked against football-data's leagueRank)
  - knockout bracket progression (two-legged aggregate) + champion
  - top scorers / assisters
  - data-completeness report (lineups present -> win-share + clean-sheet viable)

Read-only over data/cl/*.json. No network, no Firestore.
  python scripts/cl/validate_cl_harness.py --season 2025
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "cl"

KO_ORDER = ["PLAYOFFS", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "FINAL"]


def load(season: str):
    matches = json.loads((DATA_DIR / f"cl{season}_matches.json").read_text())
    details = json.loads((DATA_DIR / f"cl{season}_details.json").read_text())
    teams_path = DATA_DIR / f"cl{season}_teams.json"
    teams = json.loads(teams_path.read_text()) if teams_path.exists() else []
    return matches, details, teams


def league_table(matches):
    """Reconstruct the 36-team league phase table."""
    tbl = defaultdict(lambda: {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0})
    rank_seen = {}
    for m in matches:
        if m.get("stage") != "LEAGUE_STAGE":
            continue
        h, a = m["homeTeam"], m["awayTeam"]
        ft = m["score"]["fullTime"]
        gh, ga = ft["home"], ft["away"]
        for t in (h, a):
            if t.get("leagueRank"):
                rank_seen[t["name"]] = t["leagueRank"]
        rh, ra = tbl[h["name"]], tbl[a["name"]]
        rh["P"] += 1; ra["P"] += 1
        rh["GF"] += gh; rh["GA"] += ga
        ra["GF"] += ga; ra["GA"] += gh
        w = m["score"]["winner"]
        if w == "HOME_TEAM":
            rh["W"] += 1; ra["L"] += 1
        elif w == "AWAY_TEAM":
            ra["W"] += 1; rh["L"] += 1
        else:
            rh["D"] += 1; ra["D"] += 1
    rows = []
    for name, r in tbl.items():
        pts = r["W"] * 3 + r["D"]
        rows.append((name, pts, r["GF"] - r["GA"], r["GF"], r, rank_seen.get(name)))
    rows.sort(key=lambda x: (-x[1], -x[2], -x[3]))
    return rows


def ko_progression(matches):
    """Aggregate two-legged ties per KO stage; return winners + champion."""
    by_stage = defaultdict(list)
    for m in matches:
        if m.get("stage") in KO_ORDER:
            by_stage[m["stage"]].append(m)
    out = {}
    champion = None
    for stage in KO_ORDER:
        agg = defaultdict(lambda: defaultdict(int))  # tie_key -> {team: goals}
        for m in by_stage.get(stage, []):
            h, a = m["homeTeam"]["name"], m["awayTeam"]["name"]
            ft = m["score"]["fullTime"]
            key = tuple(sorted([h, a]))
            agg[key][h] += ft["home"]
            agg[key][a] += ft["away"]
            # capture penalty shootout winner for single-leg ties (final)
            pens = m.get("penalties") or {}
        winners = []
        for key, goals in agg.items():
            ranked = sorted(goals.items(), key=lambda kv: -kv[1])
            winners.append(ranked[0][0])
        out[stage] = (len(by_stage.get(stage, [])), winners)
        if stage == "FINAL" and winners:
            champion = winners[0]
    return out, champion


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025")
    args = ap.parse_args()
    matches, details, teams = load(args.season)

    finished = [m for m in matches if m.get("status") == "FINISHED"]
    print(f"=== CL {args.season} harness validation ===")
    print(f"matches: {len(matches)} ({len(finished)} FINISHED)")
    print(f"details cached: {len(details)}  (need {len(finished)})")
    print(f"teams file: {len(teams)} clubs"
          + (f", squad sizes {min(len(t.get('squad',[]) or []) for t in teams)}"
             f"-{max(len(t.get('squad',[]) or []) for t in teams)}" if teams else ""))

    # League table
    rows = league_table(matches)
    print(f"\n--- League phase table ({len(rows)} teams) — top 10 ---")
    print(f"{'#':>2} {'club':22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} "
          f"{'GD':>4} {'Pts':>3}  fd-rank")
    rank_mismatch = 0
    for i, (name, pts, gd, gf, r, fdrank) in enumerate(rows, 1):
        flag = "" if fdrank in (i, None) else f"  <-- fd says {fdrank}"
        if fdrank not in (i, None):
            rank_mismatch += 1
        if i <= 10:
            print(f"{i:>2} {name[:22]:22} {r['P']:>2} {r['W']:>2} {r['D']:>2} "
                  f"{r['L']:>2} {gd:>+4} {pts:>3}{flag}")
    print(f"(rank vs football-data leagueRank mismatches: {rank_mismatch})")

    # KO progression
    prog, champ = ko_progression(matches)
    print("\n--- Knockout progression (two-legged aggregate) ---")
    for stage in KO_ORDER:
        n, winners = prog.get(stage, (0, []))
        print(f"{stage:15} {n:>2} legs -> {len(winners)} winners: "
              f"{', '.join(sorted(winners))[:80]}")
    print(f"\n*** CHAMPION: {champ} ***")

    # Top scorers / assisters (from details)
    goals = defaultdict(int); assists = defaultdict(int)
    lineups_ok = 0
    for mid, d in details.items():
        for g in d.get("goals", []) or []:
            s = (g.get("scorer") or {}).get("name")
            a = (g.get("assist") or {}).get("name")
            if s and g.get("type") != "OWN":
                goals[s] += 1
            if a:
                assists[a] += 1
        if (d["homeTeam"].get("lineup") and d["awayTeam"].get("lineup")):
            lineups_ok += 1
    top_g = sorted(goals.items(), key=lambda kv: -kv[1])[:8]
    top_a = sorted(assists.items(), key=lambda kv: -kv[1])[:8]
    print("\n--- Top scorers ---")
    for n, c in top_g:
        print(f"  {c:>2}  {n}")
    print("--- Top assisters ---")
    for n, c in top_a:
        print(f"  {c:>2}  {n}")

    # Completeness
    print("\n--- Data completeness (scoring-engine viability) ---")
    print(f"details with BOTH lineups (win-share + clean-sheet viable): "
          f"{lineups_ok}/{len(details)}")
    missing = [m['id'] for m in finished if str(m['id']) not in details]
    print(f"finished matches missing detail: {len(missing)}"
          + (f" {missing[:5]}" if missing else ""))


if __name__ == "__main__":
    main()
