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
BUDGET = 60  # 2026-05-25 final
ROSTER_SIZE_CAP = 12  # reduced from 20 on 2026-05-25 to suppress cinderella


# ---------------------------------------------------------------------------
# Scoring weights - knobs to tune
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
    # Player scoring (per event) - Deep Data unlocked 2026-05-25
    player_goal: float = 5
    player_assist: float = 3       # restored after football-data.org Deep Data upgrade
    # Clean sheet: GK > DEF > everyone else (FPL-style, defensive-only).
    # NOTE: live game uses lineup-based (only players who actually played).
    # This historical sim is approximated as squad-based since we don't have
    # historical lineup data - treat results as a directional sanity check.
    player_clean_sheet_gk: float = 5
    player_clean_sheet_def: float = 2
    player_clean_sheet_other: float = 0
    # Win share - see CS note above; live game is lineup-based, sim is squad-based.
    player_win_share: float = 1


# ---------------------------------------------------------------------------
# Pre-tournament price tiers
# ---------------------------------------------------------------------------
# Based on pre-tournament public strength signals (FIFA ranking, seed pot,
# bookmaker odds). Set BEFORE the tournament begins - no post-hoc knowledge.

# Widened pricing (2026-05-25) - historical tiers now map into the same
# $1-$30 range used by the live 2026 game. Each tier is the midpoint of
# what that tier's teams would land at under the new continuous scheme.
# Historical tier prices updated 2026-05-25 to match the lifted mid-tier
# pricing in the live game (T3 lifted from 7→10 to bring its ROI down
# from 1.79 → ~1.3 pts/$).
TEAM_PRICE_TIERS_2022 = {
    22: ["Brazil", "France", "Argentina", "England", "Spain"],
    14: ["Germany", "Portugal", "Netherlands", "Belgium", "Denmark"],
    10: ["Uruguay", "Croatia", "Switzerland", "Mexico", "USA", "United States",
         "Senegal", "Serbia", "Poland"],
    5:  ["Japan", "South Korea", "Iran", "Wales", "Ecuador", "Morocco",
         "Tunisia", "Cameroon", "Australia"],
    1:  ["Qatar", "Qatar (H)", "Saudi Arabia", "Costa Rica", "Ghana", "Canada"],
}

TEAM_PRICE_TIERS_2018 = {
    22: ["Germany", "Brazil", "France", "Spain", "Argentina"],
    14: ["Belgium", "Portugal", "England", "Uruguay", "Croatia"],
    10: ["Colombia", "Mexico", "Switzerland", "Denmark", "Sweden", "Poland",
         "Senegal", "Russia"],
    5:  ["Iran", "Egypt", "Morocco", "Tunisia", "Iceland", "Serbia", "Nigeria",
         "South Korea", "Japan", "Peru", "Australia", "Costa Rica"],
    1:  ["Saudi Arabia", "Panama"],
}

TEAM_PRICE_TIERS_2014 = {
    22: ["Brazil", "Argentina", "Germany", "Spain"],
    14: ["Belgium", "Netherlands", "France", "Italy", "Uruguay", "Portugal",
         "Colombia", "England"],
    10: ["Mexico", "United States", "Russia", "Switzerland", "Chile",
         "Côte d'Ivoire", "Bosnia and Herzegovina", "Ecuador"],
    5:  ["Croatia", "Greece", "Costa Rica", "Algeria", "Nigeria", "Ghana",
         "Japan", "South Korea", "Cameroon", "Iran", "Australia"],
    1:  ["Honduras"],
}

TEAM_PRICE_TIERS_2010 = {
    22: ["Brazil", "Spain", "Argentina", "England", "Italy", "Germany"],
    14: ["Netherlands", "France", "Portugal"],
    10: ["Mexico", "United States", "Uruguay", "Côte d'Ivoire", "Cameroon",
         "Ghana", "Serbia", "Greece", "Chile", "Paraguay"],
    5:  ["South Africa", "Japan", "South Korea", "Australia", "Denmark",
         "Switzerland", "Slovenia", "Slovakia", "Nigeria"],
    1:  ["Honduras", "New Zealand", "Algeria", "North Korea"],
}

# Player price tiers - top scorers/playmakers + GKs of top teams get higher tiers.
# For each tournament we list players by tier. Anyone not listed gets the
# default $1 tier. These reflect pre-tournament expectation (form + reputation).

PLAYER_PRICE_TIERS_2022 = {
    18: ["Kylian Mbappé", "Lionel Messi", "Neymar", "Cristiano Ronaldo",
         "Harry Kane", "Karim Benzema", "Erling Haaland"],
    14: ["Robert Lewandowski", "Sadio Mané", "Mohamed Salah", "Vinícius Júnior",
         "Bruno Fernandes", "Antoine Griezmann", "Phil Foden", "Bukayo Saka",
         "Raheem Sterling", "Olivier Giroud", "Álvaro Morata", "Romelu Lukaku",
         "Heung-min Son", "Memphis Depay", "Luka Modrić"],
    11: ["Julián Álvarez", "Lautaro Martínez", "Ángel Di María", "Jude Bellingham",
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
         "Manuel Neuer", "Unai Simón"],
}

