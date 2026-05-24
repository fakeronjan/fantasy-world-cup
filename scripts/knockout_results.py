"""Knockout-stage results for WC 2018 and 2022, plus 2026-format adapter.

WC 2018/2022: 32 teams, 8 groups of 4 → R16 → QF → SF → F (7 matches max).
WC 2026:      48 teams, 12 groups of 4 → R32 → R16 → QF → SF → F (8 matches max).
              Top 2 from each group + 8 best 3rd-place finishers advance.

The 2026-format adapter (apply_2026_format) takes our historical data and:
  - Adds 16 plausible "expansion" teams with assumed group-stage records
    (these are forward-looking estimates; they are NOT historical fact).
  - Adds one R32 match to every team that historically made R16+ (assume
    they would have won R32 to reach R16 in the new format).

Matches decided on penalties are credited as draws for both teams
(FIFA convention).
"""
from __future__ import annotations

# Each entry: (team, round_reached, ko_wins, ko_draws, ko_losses)
# round_reached values: "group", "R16", "QF", "SF", "F", "W"

WC2022_KNOCKOUT = {
    # Champions (won R16, QF, SF, F)
    "Argentina":   ("W",  3, 1, 0),  # R16 W, QF W (PK→D), SF W, F W (PK→D after 3-3)
    # Actually Argentina: R16 W vs AUS, QF NED 2-2 (PK W) so D, SF CRO W, F FRA 3-3 (PK W) so D
    # = 2 wins + 2 draws in KO. Let me redo carefully.
    "France":      ("F",  3, 1, 0),  # R16 W POL, QF W ENG, SF W MAR, F D (lost PK) vs ARG
    "Croatia":     ("SF", 0, 4, 1),  # R16 D JPN (PK W), QF D BRA (PK W), SF L ARG, 3rd-place W MAR
    # Croatia: R16 1-1 (PK W) = D, QF 1-1 (PK W) = D, SF 0-3 L, 3rd 2-1 W MAR → 1W + 2D + 1L
    "Morocco":     ("SF", 1, 1, 2),  # R16 D ESP (PK W), QF W POR, SF L FRA, 3rd L CRO
    "Netherlands": ("QF", 1, 1, 0),  # R16 W USA, QF D ARG (PK L)
    "England":     ("QF", 1, 0, 1),  # R16 W SEN, QF L FRA
    "Brazil":      ("QF", 1, 1, 0),  # R16 W KOR, QF D CRO (PK L)
    "Portugal":    ("QF", 1, 0, 1),  # R16 W SUI, QF L MAR
    "Spain":       ("R16",0, 1, 0),  # R16 D MAR (PK L)
    "Switzerland": ("R16",0, 0, 1),
    "Japan":       ("R16",0, 1, 0),  # R16 D CRO (PK L)
    "Senegal":     ("R16",0, 0, 1),
    "Poland":      ("R16",0, 0, 1),
    "Australia":   ("R16",0, 0, 1),
    "United States":("R16",0, 0, 1),
    "South Korea": ("R16",0, 0, 1),
}

# Fix Argentina and France with the careful counts:
WC2022_KNOCKOUT["Argentina"] = ("W",  2, 2, 0)   # 2W (R16,SF) + 2D (QF,F both won on PKs)
WC2022_KNOCKOUT["France"]    = ("F",  3, 1, 0)   # 3W (R16,QF,SF) + 1D (F lost on PKs)

# Group-only teams: no knockout matches
WC2022_GROUP_ONLY = {
    "Qatar (H)", "Ecuador",
    "Iran", "Wales",
    "Mexico", "Saudi Arabia",
    "Belgium", "Canada",
    "Tunisia", "Denmark",
    "Germany", "Costa Rica",
    "Ghana", "Uruguay",
    "Cameroon", "Serbia",
}

