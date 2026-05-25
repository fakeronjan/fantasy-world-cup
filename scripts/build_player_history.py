"""Build docs/data/player_history.json - accurate version.

Strategy:
  1. Start with the WC 2010/2014/2018/2022 goalscorer JSON files we
     already have (clean Wikipedia-table data). For each player in
     seed_players.json, see if their name matches any of those
     goalscorers, and if so credit them with: goals in that year,
     participation that year, title/runner-up if their CURRENT team
     was the year's champion or runner-up.
  2. Layer hand-curated overrides from player_history_overrides.json
     on top (overrides take full precedence; intended for top ~50
     marquee names where the auto-derivation is incomplete - e.g.,
     goalkeepers, non-scoring stars, players who participated without
     scoring).
  3. Write the merged result to docs/data/player_history.json.

Limitations of the auto-derivation:
  - Players who played in past WCs but didn't score appear as
    "WC debut" (we have no full squad lists for past WCs). Hand-fix
    via overrides for any high-profile non-scorers.
  - Assumes a player's current national team is the same as past
    (true ~99% of the time).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PLAYERS = ROOT / "docs" / "data" / "seed_players.json"
DATA_DIR     = ROOT / "data"
OVERRIDES    = ROOT / "docs" / "data" / "player_history_overrides.json"
OUT          = ROOT / "docs" / "data" / "player_history.json"

# Year → official tournament result
WC_RESULTS = {
    2010: {"champion": "Spain",     "runner_up": "Netherlands"},
    2014: {"champion": "Germany",   "runner_up": "Argentina"},
    2018: {"champion": "France",    "runner_up": "Croatia"},
    2022: {"champion": "Argentina", "runner_up": "France"},
}


# ---------------------------------------------------------------------------
# Name normalization - handles accents, special chars, etc.
# Same logic as build_seed_players.py to keep matching consistent.
# ---------------------------------------------------------------------------

REPLACEMENTS = {
    "Ø": "O", "ø": "o", "Å": "A", "å": "a", "Æ": "Ae", "æ": "ae",
    "Œ": "Oe", "œ": "oe", "Ł": "L", "ł": "l", "Đ": "D", "đ": "d",
    "İ": "I", "ı": "i", "ß": "ss", "Þ": "Th", "þ": "th",
    "Ð": "D", "ð": "d",
}


def _norm(s: str) -> str:
    for k, v in REPLACEMENTS.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9 -]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def names_match(a: str, b: str) -> bool:
    """Match player names. Exact normalized first, then substring (one contains
    the other). Avoids false positives like 'Salah' matching 'Salah-Eddine'
    by requiring at least one full token to overlap.
    """
    na = _norm(a)
    nb = _norm(b)
    if na == nb:
        return True
    # Substring containment with token boundary
    if na in nb or nb in na:
        # Require both to share at least one full word ≥3 chars
        toks_a = set(na.split())
        toks_b = set(nb.split())
        common = {t for t in (toks_a & toks_b) if len(t) >= 3}
        return bool(common)
    return False


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build() -> None:
    seed_players = json.loads(SEED_PLAYERS.read_text())
    historical: dict[int, list[dict]] = {}
    for year in WC_RESULTS:
        path = DATA_DIR / f"wc{year}_goalscorers.json"
        if path.exists():
            historical[year] = json.loads(path.read_text())
        else:
            print(f"  ! missing {path}, skipping {year}", file=sys.stderr)
            historical[year] = []

    auto: dict[str, dict] = {}
    for p in seed_players:
        # Skip the meta key if seed file shape changes
        if not isinstance(p, dict) or "id" not in p:
            continue
        pid = p["id"]
        team_name = p.get("teamName", "")
        wc_years = []
        wc_goals = 0
        title_years = []
        runner_up_years = []

        for year, scorers in historical.items():
            for s in scorers:
                if names_match(p["name"], s["player"]):
                    wc_years.append(year)
                    wc_goals += int(s.get("goals", 0))
                    res = WC_RESULTS[year]
                    if team_name == res["champion"]:
                        title_years.append(year)
                    elif team_name == res["runner_up"]:
                        runner_up_years.append(year)
                    break  # don't double-count if multiple matches in a year

        auto[pid] = {
            "wcsPlayed": len(wc_years),
            "wcYears": sorted(set(wc_years)),
            "goals": wc_goals,
            "titles": len(title_years),
            "titleYears": sorted(set(title_years)),
            "runnerUps": len(runner_up_years),
            "runnerUpYears": sorted(set(runner_up_years)),
            "_source": "historical-goals",
        }

    # Layer overrides on top - skip keys that aren't player IDs (e.g. "_meta").
    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    for k, v in overrides.items():
        if not isinstance(v, dict):
            continue
        auto[k] = {**v, "_source": "manual"}

    OUT.write_text(json.dumps(auto, indent=2, ensure_ascii=False))

    n_auto = sum(1 for v in auto.values() if v.get("_source") == "historical-goals")
    n_manual = sum(1 for v in auto.values() if v.get("_source") == "manual")
    n_with_goals = sum(1 for v in auto.values() if (v.get("goals") or 0) > 0)
    n_with_titles = sum(1 for v in auto.values() if (v.get("titles") or 0) > 0)
    n_debut = sum(1 for v in auto.values() if v.get("wcsPlayed", 0) == 0)

    print(f"Wrote {len(auto)} player history entries → {OUT}")
    print(f"  historical-goals derived: {n_auto}")
    print(f"  manual overrides:         {n_manual}")
    print(f"  players with WC goals:    {n_with_goals}")
    print(f"  players with WC title:    {n_with_titles}")
    print(f"  WC debuts (no history):   {n_debut}")


if __name__ == "__main__":
    build()
