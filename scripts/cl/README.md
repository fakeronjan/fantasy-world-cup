# Champions League spin-off — harness (Phase 0)

A deterministic, offline replay test bed for the CL fantasy spin-off, built by
reusing the Fantasy World Cup codebase. See memory `project-cl-spinoff` for scope.

## Why
The 2025-26 CL season is *finished*, so it's a perfect fixture to build and
validate the scoring + Monte Carlo pricing engine against known outcomes before
pointing anything at the live 2026-27 season.

## Scripts
- `fetch_cl_season.py --season 2025` — pulls the full season from
  football-data.org into `data/cl/` (gitignored, regenerable):
  - `cl2025_matches.json` — match-list summary (189 matches)
  - `cl2025_details.json` — `{matchId: detail}` with goals (scorer+assist+type),
    11-man lineups, bookings, penalties. Throttled + resumable (checkpoints
    every 10; re-runs skip cached matches).
  - `cl2025_teams.json` — 36 clubs + squads.
  - Needs `FOOTBALL_DATA_KEY` (env or the Power Rankings `.env`, via `_fwc_lib`).
    No Firestore — pure local JSON.
- `validate_cl_harness.py --season 2025` — read-only sanity check. Rebuilds the
  league table (cross-checked vs football-data `leagueRank`), traces the
  two-legged KO bracket to the champion, lists top scorers/assisters, and
  confirms lineup completeness.

## Format confirmed from real data (modern CL, 2024-25+)
`LEAGUE_STAGE` 144 (36 teams × 8) → `PLAYOFFS` 16 (8 two-legged ties, seeds
9-24) → `LAST_16` 16 → `QUARTER_FINALS` 8 → `SEMI_FINALS` 4 → `FINAL` 1.
Top 8 of the league phase skip the playoff straight to LAST_16; 25-36 are out.

## Phase 0 result (2026-06-17)
189/189 matches + details cached, 36 squads (size 26-43). League table rebuilt
with **0** rank mismatches vs football-data. Bracket traced to champion
**Paris Saint-Germain**. Top scorer Mbappé (15). 189/189 matches have both
lineups → win-share + clean-sheet scoring is fully viable. Harness is complete.