PLAYER_PRICE_TIERS_2018 = {
    18: ["Lionel Messi", "Cristiano Ronaldo", "Neymar", "Mohamed Salah",
         "Kylian Mbappé", "Harry Kane"],
    14: ["Antoine Griezmann", "Paul Pogba", "Eden Hazard", "Kevin De Bruyne",
         "Romelu Lukaku", "Luis Suárez", "Edinson Cavani", "Sergio Agüero",
         "Luka Modrić", "Ivan Rakitić", "Toni Kroos", "Thomas Müller",
         "Robert Lewandowski", "Diego Costa", "Isco", "James Rodríguez",
         "Heung-min Son"],
    11: ["Olivier Giroud", "Raheem Sterling", "Dele Alli", "Jesse Lingard",
         "Marcus Rashford", "Mario Mandžukić", "Ivan Perišić", "Ante Rebić",
         "Andrés Iniesta", "Sergio Busquets", "David Silva", "Marco Asensio",
         "Radamel Falcao", "Juan Cuadrado", "Roberto Firmino", "Philippe Coutinho",
         "Gabriel Jesus", "Willian", "Marcelo", "Dries Mertens", "Yannick Carrasco",
         "Mesut Özil", "Manuel Neuer", "Marc-André ter Stegen", "Hugo Lloris",
         "Thibaut Courtois", "David de Gea", "Jordan Pickford", "Alisson"],
    3:  ["Yerry Mina", "Denis Cheryshev", "Artem Dzyuba", "Aleksandr Golovin"],
}

PLAYER_PRICE_TIERS_2014 = {
    18: ["Lionel Messi", "Cristiano Ronaldo", "Neymar", "Luis Suárez",
         "Wayne Rooney", "Arjen Robben"],
    14: ["Thomas Müller", "Andrés Iniesta", "Xavi", "Eden Hazard",
         "Sergio Agüero", "Ángel Di María", "Juan Mata", "Andrea Pirlo",
         "Mario Balotelli", "Arturo Vidal", "Alexis Sánchez",
         "Edinson Cavani", "Radamel Falcao", "Toni Kroos", "Marco Reus",
         "Mesut Özil", "Karim Benzema", "Franck Ribéry", "Cesc Fàbregas",
         "Diego Costa", "David Silva", "Robin van Persie", "Wesley Sneijder",
         "Yaya Touré", "Didier Drogba"],
    11: ["James Rodríguez", "Mario Götze", "André Schürrle", "Miroslav Klose",
         "Mats Hummels", "Manuel Neuer", "Philipp Lahm", "Bastian Schweinsteiger",
         "Pepe", "Bruno Alves", "Joel Campbell", "Gonzalo Higuaín",
         "Javier Mascherano", "Sergio Romero", "Daniel Sturridge",
         "Steven Gerrard", "Cesc Fàbregas", "Jordi Alba", "Andrés Guardado",
         "Javier Hernández", "Ochoa", "Iker Casillas", "Joe Hart",
         "Hugo Lloris", "Júlio César", "Asamoah Gyan", "Tim Cahill"],
    3:  ["Enner Valencia", "Bryan Ruiz", "Keylor Navas", "Salomón Rondón"],
}

PLAYER_PRICE_TIERS_2010 = {
    18: ["Lionel Messi", "Cristiano Ronaldo", "Kaká", "Wayne Rooney",
         "Didier Drogba", "Fernando Torres"],
    14: ["David Villa", "Xavi", "Andrés Iniesta", "Frank Lampard",
         "Steven Gerrard", "Carlos Tévez", "Diego Forlán", "Gonzalo Higuaín",
         "Robinho", "Cesc Fàbregas", "Arjen Robben", "Wesley Sneijder",
         "Robin van Persie", "Samuel Eto'o", "Andrea Pirlo", "Bastian Schweinsteiger",
         "Michael Ballack", "Luís Fabiano"],
    11: ["Thomas Müller", "Mesut Özil", "Miroslav Klose", "Lukas Podolski",
         "Philipp Lahm", "Sergio Ramos", "Iker Casillas", "Carles Puyol",
         "Gianluigi Buffon", "John Terry", "Mark van Bommel",
         "Bakary Sagna", "Patrice Évra", "Maicon", "Lúcio", "Dani Alves",
         "Júlio César", "Javier Mascherano", "Ángel Di María", "Sergio Agüero",
         "Edinson Cavani", "Luis Suárez", "Asamoah Gyan", "Kevin-Prince Boateng",
         "Park Ji-sung", "Keisuke Honda"],
    3:  ["Diego Pérez", "Diego Lugano", "Maximiliano Pereira"],
}

