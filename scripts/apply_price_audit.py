"""One-off: apply the 2026-05-25 price audit + add the marquee players
that the original squad-pull missed. Re-runnable (idempotent).

Run from project root:
  ./venv/bin/python scripts/apply_price_audit.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / 'docs' / 'data' / 'seed_players.json'
HIST = ROOT / 'docs' / 'data' / 'player_history.json'
AWAR = ROOT / 'docs' / 'data' / 'player_awards.json'

# (player_id, new_price, new_tier_or_None)
PRICE_BUMPS = [
    # A) Clear underpricings
    ('3199-spain',      7, 1),   # Rodri  - 2024 Ballon d'Or winner
    ('202283-spain',    7, 1),   # Yamal  - peer to Vini/Bellingham
    ('8004-england',    6, 1),   # Kane   - WC 2018 Golden Boot
    ('3373-france',     6, 1),   # Dembélé - current PSG/UCL form
    ('98571-argentina', 6, 1),   # J. Álvarez - 4 WC 2022 goals
    # B) Defensible bumps
    ('19334-germany',   6, 1),   # Wirtz   - gen-talent
    ('144393-germany',  6, 1),   # Musiala - gen-talent
    ('3257-portugal',   6, 1),   # Bruno Fernandes
    ('7869-netherlands', 5, 2),  # Van Dijk - Dutch captain
    ('3641-belgium',    5, 2),   # Courtois - WC 2018 Golden Glove
    ('359-germany',     5, 2),   # Kimmich  - Germany captain class
]

# Missing players from original seed (squad-pull bug). Adding by hand.
NEW_PLAYERS = [
    {
        'id': '3220-argentina', 'fdId': 3220,
        'name': 'Lautaro Martínez', 'teamId': 'argentina', 'teamName': 'Argentina',
        'position': 'FWD', 'tier': 1, 'basePrice': 6, 'club': 'Inter',
    },
    {
        'id': '3754-egypt', 'fdId': 3754,
        'name': 'Mohamed Salah', 'teamId': 'egypt', 'teamName': 'Egypt',
        'position': 'FWD', 'tier': 2, 'basePrice': 5, 'club': 'Liverpool',
    },
    {
        'id': '157135-spain', 'fdId': 157135,
        'name': 'Nico Williams', 'teamId': 'spain', 'teamName': 'Spain',
        'position': 'FWD', 'tier': 2, 'basePrice': 5, 'club': 'Athletic',
    },
    {
        'id': '32629-ghana', 'fdId': 32629,
        'name': 'Iñaki Williams', 'teamId': 'ghana', 'teamName': 'Ghana',
        'position': 'FWD', 'tier': 2, 'basePrice': 4, 'club': 'Athletic',
    },
    {
        'id': '132707-japan', 'fdId': 132707,
        'name': 'Kaoru Mitoma', 'teamId': 'japan', 'teamName': 'Japan',
        'position': 'FWD', 'tier': 2, 'basePrice': 5, 'club': 'Brighton Hove',
    },
]

# Player_history.json stubs for new players (manual curation).
NEW_HISTORY = {
    '3220-argentina': {
        'wcsPlayed': 2, 'wcYears': [2018, 2022],
        'goals': 1, 'titles': 1, 'titleYears': [2022],
        'runnerUps': 0, 'runnerUpYears': [],
        '_source': 'manual',
    },
    '3754-egypt': {
        'wcsPlayed': 1, 'wcYears': [2018],
        'goals': 2, 'titles': 0, 'titleYears': [],
        'runnerUps': 0, 'runnerUpYears': [],
        '_source': 'manual',
    },
    '157135-spain': {
        'wcsPlayed': 0, 'wcYears': [], 'goals': 0,
        'titles': 0, 'titleYears': [], 'runnerUps': 0, 'runnerUpYears': [],
        'debutant': True, '_source': 'manual',
    },
    '32629-ghana': {
        'wcsPlayed': 1, 'wcYears': [2022], 'goals': 0,
        'titles': 0, 'titleYears': [], 'runnerUps': 0, 'runnerUpYears': [],
        '_source': 'manual',
    },
    '132707-japan': {
        'wcsPlayed': 1, 'wcYears': [2022], 'goals': 0,
        'titles': 0, 'titleYears': [], 'runnerUps': 0, 'runnerUpYears': [],
        '_source': 'manual',
    },
}

# Awards for new players that fit "WC + Ballon d'Or-style" scope.
NEW_AWARDS = {
    '3220-argentina': {
        'honors': ['Copa América Golden Boot 2024'],
    },
    '3754-egypt': {
        'honors': ['2× African Footballer of the Year (2017, 2018)'],
    },
}

def main():
    # 1. seed_players.json
    seed = json.loads(SEED.read_text())
    seed_by_id = {p['id']: p for p in seed}

    # Apply price bumps
    bumped = 0
    for pid, new_price, new_tier in PRICE_BUMPS:
        if pid not in seed_by_id:
            print(f'  WARN: {pid} not in seed, skipping bump')
            continue
        p = seed_by_id[pid]
        old_price, old_tier = p['basePrice'], p['tier']
        p['basePrice'] = new_price
        if new_tier:
            p['tier'] = new_tier
        print(f'  ${old_price}→${new_price}  T{old_tier}→T{new_tier or old_tier}  {p["name"]:<26} ({p["teamName"]})')
        bumped += 1

    # Add new players (idempotent - skip if already present)
    added = 0
    for new_p in NEW_PLAYERS:
        if new_p['id'] in seed_by_id:
            print(f'  SKIP: {new_p["name"]} already in seed')
            continue
        seed.append(new_p)
        added += 1
        print(f'  +ADD ${new_p["basePrice"]} T{new_p["tier"]} {new_p["position"]} {new_p["name"]:<26} ({new_p["teamName"]}) - {new_p["club"]}')

    # Resort to preserve typical order
    seed.sort(key=lambda p: (p['teamId'], -p['basePrice'], p['name']))
    SEED.write_text(json.dumps(seed, indent=2, ensure_ascii=False) + '\n')

    # 2. player_history.json - add stubs for new players
    hist = json.loads(HIST.read_text())
    for pid, h in NEW_HISTORY.items():
        hist[pid] = h
    HIST.write_text(json.dumps(hist, indent=2, ensure_ascii=False))

    # 3. player_awards.json - add honors for new players that have them
    awards = json.loads(AWAR.read_text())
    for pid, a in NEW_AWARDS.items():
        awards[pid] = a
    AWAR.write_text(json.dumps(awards, indent=2, ensure_ascii=False))

    print(f'\nApplied {bumped} price bumps, added {added} players, '
          f'updated {len(NEW_HISTORY)} history stubs, {len(NEW_AWARDS)} award entries.')
    print(f'Total players in seed: {len(seed)}')

if __name__ == '__main__':
    main()
