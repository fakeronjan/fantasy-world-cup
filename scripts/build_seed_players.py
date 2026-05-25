"""Build docs/data/seed_players.json from the pulled WC 2026 squads.

For each of the 1,200+ players, assign a price tier:
  Tier 1 ($10): global megastars (~10)
  Tier 2 ($7):  top international starters (~30)
  Tier 3 ($5):  solid starters / GKs of top teams (~60)
  Tier 4 ($3):  named squad players / starters of weaker teams (~100)
  Tier 5 ($2):  everyone else (default ~1000)

Hand-curated dictionaries below drive Tiers 1-4. Everyone unlisted falls
to Tier 5. Designed to be re-runnable after editing — output rewrites
seed_players.json cleanly.

Usage:
  ./venv/bin/python scripts/build_seed_players.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQUADS_PATH  = ROOT / "docs" / "data" / "wc2026_squads.json"
TEAMS_PATH   = ROOT / "docs" / "data" / "seed_teams.json"
OUT_PATH     = ROOT / "docs" / "data" / "seed_players.json"


# ---------------------------------------------------------------------------
# Tier dictionaries — hand-curated
# ---------------------------------------------------------------------------

# Tier 1 ($10) — players who would be on a "best XI in the world" shortlist
# going into WC 2026. Pre-tournament star power, not retrospective.
TIER_1 = {
    "Kylian Mbappé",
    "Lionel Messi",
    "Erling Haaland",
    "Jude Bellingham",
    "Vinícius Júnior",
    "Lamine Yamal",
    "Cristiano Ronaldo",
    "Mohamed Salah",
    "Heung-Min Son",
    "Phil Foden",
}

# Tier 2 ($7) — top international starters (key XI player on a Tier 1-2 team,
# or the marquee name on a Tier 3 team).
TIER_2 = {
    # England
    "Harry Kane", "Bukayo Saka", "Declan Rice", "Cole Palmer",
    # Spain
    "Pedri", "Rodri", "Nico Williams", "Rodrigo Hernández",
    # Argentina
    "Lautaro Martínez", "Julián Álvarez", "Enzo Fernández",
    "Emiliano Martínez", "Alexis Mac Allister",
    # Brazil
    "Rodrygo", "Raphinha", "Alisson", "Casemiro",
    # France
    "Antoine Griezmann", "Aurélien Tchouaméni", "Ousmane Dembélé",
    "Eduardo Camavinga", "William Saliba",
    # Portugal
    "Bernardo Silva", "Bruno Fernandes", "Rafael Leão", "Rúben Dias",
    # Netherlands
    "Cody Gakpo", "Frenkie de Jong", "Virgil van Dijk", "Memphis Depay",
    # Belgium
    "Kevin De Bruyne", "Romelu Lukaku", "Jérémy Doku", "Thibaut Courtois",
    # Germany
    "Jamal Musiala", "Florian Wirtz", "Joshua Kimmich", "Kai Havertz",
    # Croatia
    "Luka Modrić",
    # Norway
    "Martin Ødegaard",
    # Uruguay
    "Federico Valverde", "Darwin Núñez",
    # Morocco
    "Achraf Hakimi",
    # Senegal
    "Sadio Mané",
    # USA
    "Christian Pulisic",
    # Colombia
    "Luis Díaz", "James Rodríguez",
}

# Tier 3 ($5) — solid starters / GKs of top teams / star on a Tier 3+ team
TIER_3 = {
    # England (depth + GK)
    "Jordan Pickford", "John Stones", "Marc Guéhi", "Reece James",
    "Trent Alexander-Arnold", "Conor Gallagher", "Anthony Gordon",
    # Spain
    "Aymeric Laporte", "Unai Simón", "Dani Olmo", "Ferran Torres",
    "Mikel Oyarzabal", "Daniel Carvajal", "Dani Vivian",
    # Argentina
    "Cristian Romero", "Nahuel Molina", "Rodrigo De Paul",
    "Nicolás Otamendi", "Marcos Acuña", "Leandro Paredes",
    # Brazil
    "Marquinhos", "Lucas Paquetá", "Bruno Guimarães", "Vinícius Tobias",
    "Éderson", "Gabriel Magalhães", "Endrick",
    # France
    "Theo Hernández", "Mike Maignan", "Marcus Thuram", "Adrien Rabiot",
    "Randal Kolo Muani", "Jules Koundé", "N'Golo Kanté",
    # Portugal
    "João Cancelo", "Diogo Jota", "Vitinha", "Bernardo Silva",
    "Pepe", "Diogo Costa", "Gonçalo Ramos",
    # Netherlands
    "Denzel Dumfries", "Matthijs de Ligt", "Tijjani Reijnders",
    "Bart Verbruggen", "Joey Veerman",
    # Belgium
    "Youri Tielemans", "Axel Witsel", "Leandro Trossard",
    # Germany
    "Manuel Neuer", "Antonio Rüdiger", "Niclas Füllkrug", "İlkay Gündoğan",
    "Leroy Sané", "Serge Gnabry",
    # Croatia
    "Mateo Kovačić", "Joško Gvardiol", "Dominik Livaković", "Andrej Kramarić",
    # Norway
    "Alexander Sørloth", "Antonio Nusa", "Aron Dønnum",
    # Uruguay
    "Ronald Araújo", "José María Giménez", "Sergio Rochet", "Maximiliano Araújo",
    # Switzerland
    "Granit Xhaka", "Yann Sommer", "Manuel Akanji", "Breel Embolo",
    # Mexico
    "Edson Álvarez", "Raúl Jiménez", "Guillermo Ochoa", "Hirving Lozano",
    # USA
    "Tim Weah", "Tyler Adams", "Yunus Musah", "Folarin Balogun", "Weston McKennie",
    # Morocco
    "Yassine Bounou", "Youssef En-Nesyri", "Hakim Ziyech", "Sofyan Amrabat",
    # Senegal
    "Édouard Mendy", "Ismaïla Sarr", "Idrissa Gueye", "Kalidou Koulibaly",
    # Japan
    "Takefusa Kubo", "Wataru Endo", "Daichi Kamada", "Daizen Maeda", "Kaoru Mitoma",
    # South Korea
    "Hwang Hee-Chan", "Kim Min-Jae", "Lee Kang-In",
    # Ecuador
    "Moisés Caicedo", "Piero Hincapié", "Pervis Estupiñán", "Enner Valencia",
    # Sweden
    "Alexander Isak", "Viktor Gyökeres",
    # Turkey
    "Hakan Çalhanoğlu", "Arda Güler", "Ferdi Kadıoğlu", "Kenan Yıldız",
    # Egypt
    "Trezeguet", "Mostafa Mohamed",
}

# Tier 4 ($3) — named squad players, key starters of weaker teams, GKs of mid teams
TIER_4 = {
    # Top teams' depth (Tier 1 teams)
    # England
    "Phil Foden",  # duplicate intentionally — falls back to highest tier
    "Eberechi Eze", "Ollie Watkins", "Levi Colwill", "Cole Palmer",
    "Dean Henderson", "Curtis Jones",
    # Spain
    "Mikel Merino", "Pau Cubarsí", "Marc Cucurella", "Joselu", "Fabián Ruiz",
    # Argentina
    "Géronimo Rulli", "Juan Musso", "Nicolás Tagliafico", "Marcos Senesi",
    "Thiago Almada", "Nicolás González",
    # Brazil
    "Vinícius Tobias", "Beraldo", "André", "João Pedro",
    # France
    "Bradley Barcola", "Désiré Doué", "Manu Koné", "Lucas Hernández",
    # Portugal
    "João Félix", "Nuno Mendes", "João Neves", "José Sá",
    # Netherlands
    "Justin Kluivert", "Donyell Malen", "Ian Maatsen",
    # Germany
    "Pascal Groß", "Robert Andrich", "David Raum", "Aleksandar Pavlović",
    # Croatia
    "Borna Sosa", "Marcelo Brozović", "Ivan Perišić",
    # Belgium
    "Amadou Onana", "Charles De Ketelaere", "Loïs Openda",
    # Tier 3 teams - more starters
    # Mexico
    "Luis Romo", "César Montes", "Orbelín Pineda", "Jesús Gallardo",
    # USA
    "Matt Turner", "Sergiño Dest", "Antonee Robinson", "Brenden Aaronson",
    "Gio Reyna", "Ricardo Pepi",
    # Morocco
    "Romain Saïss", "Noussair Mazraoui", "Bilal El Khannouss",
    # Senegal
    "Nicolas Jackson", "Pape Matar Sarr", "Krepin Diatta",
    # Japan
    "Hidemasa Morita", "Junya Ito", "Ko Itakura", "Ayase Ueda", "Reo Hatate",
    # South Korea
    "Cho Gue-Sung", "Hwang In-Beom", "Kim Seung-Gyu",
    # Ecuador
    "Hernán Galíndez", "Félix Torres", "Jeremy Sarmiento",
    # Sweden
    "Robin Olsen", "Albin Ekdal", "Dejan Kulusevski", "Anthony Elanga",
    # Turkey
    "Mert Günok", "Salih Özcan", "Çağlar Söyüncü",
    # Egypt
    "Mohamed Elneny", "Mohamed Hany", "Mostafa Shalaby", "Ahmed Hegazy",
    # Norway
    "Sander Berge", "Patrick Berg", "Ørjan Nyland",
    # Uruguay
    "Mathías Olivera", "Manuel Ugarte", "Maximiliano Araújo",
    # Tier 4 teams — star players
    # Canada
    "Alphonso Davies", "Jonathan David", "Cyle Larin", "Stephen Eustáquio",
    "Maxime Crépeau",
    # Australia
    "Mat Ryan", "Harry Souttar", "Aaron Mooy", "Mitchell Duke", "Jackson Irvine",
    # Iran
    "Mehdi Taremi", "Sardar Azmoun", "Alireza Beiranvand",
    # Ghana
    "Mohammed Kudus", "Thomas Partey", "Inaki Williams", "Jordan Ayew",
    # Ivory Coast
    "Sébastien Haller", "Franck Kessié", "Wilfried Singo", "Simon Adingra",
    # Algeria
    "Riyad Mahrez", "Ismaël Bennacer", "Houssem Aouar",
    # Czechia
    "Patrik Schick", "Tomáš Souček", "Vladimir Coufal", "Jiří Pavlenka",
    # Austria
    "Marcel Sabitzer", "David Alaba", "Konrad Laimer", "Marko Arnautović",
    "Christoph Baumgartner",
    # Paraguay
    "Miguel Almirón", "Antonio Sanabria",
    # Scotland
    "Andy Robertson", "Scott McTominay", "Kieran Tierney", "Angus Gunn",
    "John McGinn", "Lyndon Dykes",
    # Saudi Arabia
    "Salem Al-Dawsari",
    # Tunisia
    "Wahbi Khazri", "Hannibal Mejbri",
    # New Zealand
    "Chris Wood",
    # Cape Verde
    "Logan Costa",
    # Qatar
    "Akram Afif",
}


# ---------------------------------------------------------------------------
# Normalization for name matching
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    """Strip accents, lowercase, collapse whitespace — for lookup.

    NFKD only handles accents over base letters. Characters that ARE
    distinct letters (Ø, İ, ı, ł, ß, etc.) need manual mapping or
    they drop entirely under ascii encode."""
    REPLACEMENTS = {
        "Ø": "O", "ø": "o",
        "Å": "A", "å": "a",
        "Æ": "Ae", "æ": "ae",
        "Œ": "Oe", "œ": "oe",
        "Ł": "L", "ł": "l",
        "Đ": "D", "đ": "d",
        "İ": "I", "ı": "i",
        "ß": "ss",
        "Þ": "Th", "þ": "th",
        "Ð": "D", "ð": "d",
    }
    for k, v in REPLACEMENTS.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9 -]", " ", s.lower())  # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_lookup(name_set: set[str]) -> set[str]:
    return {_norm_name(n) for n in name_set}


def _resolve_tier_for_squad(squad_names: list[str]) -> dict[str, int]:
    """Match each squad-list name to a tier (1-4) if possible, else 5.

    Two-pass matching against the override sets:
      1. Exact normalized match
      2. Substring match (override name appears inside the squad name —
         catches 'Alisson' → 'Alisson Becker' but won't cross-match
         unrelated players like 'Salah' → 'Salah-Eddine' because the
         full override 'mohamed salah' isn't a substring of the other.)
    """
    name_to_norm = {n: _norm_name(n) for n in squad_names}

    by_tier = [(1, TIER_1), (2, TIER_2), (3, TIER_3), (4, TIER_4)]
    assigned: dict[str, int] = {}

    # Pass 1: exact match
    for tier, name_set in by_tier:
        norm_set = {_norm_name(n) for n in name_set}
        for sn, snorm in name_to_norm.items():
            if sn in assigned:
                continue
            if snorm in norm_set:
                assigned[sn] = tier

    # Pass 2: substring match for any not-yet-assigned
    for tier, name_set in by_tier:
        norm_overrides = [_norm_name(n) for n in name_set]
        for sn, snorm in name_to_norm.items():
            if sn in assigned:
                continue
            for ov in norm_overrides:
                if ov in snorm or snorm in ov:
                    assigned[sn] = tier
                    break

    # Default everyone else to Tier 5
    for sn in squad_names:
        assigned.setdefault(sn, 5)

    return assigned


TIER_PRICE = {1: 10, 2: 7, 3: 5, 4: 3, 5: 2}  # legacy buckets — kept only for tier-meets-floor logic

# Continuous pricing — every player gets a unique-ish price in [$1, $25].
# Top ~40 marquee names get hand-curated explicit prices; everyone else
# goes through a formula that combines (team strength rank, within-team
# rank, position).
PLAYER_PRICE_OVERRIDES = {
    "Kylian Mbappé":      25,
    "Lionel Messi":       22,
    "Erling Haaland":     22,
    "Jude Bellingham":    20,
    "Vinícius Júnior":    20,
    "Vinicius Junior":    20,
    "Lamine Yamal":       18,
    "Phil Foden":         18,
    "Heung-Min Son":      17,
    "Heung-min Son":      17,
    "Cristiano Ronaldo":  17,
    "Harry Kane":         15,
    "Bukayo Saka":        14,
    "Luka Modrić":        14,
    "Kevin De Bruyne":    13,
    "Pedri":              13,
    "Julián Álvarez":     12,
    "Enzo Fernández":     11,
    "Romelu Lukaku":      11,
    "Emiliano Martínez":  11,
    "Bruno Fernandes":    10,
    "Bernardo Silva":     10,
    "Rodri":              10,
    "Achraf Hakimi":      10,
    "Manuel Neuer":       10,
    "Alisson Becker":     10,
    "Casemiro":            9,
    "Alexis Mac Allister": 9,
    "Marquinhos":          9,
    "Virgil van Dijk":     9,
    "Antonio Rüdiger":     9,
    "Joshua Kimmich":      9,
    "Luis Díaz":           9,
    "Thibaut Courtois":    8,
    "Federico Valverde":   8,
    "Darwin Núñez":        8,
    "Jordan Pickford":     7,
    "Dani Olmo":           7,
    "Vitinha":             7,
    "Mateo Kovačić":       7,
    "Dani Carvajal":       7,
    "Yassine Bounou":      7,
    "Édouard Mendy":       7,
    "Edouard Mendy":       7,
    "Diogo Costa":         7,
}


# Position multiplier — forwards have goal-scoring upside, GKs have CS
# upside but capped, defenders are most-discounted.
_POS_MULT = {"FWD": 1.15, "MID": 1.00, "GK": 0.95, "DEF": 0.85, "?": 0.95}


def formula_price(team_rank: int, total_teams: int,
                   within_rank: int, squad_size: int,
                   position: str) -> int:
    """Continuous pricing formula. Returns an integer $1-$22 (top names
    get higher via the explicit override list)."""
    import math
    team_factor = max(0.04, (1 - (team_rank - 1) / max(1, total_teams - 1)) ** 1.3)
    within_factor = max(0.05, (1 - (within_rank - 1) / max(1, squad_size)) ** 1.5)
    base = 18.0 * team_factor * within_factor * _POS_MULT.get(position, 1.0)
    return max(1, round(base))


def _missing_overrides(assigned_names: set[str]) -> dict[int, list[str]]:
    """Return overrides that didn't match any squad player."""
    out: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    assigned_norms = {_norm_name(n) for n in assigned_names}
    for tier, name_set in [(1, TIER_1), (2, TIER_2), (3, TIER_3), (4, TIER_4)]:
        for n in name_set:
            nnorm = _norm_name(n)
            # An override is "matched" if any assigned squad name matches
            # exactly OR contains the override OR is contained in it.
            matched = any(
                an == nnorm or nnorm in an or an in nnorm
                for an in assigned_norms
            )
            if not matched:
                out[tier].append(n)
    return out


# ---------------------------------------------------------------------------
# Position → simplified position
# ---------------------------------------------------------------------------

def simplify_position(pos: str | None) -> str:
    if not pos:
        return "?"
    p = pos.lower()
    if "goalkeep" in p: return "GK"
    if "back" in p or "defence" in p: return "DEF"
    if "midfield" in p: return "MID"
    if "winger" in p or "forward" in p or "offence" in p: return "FWD"
    return "?"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

PER_TEAM_FLOOR = 3   # every team gets at least this many tiered players (per user, 2026-05-24)
DRAFTABLE_TIERS = {1, 2, 3, 4}  # output only includes players in these tiers


def _promote_to_meet_floor(team_squad: list[dict], tier_assignments: dict[str, int],
                            current_count: int, target: int) -> None:
    """Mutates tier_assignments to promote additional squad members to Tier 4
    until we hit the target. Selection priority:
      1. Ensure at least one GK is in the pool (promotes a GK first if missing)
      2. Promote anyone matching the hand-curated T4 list (already done, but
         confirm)
      3. Otherwise promote squad members in the order the API returns them
         (usually rough seniority/squad-number order)
    """
    needed = max(0, target - current_count)
    if needed == 0:
        return

    in_pool = [p for p in team_squad if tier_assignments.get(p["name"], 5) < 5]
    out_pool = [p for p in team_squad if tier_assignments.get(p["name"], 5) == 5]

    has_gk = any((p.get("position") or "").lower().startswith("goalkeep") for p in in_pool)
    if not has_gk:
        gk = next((p for p in out_pool if (p.get("position") or "").lower().startswith("goalkeep")), None)
        if gk:
            tier_assignments[gk["name"]] = 4
            out_pool.remove(gk)
            needed -= 1

    # Promote remaining slots from the front of the squad list.
    for p in out_pool:
        if needed <= 0:
            break
        tier_assignments[p["name"]] = 4
        needed -= 1


def build() -> None:
    if not SQUADS_PATH.exists():
        sys.exit(f"missing {SQUADS_PATH} — run scripts/pull_wc2026_squads.py first")
    squads_data = json.loads(SQUADS_PATH.read_text())
    teams_data = json.loads(TEAMS_PATH.read_text())

    name_to_slug: dict[str, str] = {t["name"]: t["id"] for t in teams_data}

    # Build a team strength index from seed_teams.json — sorted by basePrice
    # descending. Used by the formula to get team_rank.
    teams_by_strength = sorted(teams_data, key=lambda t: -t["basePrice"])
    team_rank_by_slug = {t["id"]: i + 1 for i, t in enumerate(teams_by_strength)}
    total_teams = len(teams_by_strength)

    out = []
    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    per_team_tiered = {}
    assigned_names = set()

    for team_name, info in squads_data.items():
        team_slug = name_to_slug.get(team_name) or _norm_name(team_name).replace(" ", "-")
        team_rank = team_rank_by_slug.get(team_slug, total_teams)
        squad = info.get("squad", []) or []
        squad_names = [p["name"] for p in squad]
        tier_assignments = _resolve_tier_for_squad(squad_names)

        tiered_count = sum(1 for n in squad_names if tier_assignments.get(n, 5) < 5)
        if tiered_count < PER_TEAM_FLOOR:
            _promote_to_meet_floor(squad, tier_assignments, tiered_count, PER_TEAM_FLOOR)

        # Within-team rank: sort by tier (1 best) then by name for determinism.
        squad_sorted = sorted(
            squad,
            key=lambda p: (tier_assignments.get(p["name"], 5), p["name"])
        )
        within_rank_map = {p["name"]: i + 1 for i, p in enumerate(squad_sorted)}
        squad_size = len(squad_sorted)

        for p in squad:
            tier = tier_assignments.get(p["name"], 5)
            if tier not in DRAFTABLE_TIERS:
                counts[5] += 1
                continue
            if tier < 5:
                assigned_names.add(p["name"])
            counts[tier] += 1
            doc_id = f"{p['id']}-{team_slug}"
            position = simplify_position(p.get("position"))

            # Resolve explicit override or formula price
            override = PLAYER_PRICE_OVERRIDES.get(p["name"])
            if override is None:
                # Try a normalized-name override match too
                pn = _norm_name(p["name"])
                for k, v in PLAYER_PRICE_OVERRIDES.items():
                    if _norm_name(k) == pn:
                        override = v
                        break
            if override is not None:
                base_price = override
            else:
                within = within_rank_map[p["name"]]
                base_price = formula_price(team_rank, total_teams, within, squad_size, position)

            out.append({
                "id": doc_id,
                "fdId": p["id"],
                "name": p["name"],
                "teamId": team_slug,
                "teamName": team_name,
                "position": position,
                "tier": tier,
                "basePrice": base_price,
            })

        per_team_tiered[team_name] = sum(1 for p in squad if tier_assignments.get(p["name"], 5) < 5)

    out.sort(key=lambda r: (r["teamName"], r["tier"], r["name"]))
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(f"Wrote {len(out)} DRAFTABLE players → {OUT_PATH}")
    print(f"(non-draftable squad members dropped: {counts[5]})")
    print(f"Per-tier counts (draftable): T1={counts[1]}  T2={counts[2]}  T3={counts[3]}  T4={counts[4]}")
    # Price distribution
    from collections import Counter
    price_dist = Counter(p["basePrice"] for p in out)
    print(f"\nPrice distribution ({len(price_dist)} distinct values):")
    for price in sorted(price_dist.keys(), reverse=True):
        bar = "▌" * min(50, price_dist[price])
        print(f"  ${price:>2}: {price_dist[price]:>3}  {bar}")

    print(f"\nPer-team draftable counts (floor = {PER_TEAM_FLOOR}):")
    under = [(t, n) for t, n in per_team_tiered.items() if n < PER_TEAM_FLOOR]
    if under:
        print("  ! still under floor:")
        for t, n in sorted(under, key=lambda x: x[1]):
            print(f"    {t}: {n}")
    else:
        print(f"  all 48 teams meet the floor")

    print("\nTier 1 players in output:")
    for p in out:
        if p["tier"] == 1:
            print(f"  ${p['basePrice']}  {p['name']:<28} ({p['teamName']})")

    missing = _missing_overrides(assigned_names)
    for tier in (1, 2):  # only flag the high-tier ones — T3/T4 misses are common
        if missing[tier]:
            print(f"\nTier {tier} names NOT FOUND in any squad ({len(missing[tier])}):")
            for n in missing[tier]:
                print(f"  • {n}")


if __name__ == "__main__":
    build()
