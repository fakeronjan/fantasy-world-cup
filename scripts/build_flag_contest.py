#!/usr/bin/env python3
"""Build the Flag Knockout contest seed + download flag assets.

The Flag Knockout is a fun side module: all 48 WC-2026 nations compete in a
single-elimination bracket decided by which FLAG logged-in managers like better.
This script is deterministic (fixed random seed) so re-running reproduces the
exact same draw. It:

  1. Resolves every WC team -> ISO-2 code (via countries.json, with a small
     override table for football-name mismatches).
  2. Draws 16 byes + 32 wildcard-round teams (16 games) with a fixed seed.
  3. Builds the full bracket tree: wildcard -> R32 -> R16 -> QF -> SF -> Final,
     where each R32 game is (bye) vs (winner of a wildcard game). Games in later
     rounds reference their feeder games by index; winners are filled in live by
     the admin "close round" action.
  4. Writes docs/data/flag_contest_seed.json (uploaded to Firestore by the admin
     "Initialize" button).
  5. Downloads the 48 flag SVGs into docs/flags/{iso}.svg (skips ones present).

Run: python scripts/build_flag_contest.py           (build seed + fetch flags)
     python scripts/build_flag_contest.py --no-fetch (seed only)

The draw is football-neutral: byes are random, NOT seeded by team strength, so
no nation gets a flag advantage for being good at soccer. Change DRAW_SEED to
re-roll the bracket.
"""
import json, os, random, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOCS = os.path.join(REPO, 'docs')
DRAW_SEED = 20260611  # WC 2026 kickoff date; change to re-roll the bracket

# Football-data team names that don't match countries.json -> ISO-2.
ISO_OVERRIDE = {
    'Bosnia-Herzegovina': 'BA',
    'Cape Verde Islands': 'CV',
    'Congo DR': 'CD',
    'Ivory Coast': 'CI',
    # Home nations: countries.json gives non-standard ISOs (ENG/SCO); flagcdn
    # serves the UK sub-flags as gb-eng / gb-sct / gb-wls.
    'England': 'GB-ENG',
    'Scotland': 'GB-SCT',
    'Wales': 'GB-WLS',
}


def resolve_teams():
    teams = json.load(open(f'{DOCS}/data/wc2026_teams.json'))
    countries = json.load(open(f'{DOCS}/data/countries.json'))
    by_name = {c['name']: c for c in countries}
    out = []
    for t in teams:
        name = t['name']
        iso = ISO_OVERRIDE.get(name) or (by_name.get(name) or {}).get('iso')
        if not iso:
            raise SystemExit(f'No ISO for {name!r} - add to ISO_OVERRIDE')
        emoji = (by_name.get(name) or {}).get('emoji', '')
        out.append({'name': name, 'iso': iso.lower(), 'emoji': emoji})
    if len(out) != 48:
        raise SystemExit(f'Expected 48 teams, got {len(out)}')
    return out


def build_bracket(teams):
    """Return the full bracket structure. Seeds 1..48 are assigned by the draw
    order (arbitrary/neutral); byes get the low seed numbers so tie-breaks
    (higher seed = lower number advances) are deterministic."""
    rng = random.Random(DRAW_SEED)
    order = teams[:]
    rng.shuffle(order)
    for i, t in enumerate(order):
        t['seed'] = i + 1  # 1..48, draw order

    byes = order[:16]           # 16 teams skip the wildcard round
    wc_pool = order[16:]        # 32 teams play the wildcard round
    rng.shuffle(wc_pool)

    def game(gid, a, b):
        # a/b are team dicts or None (TBD, filled when a feeder round closes).
        return {'id': gid, 'a': a, 'b': b, 'winner': None,
                'votesA': None, 'votesB': None, 'closed': False}

    rounds = {}
    # wildcard: 16 games, 32 teams
    wc_games = [game(f'wc{i}', wc_pool[2 * i], wc_pool[2 * i + 1]) for i in range(16)]
    rounds['wildcard'] = {'games': wc_games}
    # R32: 16 games, each = bye[i] vs winner(wc[i]); feeder recorded for 'b'.
    r32 = []
    for i in range(16):
        g = game(f'r32_{i}', byes[i], None)
        g['bFeeder'] = f'wc{i}'   # slot b filled from wildcard winner on close
        r32.append(g)
    rounds['R32'] = {'games': r32}
    # R16..F: each game fed by two games of the prior round.
    prior = 'R32'
    for rnd, n in [('R16', 8), ('QF', 4), ('SF', 2), ('F', 1)]:
        games = []
        for i in range(n):
            g = game(f'{rnd.lower()}_{i}', None, None)
            g['aFeeder'] = f'{prior.lower()}_{2 * i}' if prior != 'R32' else f'r32_{2 * i}'
            g['bFeeder'] = f'{prior.lower()}_{2 * i + 1}' if prior != 'R32' else f'r32_{2 * i + 1}'
            games.append(g)
        rounds[rnd] = {'games': games}
        prior = rnd

    return {
        'status': 'setup',
        'currentRound': 'wildcard',
        'votingOpen': False,
        'roundOrder': ['wildcard', 'R32', 'R16', 'QF', 'SF', 'F'],
        'rounds': rounds,
        'byes': byes,
        'champion': None,
        'drawSeed': DRAW_SEED,
    }


def fetch_flags(teams):
    outdir = os.path.join(DOCS, 'flags')
    os.makedirs(outdir, exist_ok=True)
    got = skip = fail = 0
    for t in teams:
        iso = t['iso']
        dest = os.path.join(outdir, f'{iso}.svg')
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            skip += 1
            continue
        url = f'https://flagcdn.com/{iso}.svg'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'fwc-flag-contest/1.0'})
            data = urllib.request.urlopen(req, timeout=20).read()
            if not data or b'<svg' not in data[:200].lower():
                raise ValueError('not an SVG')
            open(dest, 'wb').write(data)
            got += 1
        except Exception as e:
            fail += 1
            print(f'  FAIL {iso} ({t["name"]}): {e}')
    print(f'flags: {got} downloaded, {skip} already present, {fail} failed '
          f'({len(teams)} teams)')
    return fail == 0


def main():
    teams = resolve_teams()
    seed = build_bracket(teams)
    dest = os.path.join(DOCS, 'data', 'flag_contest_seed.json')
    json.dump(seed, open(dest, 'w'), ensure_ascii=False, indent=2)
    print(f'wrote {dest}')
    print(f'  wildcard games: {len(seed["rounds"]["wildcard"]["games"])}, '
          f'byes: {len(seed["byes"])}')
    if '--no-fetch' not in sys.argv:
        ok = fetch_flags(teams)
        if not ok:
            sys.exit('some flags failed to download - see above')


if __name__ == '__main__':
    main()