DEFAULT_TEAM_PRICE = 1
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
    goals_against: int = 0   # used to estimate clean sheets

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
    # Position: "GK" for goalkeepers, anything else (DEF/MID/FWD/None) means
    # the player gets the non-GK clean sheet rate.
    position: str | None = None
    # Reference to which team's record drives the player's win-share + CS pts.
    team_name: str | None = None
    price: int = DEFAULT_PLAYER_PRICE

    def points(self, w: ScoringWeights, team: Team | None = None) -> float:
        pts = w.player_goal * self.goals + w.player_assist * self.assists
        if team is not None:
            # Win share (1 per team win)
            pts += w.player_win_share * team.matches_won
            # Clean sheet pts depend on position (GK vs other)
            cs = team_clean_sheets_estimate(team)
            if   self.position == "GK":  cs_rate = w.player_clean_sheet_gk
            elif self.position == "DEF": cs_rate = w.player_clean_sheet_def
            else:                          cs_rate = w.player_clean_sheet_other
            pts += cs_rate * cs
        return pts


def team_clean_sheets_estimate(team: Team) -> int:
    """Estimate clean sheets per team - always integer, since CS is binary per match.

    Uses a Poisson approximation: assume goals_against are distributed
    across matches with rate λ = GA/matches. Probability of any given
    match being a clean sheet = e^(-λ). Expected CS count = matches * e^(-λ).
    Rounded to nearest integer.

    In the live game we'll use the actual per-match CS count (always
    integer by nature); this estimate is only for historical-data sims.
    """
    import math
    matches = team.matches_won + team.matches_drawn + team.matches_lost
    if matches == 0:
        return 0
    ga = getattr(team, "goals_against", 0) or 0
    rate = ga / matches
    return round(matches * math.exp(-rate))


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
    team_tiers = {
        2022: TEAM_PRICE_TIERS_2022,
        2018: TEAM_PRICE_TIERS_2018,
        2014: TEAM_PRICE_TIERS_2014,
        2010: TEAM_PRICE_TIERS_2010,
    }[year]

    # Wikipedia's column name varies across years ('Team' vs 'Teamvte')
    team_col = next(c for c in df.columns if c.startswith("Team"))

    teams: list[Team] = []
    for _, row in df.iterrows():
        raw = str(row[team_col]).strip()
        norm = _norm_team(raw)
        gw, gd, gl = int(row["W"]), int(row["D"]), int(row["L"])

        # Find KO record - try both raw and normalized names
        ko = ko_data.get(raw) or ko_data.get(norm)
        if ko is None:
            # Some 2022 group-only teams are listed with (H) only on host
            ko = ("group", 0, 0, 0)
            # Warn so we can fix the name mapping
            print(f"  [warn] no KO data for '{raw}' / '{norm}', assuming group-only")
        round_reached, kw, kd, kl = ko

        price = _price_for(raw, team_tiers, DEFAULT_TEAM_PRICE)
        ga = int(row["GA"]) if "GA" in row else 0
        teams.append(Team(
            name=norm,
            matches_won=gw + kw,
            matches_drawn=gd + kd,
            matches_lost=gl + kl,
            final_round=round_reached,
            price=price,
            goals_against=ga,
        ))

    if format_2026:
        # Add the 16 expansion teams (their group-stage records aren't in the
        # historical CSV - they're forward-looking estimates).
        for exp_name, (gw, gd, gl) in WC2026_EXPANSION_TEAMS.items():
            teams.append(Team(
                name=exp_name,
                matches_won=gw,
                matches_drawn=gd,
                matches_lost=gl,
                final_round="group",
                price=DEFAULT_TEAM_PRICE,  # $3 - outsider tier
                goals_against=max(3, 6 - 2 * gw),  # rough proxy
            ))

    # --- Players: goalscorers JSON ---
    scorers = json.loads((DATA_DIR / f"wc{year}_goalscorers.json").read_text())
    player_tiers = {
        2022: PLAYER_PRICE_TIERS_2022,
        2018: PLAYER_PRICE_TIERS_2018,
        2014: PLAYER_PRICE_TIERS_2014,
        2010: PLAYER_PRICE_TIERS_2010,
    }[year]
    players: list[Player] = []
    # Build a map of team name → goalscorers we already have, so we can
    # add "phantom squad" entries (non-scoring squad players) per team.
    # Goalscorers JSON doesn't currently include team - we infer team from
    # tier list mappings where possible, but for the simulation we just
    # need an aggregate count, so we add phantoms attached to each team.
    scorers_by_team: dict[str, list[dict]] = {}
    for p in scorers:
        # Tag scorers to a team if known via tier list, else "Unknown"
        team_name = _team_for_player(p["player"], year)
        scorers_by_team.setdefault(team_name, []).append(p)
        price = _price_for(p["player"], player_tiers, DEFAULT_PLAYER_PRICE)
        price = max(price, pricing.min_price)
        # GKs: identify by name match in a small GK list (priced at tier 3 = $3)
        position = "GK" if _is_known_gk(p["player"]) else None
        players.append(Player(
            name=p["player"], goals=int(p["goals"]),
            position=position, team_name=team_name, price=price,
        ))

    # Add phantom squad players (~26 per team, minus known scorers, minus
    # 3 GKs since each team has ~3 GKs in their squad). Used for win-share
    # and clean-sheet point modeling. All at $2 minimum (cheapest tier).
    SQUAD_SIZE = 26
    GKS_PER_TEAM = 3
    for t in teams:
        existing = len(scorers_by_team.get(t.name, []))
        existing_gks = sum(1 for p in scorers_by_team.get(t.name, [])
                            if _is_known_gk(p["player"]))
        # Add GKs to fill out 3 per team
        gk_needed = max(0, GKS_PER_TEAM - existing_gks)
        for i in range(gk_needed):
            players.append(Player(
                name=f"{t.name}-GK{i+1}", goals=0,
                position="GK", team_name=t.name,
                price=max(3, pricing.min_price),
            ))
        # Fill remaining squad slots with outfield phantoms
        outfield_needed = max(0, SQUAD_SIZE - existing - gk_needed)
        for i in range(outfield_needed):
            players.append(Player(
                name=f"{t.name}-sq{i+1}", goals=0,
                position=None, team_name=t.name,
                price=pricing.min_price,
            ))
    return teams, players


