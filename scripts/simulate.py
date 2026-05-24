"""Fantasy World Cup balance simulation.

Loads historical team and player data for WC 2018 and 2022, applies a
configurable scoring system, and reports how total fantasy points
distribute across teams vs. players. The goal: tune scoring weights and
price tiers so that all-team, all-player, and mixed $100 rosters have
comparable expected value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from knockout_results import (
    all_teams_with_round,
    apply_2026_format,
    WC2026_EXPANSION_TEAMS,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BUDGET = 100
ROSTER_SIZE_CAP = 12  # max picks — keeps strategy meaningful (vs. spam $1 players)


# ---------------------------------------------------------------------------
# Scoring weights — knobs to tune
# ---------------------------------------------------------------------------

@dataclass
class ScoringWeights:
    # Team scoring (per match)
    team_win: float = 3
    team_draw: float = 1
    # Team advancement bonuses (one-time, awarded when round is reached).
    # bonus_r32 only matters for the 2026 48-team format (no R32 in 2018/2022).
    bonus_r32: float = 1
    bonus_r16: float = 2
    bonus_qf: float = 4
    bonus_sf: float = 6
    bonus_final: float = 10
    bonus_champion: float = 15
    # Player scoring (per event)
    player_goal: float = 5
    player_assist: float = 3       # not used yet — no assists data
    player_clean_sheet: float = 4  # not used yet


# ---------------------------------------------------------------------------
# Pre-tournament price tiers
# ---------------------------------------------------------------------------
# Based on pre-tournament public strength signals (FIFA ranking, seed pot,
# bookmaker odds). Set BEFORE the tournament begins — no post-hoc knowledge.

TEAM_PRICE_TIERS_2022 = {
    # Tier 1 — title contenders
    15: ["Brazil", "France", "Argentina", "England", "Spain"],
    # Tier 2 — strong contenders
    12: ["Germany", "Portugal", "Netherlands", "Belgium", "Denmark"],
    # Tier 3 — outside chance
    9:  ["Uruguay", "Croatia", "Switzerland", "Mexico", "USA", "United States",
         "Senegal", "Serbia", "Poland"],
    # Tier 4 — unlikely
    6:  ["Japan", "South Korea", "Iran", "Wales", "Ecuador", "Morocco",
         "Tunisia", "Cameroon", "Australia"],
    # Tier 5 — host / qualifier outsiders
    3:  ["Qatar", "Qatar (H)", "Saudi Arabia", "Costa Rica", "Ghana", "Canada"],
}

TEAM_PRICE_TIERS_2018 = {
    15: ["Germany", "Brazil", "France", "Spain", "Argentina"],
    12: ["Belgium", "Portugal", "England", "Uruguay", "Croatia"],
    9:  ["Colombia", "Mexico", "Switzerland", "Denmark", "Sweden", "Poland",
         "Senegal", "Russia"],
    6:  ["Iran", "Egypt", "Morocco", "Tunisia", "Iceland", "Serbia", "Nigeria",
         "South Korea", "Japan", "Peru", "Australia", "Costa Rica"],
    3:  ["Saudi Arabia", "Panama"],
}

# Player price tiers — top scorers/playmakers + GKs of top teams get higher tiers.
# For each tournament we list players by tier. Anyone not listed gets the
# default $1 tier. These reflect pre-tournament expectation (form + reputation).

PLAYER_PRICE_TIERS_2022 = {
    10: ["Kylian Mbappé", "Lionel Messi", "Neymar", "Cristiano Ronaldo",
         "Harry Kane", "Karim Benzema", "Erling Haaland"],  # Benzema/Haaland not in tournament — illustrative
    7:  ["Robert Lewandowski", "Sadio Mané", "Mohamed Salah", "Vinícius Júnior",
         "Bruno Fernandes", "Antoine Griezmann", "Phil Foden", "Bukayo Saka",
         "Raheem Sterling", "Olivier Giroud", "Álvaro Morata", "Romelu Lukaku",
         "Heung-min Son", "Memphis Depay", "Luka Modrić"],
    5:  ["Julián Álvarez", "Lautaro Martínez", "Ángel Di María", "Jude Bellingham",
         "Mason Mount", "Marcus Rashford", "Jamal Musiala", "Kai Havertz",
         "Serge Gnabry", "Pedri", "Gavi", "Ferran Torres", "Cody Gakpo",
         "Frenkie de Jong", "Ivan Perišić", "Andrej Kramarić", "Joško Gvardiol",
         "Richarlison", "Casemiro", "Lucas Paquetá", "Rodrygo", "João Félix",
         "Bernardo Silva", "Rafael Leão", "Hakim Ziyech", "Achraf Hakimi",
         "Sofyan Amrabat", "Youssef En-Nesyri", "Aurélien Tchouaméni",
         "Theo Hernández", "Ousmane Dembélé", "Vincent Aboubakar"],
    3:  ["Enner Valencia", "Gonçalo Ramos", "Jordan Henderson", "Declan Rice",
         "Kalidou Koulibaly", "Wojciech Szczęsny", "Emiliano Martínez",
         "Yassine Bounou", "Hugo Lloris", "Thibaut Courtois", "Alisson",
         "Andries Noppert", "Dominik Livaković", "Édouard Mendy",
         "Manuel Neuer", "Unai Simón"],  # mostly GKs
}

PLAYER_PRICE_TIERS_2018 = {
    10: ["Lionel Messi", "Cristiano Ronaldo", "Neymar", "Mohamed Salah",
         "Kylian Mbappé", "Harry Kane"],
    7:  ["Antoine Griezmann", "Paul Pogba", "Eden Hazard", "Kevin De Bruyne",
         "Romelu Lukaku", "Luis Suárez", "Edinson Cavani", "Sergio Agüero",
         "Luka Modrić", "Ivan Rakitić", "Toni Kroos", "Thomas Müller",
         "Robert Lewandowski", "Diego Costa", "Isco", "James Rodríguez",
         "Heung-min Son"],
    5:  ["Olivier Giroud", "Raheem Sterling", "Dele Alli", "Jesse Lingard",
         "Marcus Rashford", "Mario Mandžukić", "Ivan Perišić", "Ante Rebić",
         "Andrés Iniesta", "Sergio Busquets", "David Silva", "Marco Asensio",
         "Radamel Falcao", "Juan Cuadrado", "Roberto Firmino", "Philippe Coutinho",
         "Gabriel Jesus", "Willian", "Marcelo", "Dries Mertens", "Yannick Carrasco",
         "Mesut Özil", "Manuel Neuer", "Marc-André ter Stegen", "Hugo Lloris",
         "Thibaut Courtois", "David de Gea", "Jordan Pickford", "Alisson"],
    3:  ["Yerry Mina", "Denis Cheryshev", "Artem Dzyuba", "Aleksandr Golovin"],
}

DEFAULT_TEAM_PRICE = 3
DEFAULT_PLAYER_PRICE = 1  # also configurable per-run, see PlayerPricing below


@dataclass
class PlayerPricing:
    """Override pricing to test variants. min_price = $1 keeps the original
    tiers; min_price = $2 lifts the cheapest tier to $2 (removes 'spam $1
    players' strategy)."""
    min_price: int = 1


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _norm_team(name: str) -> str:
    """Strip host marker '(H)' and surrounding whitespace."""
    return name.replace("(H)", "").strip()


def _price_for(name: str, tiers: dict[int, list[str]], default: int) -> int:
    for price, names in tiers.items():
        if name in names:
            return price
    # Try normalized form
    n = _norm_team(name)
    for price, names in tiers.items():
        if n in (_norm_team(x) for x in names):
            return price
    return default


@dataclass
class Team:
    name: str
    matches_won: int
    matches_drawn: int
    matches_lost: int
    final_round: str  # group, R16, QF, SF, F, W
    price: int

    def points(self, w: ScoringWeights, format_2026: bool = False) -> float:
        pts = w.team_win * self.matches_won + w.team_draw * self.matches_drawn
        # Cumulative advancement bonuses. In 2026 format, R32 is a real round
        # and gets its own bonus; any team that reached R16 also passed R32.
        if format_2026:
            order = ["group", "R32", "R16", "QF", "SF", "F", "W"]
            bonuses = {
                "R32": w.bonus_r32, "R16": w.bonus_r16, "QF": w.bonus_qf,
                "SF": w.bonus_sf, "F": w.bonus_final, "W": w.bonus_champion,
            }
            # If team reached R16+, they passed R32 implicitly
            if self.final_round in ("R16", "QF", "SF", "F", "W"):
                # Include R32 bonus
                reached_idx = order.index(self.final_round)
                for r in order[1:reached_idx + 1]:
                    pts += bonuses[r]
            else:
                # group only
                pass
        else:
            order = ["group", "R16", "QF", "SF", "F", "W"]
            bonuses = {
                "R16": w.bonus_r16, "QF": w.bonus_qf, "SF": w.bonus_sf,
                "F": w.bonus_final, "W": w.bonus_champion,
            }
            reached_idx = order.index(self.final_round)
            for r in order[1:reached_idx + 1]:
                pts += bonuses[r]
        return pts


@dataclass
class Player:
    name: str
    goals: int
    assists: int = 0
    clean_sheets: int = 0
    price: int = DEFAULT_PLAYER_PRICE

    def points(self, w: ScoringWeights) -> float:
        return (w.player_goal * self.goals
                + w.player_assist * self.assists
                + w.player_clean_sheet * self.clean_sheets)


def load_year(year: int, pricing: PlayerPricing | None = None,
               format_2026: bool = False) -> tuple[list[Team], list[Player]]:
    """Load WC data. If format_2026=True, adapt the historical dataset:
    add 16 expansion teams and credit advancers with a +1 R32 win."""
    pricing = pricing or PlayerPricing()
    # --- Teams: group standings + knockout augment ---
    df = pd.read_csv(DATA_DIR / f"wc{year}_group_standings.csv")
    ko_data = all_teams_with_round(year)
    if format_2026:
        ko_data = apply_2026_format(ko_data)
    team_tiers = TEAM_PRICE_TIERS_2022 if year == 2022 else TEAM_PRICE_TIERS_2018

    # Wikipedia's column name varies across years ('Team' vs 'Teamvte')
    team_col = next(c for c in df.columns if c.startswith("Team"))

    teams: list[Team] = []
    for _, row in df.iterrows():
        raw = str(row[team_col]).strip()
        norm = _norm_team(raw)
        gw, gd, gl = int(row["W"]), int(row["D"]), int(row["L"])

        # Find KO record — try both raw and normalized names
        ko = ko_data.get(raw) or ko_data.get(norm)
        if ko is None:
            # Some 2022 group-only teams are listed with (H) only on host
            ko = ("group", 0, 0, 0)
            # Warn so we can fix the name mapping
            print(f"  [warn] no KO data for '{raw}' / '{norm}', assuming group-only")
        round_reached, kw, kd, kl = ko

        price = _price_for(raw, team_tiers, DEFAULT_TEAM_PRICE)
        teams.append(Team(
            name=norm,
            matches_won=gw + kw,
            matches_drawn=gd + kd,
            matches_lost=gl + kl,
            final_round=round_reached,
            price=price,
        ))

    if format_2026:
        # Add the 16 expansion teams (their group-stage records aren't in the
        # historical CSV — they're forward-looking estimates).
        for exp_name, (gw, gd, gl) in WC2026_EXPANSION_TEAMS.items():
            teams.append(Team(
                name=exp_name,
                matches_won=gw,
                matches_drawn=gd,
                matches_lost=gl,
                final_round="group",
                price=DEFAULT_TEAM_PRICE,  # $3 — outsider tier
            ))

    # --- Players: goalscorers JSON ---
    scorers = json.loads((DATA_DIR / f"wc{year}_goalscorers.json").read_text())
    player_tiers = PLAYER_PRICE_TIERS_2022 if year == 2022 else PLAYER_PRICE_TIERS_2018
    players: list[Player] = []
    for p in scorers:
        name = p["player"]
        price = _price_for(name, player_tiers, DEFAULT_PLAYER_PRICE)
        price = max(price, pricing.min_price)
        players.append(Player(name=name, goals=int(p["goals"]), price=price))
    return teams, players


# ---------------------------------------------------------------------------
# Roster optimization
# ---------------------------------------------------------------------------

@dataclass
class Asset:
    name: str
    kind: str       # "team" or "player"
    points: float
    price: int


def to_assets(teams: list[Team], players: list[Player], w: ScoringWeights,
               format_2026: bool = False) -> list[Asset]:
    out = [Asset(t.name, "team", t.points(w, format_2026), t.price) for t in teams]
    out += [Asset(p.name, "player", p.points(w), p.price) for p in players]
    return out


def greedy_max_points(assets: list[Asset], budget: int,
                       roster_cap: int = ROSTER_SIZE_CAP) -> tuple[float, list[Asset]]:
    """Pick assets to maximize total points subject to budget and roster cap.

    Greedy by points/dollar with two passes:
      1) take highest pt/$ items until roster cap reached or budget tight
      2) try swapping in higher-absolute-points items if we have unspent budget
    """
    # Pass 1: pure pt/$ greedy
    sorted_ = sorted(assets, key=lambda a: a.points / max(a.price, 1), reverse=True)
    picked: list[Asset] = []
    spent = 0
    for a in sorted_:
        if len(picked) >= roster_cap:
            break
        if spent + a.price <= budget:
            picked.append(a)
            spent += a.price

    # Pass 2: with remaining budget, swap weakest pick for a stronger one
    improved = True
    while improved:
        improved = False
        for i, current in enumerate(picked):
            remaining = budget - (spent - current.price)
            candidates = [a for a in assets
                          if a not in picked and a.price <= remaining
                          and a.points > current.points]
            if not candidates:
                continue
            best = max(candidates, key=lambda a: a.points)
            picked[i] = best
            spent = sum(p.price for p in picked)
            improved = True
            break

    return sum(a.points for a in picked), picked


def best_strategy(year: int, w: ScoringWeights, restrict: str | None = None,
                   pricing: PlayerPricing | None = None,
                   format_2026: bool = False) -> tuple[float, list[Asset]]:
    """restrict: None | 'team' | 'player' — limit asset pool to one kind."""
    teams, players = load_year(year, pricing, format_2026)
    assets = to_assets(teams, players, w, format_2026)
    if restrict == "team":
        assets = [a for a in assets if a.kind == "team"]
    elif restrict == "player":
        assets = [a for a in assets if a.kind == "player"]
    return greedy_max_points(assets, BUDGET)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(year: int, w: ScoringWeights, pricing: PlayerPricing | None = None,
            format_2026: bool = False) -> None:
    pricing = pricing or PlayerPricing()
    teams, players = load_year(year, pricing, format_2026)

    fmt_label = "2026 format (48 teams + R32)" if format_2026 else f"{year} format (32 teams)"
    print(f"\n{'='*70}")
    print(f"  WC {year} data, {fmt_label}")
    print(f"  budget ${BUDGET}, max roster {ROSTER_SIZE_CAP}, "
          f"min player price ${pricing.min_price}")
    print(f"{'='*70}")

    # Aggregate signal: total team points vs total player points
    total_team_pts = sum(t.points(w, format_2026) for t in teams)
    total_player_pts = sum(p.points(w) for p in players)
    total_team_price = sum(t.price for t in teams)
    total_player_price = sum(p.price for p in players)
    print(f"\nAggregate market:")
    print(f"  Teams:   {len(teams):3d} assets, ${total_team_price:5d} total price, "
          f"{total_team_pts:6.0f} total points  → {total_team_pts/total_team_price:.2f} pts/$")
    print(f"  Players: {len(players):3d} assets, ${total_player_price:5d} total price, "
          f"{total_player_pts:6.0f} total points  → {total_player_pts/total_player_price:.2f} pts/$")

    # Top performers by points/$
    all_assets = to_assets(teams, players, w, format_2026)
    print(f"\nTop 15 value picks (points per dollar):")
    print(f"  {'asset':<28} {'kind':<7} {'price':>5} {'pts':>5} {'pt/$':>5}")
    for a in sorted(all_assets, key=lambda x: x.points / max(x.price, 1), reverse=True)[:15]:
        print(f"  {a.name[:27]:<28} {a.kind:<7} {a.price:>5d} {a.points:>5.1f} "
              f"{a.points/max(a.price,1):>5.2f}")

    # Strategy comparison
    print(f"\nStrategy comparison (optimal ${BUDGET} roster, ≤{ROSTER_SIZE_CAP} picks):")
    for label, kind in (("all-team", "team"), ("all-player", "player"), ("any-mix", None)):
        pts, picked = best_strategy(year, w, kind, pricing, format_2026)
        teams_count = sum(1 for a in picked if a.kind == "team")
        player_count = sum(1 for a in picked if a.kind == "player")
        spent = sum(a.price for a in picked)
        sample = ", ".join(a.name for a in picked[:5])
        if len(picked) > 5:
            sample += f", ... (+{len(picked)-5} more)"
        print(f"  {label:<11}  {pts:6.1f} pts   "
              f"({teams_count}T + {player_count}P, ${spent} spent)")
        print(f"               picks: {sample}")


PRESETS: list[tuple[str, ScoringWeights, PlayerPricing]] = [
    # All presets now use player_assist=0 (no automated source for assists;
    # see project_sports_api memory). Clean sheets still TBD (need position
    # data to award correctly).
    ("A. baseline (simple, no assists)",
     ScoringWeights(player_assist=0, player_clean_sheet=0),
     PlayerPricing(min_price=1)),
    ("B. team-boosted",
     ScoringWeights(
        team_win=4, team_draw=2,
        bonus_r16=4, bonus_qf=8, bonus_sf=12, bonus_final=20, bonus_champion=30,
        player_goal=5, player_assist=0, player_clean_sheet=0),
     PlayerPricing(min_price=1)),
    ("C. team-boosted, no $1 players",
     ScoringWeights(
        team_win=4, team_draw=2,
        bonus_r16=4, bonus_qf=8, bonus_sf=12, bonus_final=20, bonus_champion=30,
        player_goal=5, player_assist=0, player_clean_sheet=0),
     PlayerPricing(min_price=2)),
    ("D. lighter team boost, no $1 players",
     ScoringWeights(
        team_win=3, team_draw=1,
        bonus_r16=3, bonus_qf=6, bonus_sf=10, bonus_final=15, bonus_champion=25,
        player_goal=5, player_assist=0, player_clean_sheet=0),
     PlayerPricing(min_price=2)),
]


def main() -> None:
    summary_rows = []
    # Run each preset under both the historical 32-team format AND the
    # adapted 2026 48-team format, on both 2018 and 2022 data.
    for preset_name, weights, pricing in PRESETS:
        print(f"\n\n{'#'*70}")
        print(f"# PRESET: {preset_name}")
        print(f"# weights: {weights}")
        print(f"# pricing: {pricing}")
        print(f"{'#'*70}")
        for year in (2022, 2018):
            for fmt_2026 in (False, True):
                report(year, weights, pricing, fmt_2026)
                row = {
                    "preset": preset_name,
                    "year": year,
                    "format": "2026" if fmt_2026 else "32T",
                }
                for label, kind in (("team", "team"), ("player", "player"), ("mix", None)):
                    pts, _ = best_strategy(year, weights, kind, pricing, fmt_2026)
                    row[f"{label}_pts"] = pts
                row["gap_pct"] = abs(row["team_pts"] - row["player_pts"]) / max(row["team_pts"], row["player_pts"]) * 100
                row["mix_advantage_pct"] = (row["mix_pts"] - max(row["team_pts"], row["player_pts"])) / max(row["team_pts"], row["player_pts"]) * 100
                summary_rows.append(row)

    print(f"\n\n{'='*78}")
    print("  BALANCE SUMMARY — lower gap% and higher mix-advantage% are better")
    print(f"{'='*78}")
    print(f"{'preset':<38} {'yr':<5} {'fmt':<5} {'team':>5} {'plr':>5} {'mix':>5} {'gap%':>5} {'mix+%':>6}")
    for r in summary_rows:
        print(f"{r['preset']:<38} {r['year']:<5} {r['format']:<5} {r['team_pts']:>5.0f} "
              f"{r['player_pts']:>5.0f} {r['mix_pts']:>5.0f} "
              f"{r['gap_pct']:>4.0f}% {r['mix_advantage_pct']:>5.0f}%")


if __name__ == "__main__":
    main()