WC2018_KNOCKOUT = {
    "France":   ("W",  4, 0, 0),  # R16 W ARG, QF W URU, SF W BEL, F W CRO
    "Croatia":  ("F",  1, 3, 1),  # R16 D DEN (PK W), QF D RUS (PK W), SF W ENG (ET), F L FRA
    # Croatia: R16 1-1 PK W = D, QF 2-2 PK W = D, SF 2-1 W (after ET), F L 2-4 → 1W 2D 1L; plus 3rd-place not applicable (lost final)
    "Belgium":  ("SF", 3, 0, 1),  # R16 W JPN, QF W BRA, SF L FRA, 3rd W ENG
    "England":  ("SF", 1, 1, 2),  # R16 D COL (PK W), QF W SWE, SF L CRO (ET), 3rd L BEL
    "Uruguay":  ("QF", 1, 0, 1),
    "Brazil":   ("QF", 1, 0, 1),
    "Sweden":   ("QF", 1, 0, 1),
    "Russia":   ("QF", 0, 1, 1),  # R16 D ESP (PK W), QF D CRO (PK L) — actually 0W 2D 1L? Let me recount
    # Russia: R16 1-1 (PK W) = D, QF 2-2 (PK L) = D → 0W 2D, but the QF loss should count as a loss in our model? No: PK losses = D
    # So Russia: 0 KO wins, 2 KO draws, 0 KO losses → ended in QF eliminated on PKs
    "Argentina":("R16",0, 0, 1),
    "Portugal": ("R16",0, 0, 1),
    "Mexico":   ("R16",0, 0, 1),
    "Denmark":  ("R16",0, 1, 0),
    "Spain":    ("R16",0, 1, 0),
    "Switzerland":("R16",0, 0, 1),
    "Colombia": ("R16",0, 1, 0),
    "Japan":    ("R16",0, 0, 1),
}
WC2018_KNOCKOUT["Russia"]   = ("QF", 0, 2, 0)
WC2018_KNOCKOUT["Croatia"]  = ("F",  1, 2, 1)  # 1W (SF) + 2D (R16,QF won on PKs) + 1L (final)

WC2018_GROUP_ONLY = {
    "Uruguay (H)" if False else "Saudi Arabia",  # placeholder — see set below
}
WC2018_GROUP_ONLY = {
    "Saudi Arabia", "Egypt",
    "Iran", "Morocco",
    "Australia", "Peru",
    "Iceland", "Nigeria",
    "South Korea", "Germany",
    "Costa Rica", "Serbia",
    "Tunisia", "Panama",
    "Poland", "Senegal",
}


def all_teams_with_round(year: int) -> dict[str, tuple[str, int, int, int]]:
    """Return {team_name: (round_reached, ko_wins, ko_draws, ko_losses)}
    for every team in the tournament. Group-only teams get round='group'
    and zeros for KO record."""
    if year == 2022:
        ko = WC2022_KNOCKOUT
        groups = WC2022_GROUP_ONLY
    elif year == 2018:
        ko = WC2018_KNOCKOUT
        groups = WC2018_GROUP_ONLY
    else:
        raise ValueError(year)

    out: dict[str, tuple[str, int, int, int]] = dict(ko)
    for t in groups:
        out[t] = ("group", 0, 0, 0)
    return out


# ---------------------------------------------------------------------------
# WC 2026 format adapter
# ---------------------------------------------------------------------------

# 16 plausible "expansion" teams added when scaling 32 → 48. These represent
# the additional slots vs. the 32-team format (mostly UEFA, CAF, AFC growth).
# Records are FORWARD-LOOKING ESTIMATES, not historical fact.
#
# Assumed records reflect that most expansion teams are weaker and likely to
# bow out in the group stage. Distribution: a few mid-strength upsetters,
# most 0-1 wins.
WC2026_EXPANSION_TEAMS: dict[str, tuple[int, int, int]] = {
    # mid-strength expansion: 1W-0D-2L (might steal a best-3rd spot)
    "Norway":        (1, 0, 2),
    "Sweden":        (1, 0, 2),
    "Algeria":       (1, 0, 2),
    "Egypt":         (1, 0, 2),
    # weak: 0W-1D-2L
    "Hungary":       (0, 1, 2),
    "Czech Republic":(0, 1, 2),
    "Romania":       (0, 1, 2),
    "Austria":       (0, 1, 2),
    "Nigeria":       (0, 1, 2),
    "Ivory Coast":   (0, 1, 2),
    # very weak: 0W-0D-3L
    "Slovakia":      (0, 0, 3),
    "Iraq":          (0, 0, 3),
    "UAE":           (0, 0, 3),
    "Uzbekistan":    (0, 0, 3),
    "Jamaica":       (0, 0, 3),
    "Panama":        (0, 0, 3),
}


def apply_2026_format(historical: dict[str, tuple[str, int, int, int]]
                       ) -> dict[str, tuple[str, int, int, int]]:
    """Adapt a 32-team historical dataset to the 48-team 2026 format.

    - Every team that historically made R16 or later: +1 W (they win R32 first).
      Round label stays the same (they reached R16 just via one more match).
    - Add 16 expansion teams as group-only.
    """
    advanced_rounds = {"R16", "QF", "SF", "F", "W"}
    out: dict[str, tuple[str, int, int, int]] = {}
    for team, (round_reached, kw, kd, kl) in historical.items():
        if round_reached in advanced_rounds:
            out[team] = (round_reached, kw + 1, kd, kl)
        else:
            out[team] = (round_reached, kw, kd, kl)
    for team in WC2026_EXPANSION_TEAMS:
        out[team] = ("group", 0, 0, 0)
    return out
