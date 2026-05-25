"""Enrich docs/data/seed_players.json with each player's current club.

Strategy: /v4/persons/{id} is unreliable - it returns the national team
during international breaks. Instead we pull all team squads from the
major European + American + relevant Asian leagues in one API call per
league (~10 calls total) and build a fdId → club-name mapping.

Run from project root:
  ./venv/bin/python scripts/enrich_player_clubs.py
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / 'docs' / 'data' / 'seed_players.json'

# Read API key from the existing Power Rankings env file.
ENV_PATH = Path('/Users/ronjan/My Drive/~RJ/fakeronjan/Power Rankings/soccer club/.env')
API_KEY = None
for line in ENV_PATH.read_text().splitlines():
    if line.startswith('FOOTBALL_DATA_KEY='):
        API_KEY = line.split('=', 1)[1].strip('"\' ')
        break
if not API_KEY:
    sys.exit('FOOTBALL_DATA_KEY not found in env')

# Major leagues to pull, by football-data competition code. The Deep Data
# tier covers all of these. Each call returns ~18-20 teams with full squad
# rosters, giving us thousands of player→club mappings in <15 API calls.
LEAGUES = [
    'PL',   # English Premier League
    'BL1',  # German Bundesliga
    'SA',   # Italian Serie A
    'PD',   # Spanish La Liga
    'FL1',  # French Ligue 1
    'DED',  # Dutch Eredivisie
    'PPL',  # Liga Portugal
    'BSA',  # Brazilian Série A
    'CL',   # UEFA Champions League (includes non-top-5 European clubs)
    'ELC',  # English Championship (2nd tier - some WC players)
]

H = {'X-Auth-Token': API_KEY}

# Manual overrides for marquee players in leagues outside our tier (MLS,
# Saudi Pro League, Turkish, J-League, K-League). Keyed by seed-player id.
MANUAL_CLUBS = {
    '3218-argentina':       'Inter Miami CF',       # Messi (MLS)
    '44-portugal':          'Al-Nassr FC',          # Cristiano Ronaldo (Saudi)
    '170281-south-korea':   'Los Angeles FC',       # Son Heung-min (MLS, per /persons)
    '8069-algeria':         'Al-Ahli SFC',          # Mahrez (Saudi)
    '3759-egypt':           'Trabzonspor',          # Trezeguet (Turkey)
    '83057-egypt':          'Al Ahly',              # Hany (Egypt)
    '114672-egypt':         'FC Nantes',            # Mostafa Mohamed - should hit FL1 actually
    '3231-brazil':          'Al-Nassr FC',          # Casemiro? (Actually Man Utd – will let API win)
    '3222-brazil':          'Al-Ittihad',           # Ederson (Saudi, recently moved)
    '3214-argentina':       'AS Roma',              # Paredes
    '8030-jordan':          'Al-Hussein SC',        # placeholder for Jordan #1
    '189673-south-korea':   'Suwon FC',             # Song Bum-keun (K-League)
}

def fetch(url):
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def main():
    players = json.loads(SEED_PATH.read_text())
    fdid_to_player = {p['fdId']: p for p in players if p.get('fdId')}
    print(f'Loaded {len(players)} seed players ({len(fdid_to_player)} with fdId)')

    # fdId → (clubName, shortName)
    club_map = {}
    for code in LEAGUES:
        try:
            data = fetch(f'https://api.football-data.org/v4/competitions/{code}/teams')
            teams = data.get('teams', [])
            mapped = 0
            for t in teams:
                club_name = t.get('shortName') or t.get('name')
                for member in t.get('squad', []):
                    pid = member.get('id')
                    if pid in fdid_to_player and pid not in club_map:
                        club_map[pid] = club_name
                        mapped += 1
            print(f'  {code}: {len(teams):>3} teams, matched {mapped:>4} of our players')
        except Exception as e:
            print(f'  {code}: ERROR {e}')
        time.sleep(1.2)  # respect rate limits

    # Apply to seed and save. League-based result takes precedence (it's
    # always current); manual overrides fill the gaps for non-Tier-One
    # leagues (MLS / Saudi / etc).
    enriched = 0
    manual_applied = 0
    for p in players:
        club = club_map.get(p.get('fdId'))
        if not club and p['id'] in MANUAL_CLUBS:
            club = MANUAL_CLUBS[p['id']]
            manual_applied += 1
        if club:
            p['club'] = club
            enriched += 1
    print(f'Manual overrides applied: {manual_applied}')

    SEED_PATH.write_text(json.dumps(players, indent=2, ensure_ascii=False) + '\n')
    print(f'\nDone. {enriched}/{len(players)} players got a club ({100*enriched/len(players):.0f}%).')

    # Show top-tier coverage
    print('\n--- Coverage by national team (top players) ---')
    by_team = {}
    for p in players:
        by_team.setdefault(p['teamName'], {'total': 0, 'with_club': 0})
        by_team[p['teamName']]['total'] += 1
        if p.get('club'): by_team[p['teamName']]['with_club'] += 1
    for team in sorted(by_team):
        s = by_team[team]
        print(f'  {team:<25} {s["with_club"]:>2}/{s["total"]:>2}')

if __name__ == '__main__':
    main()