# Small helpers used by load_year for tagging team + position.

def _team_for_player(name: str, year: int) -> str:
    """Look up which team a player belonged to, via the price tier lists.
    Returns 'Unknown' if not findable (rare - sim ignores those for win-share)."""
    tiers = {
        2022: PLAYER_PRICE_TIERS_2022,
        2018: PLAYER_PRICE_TIERS_2018,
        2014: PLAYER_PRICE_TIERS_2014,
        2010: PLAYER_PRICE_TIERS_2010,
    }[year]
    # The price tier dict doesn't carry team info - fall back to known
    # team lookups for the most prolific scorers.
    KNOWN = {
        # 2022
        "Kylian Mbappé": "France", "Olivier Giroud": "France",
        "Antoine Griezmann": "France", "Ousmane Dembélé": "France",
        "Aurélien Tchouaméni": "France", "Theo Hernández": "France",
        "Hugo Lloris": "France",
        "Lionel Messi": "Argentina", "Julián Álvarez": "Argentina",
        "Ángel Di María": "Argentina", "Lautaro Martínez": "Argentina",
        "Alexis Mac Allister": "Argentina", "Enzo Fernández": "Argentina",
        "Nahuel Molina": "Argentina", "Emiliano Martínez": "Argentina",
        "Neymar": "Brazil", "Vinícius Júnior": "Brazil", "Richarlison": "Brazil",
        "Casemiro": "Brazil", "Lucas Paquetá": "Brazil", "Rodrygo": "Brazil",
        "Alisson": "Brazil",
        "Harry Kane": "England", "Bukayo Saka": "England",
        "Marcus Rashford": "England", "Mason Mount": "England",
        "Phil Foden": "England", "Raheem Sterling": "England",
        "Jude Bellingham": "England", "Jordan Henderson": "England",
        "Declan Rice": "England",
        "Álvaro Morata": "Spain", "Ferran Torres": "Spain",
        "Pedri": "Spain", "Gavi": "Spain", "Unai Simón": "Spain",
        "Cody Gakpo": "Netherlands", "Memphis Depay": "Netherlands",
        "Frenkie de Jong": "Netherlands", "Andries Noppert": "Netherlands",
        "Bruno Fernandes": "Portugal", "Cristiano Ronaldo": "Portugal",
        "Gonçalo Ramos": "Portugal", "João Félix": "Portugal",
        "Bernardo Silva": "Portugal", "Rafael Leão": "Portugal",
        "Ivan Perišić": "Croatia", "Andrej Kramarić": "Croatia",
        "Luka Modrić": "Croatia", "Dominik Livaković": "Croatia",
        "Hakim Ziyech": "Morocco", "Achraf Hakimi": "Morocco",
        "Sofyan Amrabat": "Morocco", "Youssef En-Nesyri": "Morocco",
        "Yassine Bounou": "Morocco",
        "Enner Valencia": "Ecuador",
        "Vincent Aboubakar": "Cameroon",
        "Robert Lewandowski": "Poland", "Wojciech Szczęsny": "Poland",
        "Sadio Mané": "Senegal", "Édouard Mendy": "Senegal",
        "Kalidou Koulibaly": "Senegal",
        "Heung-min Son": "South Korea",
        "Mohammed Kudus": "Ghana",
        "Mehdi Taremi": "Iran",
        "Ritsu Dōan": "Japan", "Cho Gue-sung": "South Korea",
        "Wout Weghorst": "Netherlands",
        "Salem Al-Dawsari": "Saudi Arabia",
        "Aleksandar Mitrović": "Serbia",
        "Breel Embolo": "Switzerland",
        "Giorgian de Arrascaeta": "Uruguay",
        "Niclas Füllkrug": "Germany", "Manuel Neuer": "Germany",
        "Kai Havertz": "Germany", "Jamal Musiala": "Germany",
        "Serge Gnabry": "Germany",
        "Thibaut Courtois": "Belgium",
        # 2018
        "Romelu Lukaku": "Belgium", "Eden Hazard": "Belgium",
        "Kevin De Bruyne": "Belgium", "Dries Mertens": "Belgium",
        "Yannick Carrasco": "Belgium", "Nacer Chadli": "Belgium",
        "Michy Batshuayi": "Belgium",
        "Paul Pogba": "France", "Mbappé": "France",
        "Diego Costa": "Spain", "Isco": "Spain", "Andrés Iniesta": "Spain",
        "Sergio Busquets": "Spain", "David Silva": "Spain",
        "Marco Asensio": "Spain", "David de Gea": "Spain",
        "Luis Suárez": "Uruguay", "Edinson Cavani": "Uruguay",
        "Toni Kroos": "Germany", "Thomas Müller": "Germany",
        "Mesut Özil": "Germany", "Marc-André ter Stegen": "Germany",
        "Yerry Mina": "Colombia", "James Rodríguez": "Colombia",
        "Radamel Falcao": "Colombia", "Juan Cuadrado": "Colombia",
        "Roberto Firmino": "Brazil", "Philippe Coutinho": "Brazil",
        "Gabriel Jesus": "Brazil", "Willian": "Brazil", "Marcelo": "Brazil",
        "Denis Cheryshev": "Russia", "Artem Dzyuba": "Russia",
        "Mario Mandžukić": "Croatia", "Ivan Rakitić": "Croatia",
        "Ante Rebić": "Croatia",
        "Sergio Agüero": "Argentina",
        "Dele Alli": "England", "Jesse Lingard": "England",
        "Jordan Pickford": "England",
        "Mohamed Salah": "Egypt",
        "Mile Jedinak": "Australia",
        "John Stones": "England",
        "Takashi Inui": "Japan",
        "Ahmed Musa": "Nigeria",
        "Andreas Granqvist": "Sweden",
        "Wahbi Khazri": "Tunisia",
        "Aleksandr Golovin": "Russia",
        # 2014
        "Robin van Persie": "Netherlands", "Arjen Robben": "Netherlands",
        "Wesley Sneijder": "Netherlands", "Memphis Depay": "Netherlands",
        "Klaas-Jan Huntelaar": "Netherlands", "Stefan de Vrij": "Netherlands",
        "Tim Cahill": "Australia",
        "André Schürrle": "Germany", "Mario Götze": "Germany",
        "Miroslav Klose": "Germany", "Mats Hummels": "Germany",
        "Mesut Özil": "Germany",
        "James Rodríguez": "Colombia", "Juan Cuadrado": "Colombia",
        "Pablo Armero": "Colombia", "Juan Quintero": "Colombia",
        "Jackson Martínez": "Colombia", "Carlos Bacca": "Colombia",
        "Karim Benzema": "France",
        "Mathieu Valbuena": "France", "Olivier Giroud": "France",
        "Paul Pogba": "France", "Antoine Griezmann": "France",
        "Blaise Matuidi": "France",
        "Joel Campbell": "Costa Rica", "Bryan Ruiz": "Costa Rica",
        "Keylor Navas": "Costa Rica", "Óscar Duarte": "Costa Rica",
        "Marcos Ureña": "Costa Rica",
        "Enner Valencia": "Ecuador",  # also a 2022 player; 2014 was Ecuador both times
        "David Luiz": "Brazil", "Oscar": "Brazil", "Fred": "Brazil",
        "Júlio César": "Brazil",
        "Gonzalo Higuaín": "Argentina", "Marcos Rojo": "Argentina",
        "Ángel Di María": "Argentina", "Sergio Agüero": "Argentina",
        "Javier Mascherano": "Argentina", "Sergio Romero": "Argentina",
        "Daniel Sturridge": "England", "Steven Gerrard": "England",
        "Wayne Rooney": "England",
        "Mario Balotelli": "Italy", "Andrea Pirlo": "Italy",
        "Claudio Marchisio": "Italy",
        "Andrés Guardado": "Mexico", "Javier Hernández": "Mexico",
        "Oribe Peralta": "Mexico", "Rafael Márquez": "Mexico",
        "Guillermo Ochoa": "Mexico",
        "Andre Ayew": "Ghana", "Asamoah Gyan": "Ghana",
        "Salomón Rondón": "Venezuela",  # not in 2014 actually - Venezuela didn't qualify
        # 2010
        "Diego Forlán": "Uruguay", "Luis Suárez": "Uruguay",
        "Edinson Cavani": "Uruguay", "Diego Pérez": "Uruguay",
        "Diego Lugano": "Uruguay", "Maximiliano Pereira": "Uruguay",
        "Álvaro Pereira": "Uruguay",
        "David Villa": "Spain", "Andrés Iniesta": "Spain", "Xavi": "Spain",
        "Iker Casillas": "Spain", "Carles Puyol": "Spain",
        "Sergio Ramos": "Spain", "Fernando Torres": "Spain",
        "Cesc Fàbregas": "Spain",
        "Wesley Sneijder": "Netherlands", "Arjen Robben": "Netherlands",
        "Robin van Persie": "Netherlands", "Mark van Bommel": "Netherlands",
        "Thomas Müller": "Germany", "Miroslav Klose": "Germany",
        "Mesut Özil": "Germany", "Lukas Podolski": "Germany",
        "Bastian Schweinsteiger": "Germany", "Philipp Lahm": "Germany",
        "Michael Ballack": "Germany",  # didn't actually play - injured
        "Luís Fabiano": "Brazil", "Robinho": "Brazil", "Kaká": "Brazil",
        "Maicon": "Brazil", "Lúcio": "Brazil", "Dani Alves": "Brazil",
        "Carlos Tévez": "Argentina", "Lionel Messi": "Argentina",
        "Diego Maradona": "Argentina",  # coach
        "Asamoah Gyan": "Ghana", "Kevin-Prince Boateng": "Ghana",
        "Andre Ayew": "Ghana",  # was on 2010 squad? actually I'm not sure
        "Park Ji-sung": "South Korea",
        "Keisuke Honda": "Japan",
        "Wayne Rooney": "England", "Frank Lampard": "England",
        "Steven Gerrard": "England",
        "Cristiano Ronaldo": "Portugal", "Pepe": "Portugal",
        "Didier Drogba": "Côte d'Ivoire", "Samuel Eto'o": "Cameroon",
        "Andrea Pirlo": "Italy", "Gianluigi Buffon": "Italy",
        "Róbert Vittek": "Slovakia",
    }
    return KNOWN.get(name, "Unknown")


