# Fantasy World Cup 2026

A salary-cap fantasy game for the 2026 FIFA World Cup. Friends sign in with
Google, draft a roster of teams + players within a $100 budget (12-pick cap),
and score points as their picks win and score goals.

Live at: `fakeronjan.github.io/fantasy-world-cup` (after deploy).

## Status

Built during 2026-05-24 → 2026-06-11. See
[the build plan](../../.claude/plans/structured-sparking-barto.md) and
[simulation findings](SIMULATION_FINDINGS.md) for background.

## Layout

```
docs/                  → GitHub Pages serves from here
  index.html           landing + sign-in
  draft.html           pre-tournament roster draft
  roster.html          my roster + points
  leaderboard.html     ranked players
  transfer.html        between-round trades
  admin.html           owner-only result entry / overrides
  shared.css           teal + pink palette (matches Power Rankings sites)
  shared.js            Firebase init, auth helpers
  data/                seed JSON for teams / players

scripts/
  fetch_wc_data.py     historical WC data pull (already run; informed scoring weights)
  simulate.py          scoring balance simulation (already run; see SIMULATION_FINDINGS.md)
  seed_assets.py       one-off: writes 48 teams + ~180 players to Firestore
  ingest_results.py    GitHub Actions target: poll sports API, update Firestore
  reprice.py           run between rounds: update prices, zero out eliminated

.github/workflows/
  ingest.yml           cron job, every 15 min during match windows

firestore.rules        security rules
```

## Setting up locally

```bash
# Python env (used by data scripts)
python3 -m venv venv
./venv/bin/pip install -r scripts/requirements.txt

# View the site locally
cd docs && python3 -m http.server 8000
# Open http://localhost:8000
```

## One-time Firebase setup

1. Create a Firebase project at https://console.firebase.google.com.
2. Add a Web app to the project and copy the config object.
3. Paste it into `docs/shared.js` where it says `REPLACE_ME`.
4. Enable Google as a sign-in provider: Authentication → Sign-in method.
5. Create a Firestore database in production mode.
6. Apply `firestore.rules` (Firestore → Rules → paste → Publish).
7. Sign in once with your admin Google account, then add your uid to
   `ADMIN_UIDS` in `docs/shared.js` AND to `firestore.rules`.

## Scoring + pricing

Captured in `SIMULATION_FINDINGS.md` after running balance analysis on
WC 2018 + 2022 data adapted to the 48-team 2026 format. Short version:
3 pts per win, 1 per draw, advancement bonuses 1/2/4/6/10/15 (R32/R16/QF/SF/F/W),
5 pts per goal. Roster cap 12, budget $100, $2 player price floor.

## Reproducing the historical analysis

```bash
./venv/bin/python scripts/fetch_wc_data.py   # one-off Wikipedia data pull
./venv/bin/python scripts/simulate.py        # weight-balance simulation
```
