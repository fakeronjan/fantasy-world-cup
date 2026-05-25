"""Focused side-by-side of top teams + top players under the candidate presets,
under the 2026 48-team format. Compares against the goals-only baseline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulate import (
    ScoringWeights, PlayerPricing,
    load_year, team_clean_sheets_estimate,
)


CANDIDATES = [
    ("F. RECOMMENDED — integer everywhere, less team high-end variance",
     ScoringWeights(
        team_win=3, team_draw=1,
        bonus_r32=1, bonus_r16=2, bonus_qf=3, bonus_sf=5,
        bonus_final=8, bonus_champion=12,
        player_goal=5, player_assist=0,
        player_clean_sheet_gk=5, player_clean_sheet_other=1,
        player_win_share=1,
     )),
]


def report_year(year: int) -> None:
    for label, w in CANDIDATES:
        teams, players = load_year(year, PlayerPricing(min_price=2), format_2026=True)
        teams_by_name = {t.name: t for t in teams}

        print(f"\n{'─'*78}")
        print(f"  WC {year} adapted to 2026 format  —  {label}")
        print(f"{'─'*78}")

        # Top 10 teams
        ranked_teams = sorted(teams, key=lambda x: x.points(w, True), reverse=True)[:10]
        print(f"  TOP 10 TEAMS")
        print(f"  {'#':<3}{'team':<22}{'round':<6} {'W-D-L':<7} {'GA':<3} {'CS~':<5} {'$':<4} {'pts':>5}")
        for i, t in enumerate(ranked_teams, 1):
            cs = team_clean_sheets_estimate(t)
            print(f"  {i:<3}{t.name[:21]:<22}{t.final_round:<6} "
                  f"{t.matches_won}-{t.matches_drawn}-{t.matches_lost:<3} "
                  f"{t.goals_against:<3d} {cs:<5.1f} "
                  f"${t.price:<3d} {t.points(w, True):>5.1f}")

        # Top 10 players
        print(f"\n  TOP 10 PLAYERS  (g=goal pts, ws=win share, cs=clean sheet pts)")
        print(f"  {'#':<3}{'player':<28}{'team':<14}{'pos':<4}{'$':<4} "
              f"{'g':>2} {'g.pts':>5} {'ws':>5} {'cs':>5} {'total':>6}")
        scored = []
        for p in players:
            # Skip phantom squad players for top-10 (they're price floor + 0g; all tie)
            if p.name.startswith(("GK", )) or "-sq" in p.name or "-GK" in p.name:
                continue
            t = teams_by_name.get(p.team_name)
            if t is None:
                continue
            gp = w.player_goal * p.goals
            wp = w.player_win_share * t.matches_won
            cs = team_clean_sheets_estimate(t)
            cs_rate = w.player_clean_sheet_gk if p.position == "GK" else w.player_clean_sheet_other
            cp = cs_rate * cs
            scored.append((p, t, gp, wp, cp, gp + wp + cp))
        for i, (p, t, gp, wp, cp, tot) in enumerate(
                sorted(scored, key=lambda x: x[5], reverse=True)[:10], 1):
            pos = p.position or "—"
            print(f"  {i:<3}{p.name[:27]:<28}{(p.team_name or '?')[:13]:<14}"
                  f"{pos:<4}${p.price:<3d} "
                  f"{p.goals:>2d} {gp:>5.1f} {wp:>5.1f} {cp:>5.1f} {tot:>6.1f}")

        # Aggregate totals
        team_total = sum(t.points(w, True) for t in teams)
        player_total = sum(s[5] for s in scored)
        # Include phantom squad win-share contributions
        phantom_player_total = 0.0
        for p in players:
            if not (p.name.startswith(("GK",)) or "-sq" in p.name or "-GK" in p.name):
                continue
            t = teams_by_name.get(p.team_name)
            if t is None: continue
            wp = w.player_win_share * t.matches_won
            cs = team_clean_sheets_estimate(t)
            cs_rate = w.player_clean_sheet_gk if p.position == "GK" else w.player_clean_sheet_other
            phantom_player_total += wp + cs_rate * cs
        print(f"\n  AGGREGATE: teams {team_total:.0f}, named players {player_total:.0f}, "
              f"phantom squad pool {phantom_player_total:.0f}, "
              f"all players {player_total + phantom_player_total:.0f}")


if __name__ == "__main__":
    for yr in (2022, 2018, 2014, 2010):
        print(f"\n\n{'='*78}")
        print(f"  WC {yr} DATA")
        print(f"{'='*78}")
        report_year(yr)