_KNOWN_GKS = {
    # 2022
    "Emiliano Martínez", "Hugo Lloris", "Thibaut Courtois", "Alisson",
    "Andries Noppert", "Dominik Livaković", "Édouard Mendy",
    "Manuel Neuer", "Unai Simón", "Wojciech Szczęsny", "Yassine Bounou",
    # 2018
    "David de Gea", "Marc-André ter Stegen", "Jordan Pickford",
    # 2014
    "Keylor Navas", "Guillermo Ochoa", "Júlio César", "Sergio Romero",
    "Iker Casillas", "Joe Hart", "Hugo Lloris",
    # 2010
    "Gianluigi Buffon",
}

def _is_known_gk(name: str) -> bool:
    return name in _KNOWN_GKS


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
    teams_by_name = {t.name: t for t in teams}
    out = [Asset(t.name, "team", t.points(w, format_2026), t.price) for t in teams]
    out += [Asset(p.name, "player",
                  p.points(w, teams_by_name.get(p.team_name)), p.price)
            for p in players]
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
    """restrict: None | 'team' | 'player' - limit asset pool to one kind."""
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

    # Top 10 teams by raw points
    print(f"\nTop 10 teams by raw points:")
    print(f"  {'team':<22} {'final':<6} {'W-D-L':<7} {'GA':<3} {'CS~':<4} {'price':>5} {'pts':>5}")
    for t in sorted(teams, key=lambda x: x.points(w, format_2026), reverse=True)[:10]:
        cs = team_clean_sheets_estimate(t)
        print(f"  {t.name[:21]:<22} {t.final_round:<6} "
              f"{t.matches_won}-{t.matches_drawn}-{t.matches_lost:<3} "
              f"{t.goals_against:<3d} {cs:<4.1f} "
              f"${t.price:>4d} {t.points(w, format_2026):>5.1f}")

    # Top 10 players by raw points (showing scoring breakdown)
    print(f"\nTop 10 players by raw points (g=goal pts, w=win share, c=clean sheet pts):")
    print(f"  {'player':<28} {'team':<14} {'pos':<3} {'price':>5} {'g':>2} {'goalpts':>7} {'winshr':>7} {'CSpts':>6} {'total':>6}")
    teams_by_name = {t.name: t for t in teams}
    scored_players = []
    for p in players:
        t = teams_by_name.get(p.team_name)
        if t is None:
            continue
        goal_pts = w.player_goal * p.goals
        win_share_pts = w.player_win_share * t.matches_won
        cs = team_clean_sheets_estimate(t)
        if   p.position == "GK":  cs_rate = w.player_clean_sheet_gk
        elif p.position == "DEF": cs_rate = w.player_clean_sheet_def
        else:                       cs_rate = w.player_clean_sheet_other
        cs_pts = cs_rate * cs
        total = goal_pts + win_share_pts + cs_pts + w.player_assist * p.assists
        scored_players.append((p, t, goal_pts, win_share_pts, cs_pts, total))
    for p, t, gp, wp, cp, tot in sorted(scored_players, key=lambda x: x[5], reverse=True)[:10]:
        pos = p.position or " - "
        team_short = (p.team_name or "?")[:13]
        print(f"  {p.name[:27]:<28} {team_short:<14} {pos:<3} "
              f"${p.price:>4d} {p.goals:>2d} {gp:>7.1f} {wp:>7.1f} {cp:>6.1f} {tot:>6.1f}")

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
    # Goals-only baseline (no CS, no win share) - for comparison.
    ("A. goals-only baseline",
     ScoringWeights(player_clean_sheet_gk=0, player_clean_sheet_def=0, player_clean_sheet_other=0,
                     player_win_share=0),
     PlayerPricing(min_price=2)),
    # F. Calibrated to bring champion ROI in line with top scorer ROI.
    # Smaller per-round bonuses, smaller champion premium. All integers.
    # Target: champion ≈ 1.4x top scorer (not 2.2x). Same ~$15 price band.
    ("F. RECOMMENDED: integer weights, less high-end team variance",
     ScoringWeights(
        team_win=3, team_draw=1,
        bonus_r32=1, bonus_r16=2, bonus_qf=3, bonus_sf=5,
        bonus_final=8, bonus_champion=12,
        # Player scoring: user's design, all integer
        player_goal=5, player_assist=0,
        player_clean_sheet_gk=5, player_clean_sheet_def=2, player_clean_sheet_other=0,
        player_win_share=1,
     ),
     PlayerPricing(min_price=2)),
]


PRICE_BUCKETS = [
    (25, 30, "$25-30  (premium)"),
    (18, 24, "$18-24  (top)"),
    (13, 17, "$13-17  (high)"),
    (8, 12,  "$ 8-12  (mid)"),
    (4,  7,  "$ 4- 7  (low)"),
    (1,  3,  "$ 1- 3  (floor)"),
]


def _bucket_for_price(price: int) -> str | None:
    for lo, hi, label in PRICE_BUCKETS:
        if lo <= price <= hi:
            return label
    return None


def bucket_return_analysis() -> None:
    """Group historical assets into price BUCKETS (not exact tiers) and
    report per-bucket: count, mean/median/p25/p75 pts, pts/$ at midpoint,
    and the breakeven-rate (% of assets that returned ≥ their price).

    Balanced game ≈ comparable mean pts/$ across buckets, with bigger
    variance at higher buckets."""
    from collections import defaultdict
    import statistics

    weights = ScoringWeights()
    pricing = PlayerPricing(min_price=1)

    team_by_bucket   = defaultdict(list)   # bucket → list of (price, points)
    player_by_bucket = defaultdict(list)

    for year in (2010, 2014, 2018, 2022):
        teams, players = load_year(year, pricing, format_2026=True)
        teams_by_name = {t.name: t for t in teams}
        for t in teams:
            bucket = _bucket_for_price(t.price)
            if bucket: team_by_bucket[bucket].append((t.price, t.points(weights, format_2026=True)))
        for p in players:
            team = teams_by_name.get(p.team_name)
            if team is None: continue
            bucket = _bucket_for_price(p.price)
            if bucket: player_by_bucket[bucket].append((p.price, p.points(weights, team)))

    def _print_block(label: str, data: dict) -> None:
        print(f"\n{label}")
        print(f"  {'bucket':<22} {'n':>5} {'mean':>6} {'med':>5} {'p25':>5} {'p75':>5} "
              f"{'min':>4} {'max':>4} {'pts/$':>6} {'break':>6}")
        for lo, hi, b_label in PRICE_BUCKETS:
            rows = data.get(b_label, [])
            if not rows:
                continue
            pts_only = [pts for (_, pts) in rows]
            prices = [pr for (pr, _) in rows]
            pts_sorted = sorted(pts_only)
            n = len(pts_only)
            mean = statistics.mean(pts_only)
            med = statistics.median(pts_only)
            p25 = pts_sorted[n // 4] if n >= 4 else pts_sorted[0]
            p75 = pts_sorted[(3 * n) // 4] if n >= 4 else pts_sorted[-1]
            midpoint = (lo + hi) / 2
            # breakeven = fraction of assets that returned ≥ their price
            be_count = sum(1 for (pr, pts) in rows if pts >= pr)
            breakeven = 100 * be_count / n
            print(f"  {b_label:<22} {n:>5} {mean:>6.1f} {med:>5.0f} {p25:>5.0f} {p75:>5.0f} "
                  f"{min(pts_only):>4.0f} {max(pts_only):>4.0f} "
                  f"{mean/midpoint:>6.2f} {breakeven:>5.0f}%")

    print(f"\n{'='*80}")
    print("  PRICE-BUCKET ROI - averages across WC 2010, 2014, 2018, 2022")
    print(f"  (locked scoring weights, 2026 48-team format, $1-$30 spread)")
    print(f"{'='*80}")
    print("\n  pts/$  = mean pts ÷ bucket midpoint  (higher = better ROI in bucket)")
    print("  break  = % of assets that returned ≥ their price  (the 'paid off' rate)")

    _print_block("TEAMS:", team_by_bucket)
    _print_block("PLAYERS (named goalscorers from historical data):", player_by_bucket)


def tier_return_analysis() -> None:
    """[Legacy] For each price tier, across all 4 historical World Cups, report
    average + median actual points earned. Lets us sanity-check whether
    each tier is paying off proportionally."""
    from collections import defaultdict
    import statistics

    weights = ScoringWeights()  # use the locked defaults
    pricing = PlayerPricing(min_price=2)

    team_by_tier: dict[int, list[float]] = defaultdict(list)
    player_by_tier: dict[int, list[float]] = defaultdict(list)
    # Track scored-only separately so we can show the contrast.
    player_by_tier_scored_only: dict[int, list[float]] = defaultdict(list)

    for year in (2010, 2014, 2018, 2022):
        teams, players = load_year(year, pricing, format_2026=True)
        for t in teams:
            team_by_tier[t.price].append(t.points(weights, format_2026=True))
        teams_by_name = {t.name: t for t in teams}
        for p in players:
            t = teams_by_name.get(p.team_name)
            if t is None:
                continue
            pts = p.points(weights, t)
            player_by_tier[p.price].append(pts)
            if p.goals > 0:
                player_by_tier_scored_only[p.price].append(pts)

    print(f"\n{'='*78}")
    print(f"  TIER RETURN ANALYSIS - averages across WC 2010, 2014, 2018, 2022")
    print(f"  (using locked scoring weights; teams + players adapted to 2026 format)")
    print(f"{'='*78}")

    print(f"\nTEAMS:")
    print(f"  {'price':>5} {'n':>4} {'mean':>6} {'median':>6} {'p25':>5} {'p75':>5} {'min':>4} {'max':>4} {'pts/$':>6}")
    for price in sorted(team_by_tier.keys(), reverse=True):
        pts = team_by_tier[price]
        if not pts:
            continue
        pts_sorted = sorted(pts)
        n = len(pts)
        mean = statistics.mean(pts)
        med = statistics.median(pts)
        p25 = pts_sorted[n // 4]
        p75 = pts_sorted[(3 * n) // 4]
        print(f"  ${price:>4} {n:>4} {mean:>6.1f} {med:>6.1f} {p25:>5.1f} {p75:>5.1f} "
              f"{min(pts):>4.0f} {max(pts):>4.0f} {mean/price:>6.2f}")

    print(f"\nPLAYERS (ALL squad members - what you'd ACTUALLY draft from):")
    print(f"  {'price':>5} {'n':>4} {'mean':>6} {'median':>6} {'p25':>5} {'p75':>5} {'min':>4} {'max':>4} {'pts/$':>6}")
    for price in sorted(player_by_tier.keys(), reverse=True):
        pts = player_by_tier[price]
        if not pts:
            continue
        pts_sorted = sorted(pts)
        n = len(pts)
        mean = statistics.mean(pts)
        med = statistics.median(pts)
        p25 = pts_sorted[n // 4]
        p75 = pts_sorted[(3 * n) // 4]
        print(f"  ${price:>4} {n:>4} {mean:>6.1f} {med:>6.1f} {p25:>5.1f} {p75:>5.1f} "
              f"{min(pts):>4.0f} {max(pts):>4.0f} {mean/price:>6.2f}")

    print(f"\nPLAYERS (scorers-only subset - for context: 'if they DID score, what was their return?'):")
    print(f"  {'price':>5} {'n':>4} {'mean':>6} {'median':>6} {'p25':>5} {'p75':>5} {'min':>4} {'max':>4} {'pts/$':>6}")
    for price in sorted(player_by_tier_scored_only.keys(), reverse=True):
        pts = player_by_tier_scored_only[price]
        if not pts:
            continue
        pts_sorted = sorted(pts)
        n = len(pts)
        mean = statistics.mean(pts)
        med = statistics.median(pts)
        p25 = pts_sorted[n // 4]
        p75 = pts_sorted[(3 * n) // 4]
        print(f"  ${price:>4} {n:>4} {mean:>6.1f} {med:>6.1f} {p25:>5.1f} {p75:>5.1f} "
              f"{min(pts):>4.0f} {max(pts):>4.0f} {mean/price:>6.2f}")


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
    print("  BALANCE SUMMARY - lower gap% and higher mix-advantage% are better")
    print(f"{'='*78}")
    print(f"{'preset':<38} {'yr':<5} {'fmt':<5} {'team':>5} {'plr':>5} {'mix':>5} {'gap%':>5} {'mix+%':>6}")
    for r in summary_rows:
        print(f"{r['preset']:<38} {r['year']:<5} {r['format']:<5} {r['team_pts']:>5.0f} "
              f"{r['player_pts']:>5.0f} {r['mix_pts']:>5.0f} "
              f"{r['gap_pct']:>4.0f}% {r['mix_advantage_pct']:>5.0f}%")


if __name__ == "__main__":
    import sys
    if "--buckets" in sys.argv:
        bucket_return_analysis()
    elif "--tier-returns" in sys.argv:
        tier_return_analysis()
    else:
        main()
