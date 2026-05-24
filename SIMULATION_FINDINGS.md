# Fantasy World Cup 2026 — Simulation Findings (2026-05-24)

Historical-balance analysis using WC 2018 + WC 2022 data, adapted to the
2026 48-team format. Goal: figure out whether a "$100 budget, mix teams
and players" game is balanced enough that no single strategy trivially
dominates.

## TL;DR

The 2026 format expansion (48 teams, new Round of 32) does most of the
balancing work for us. Recommended starting design:

1. **Simple scoring weights** — 3W/1D per match, advancement bonuses
   1/2/4/6/10/15 for R32/R16/QF/SF/F/W, 5 pts per player goal.
2. **Roster cap of 12 picks.** Forces real tradeoffs vs. spamming cheap
   options.
3. **$2 player price minimum.** Removes the "buy 80 $1 players who scored
   once" degenerate strategy.
4. **$100 budget**, mix teams and players freely under the cap.

Result: under historical 2018/2022 data scaled to 2026 format, pure
all-team and pure all-player rosters score within 4–8% of each other,
while smart mixed rosters beat both by 27–32%. That's a balanced game with
a meaningful skill premium for thoughtful drafting.

## How the 2026 format adaptation works

WC 2026 expands from 32 → 48 teams, with a new round structure:

- 12 groups of 4 (vs. 8 in 2022)
- Top 2 + 8 best 3rd-place teams advance (32 KO entrants)
- New Round of 32 before R16 → QF → SF → F
- Champion plays 8 matches (vs. 7 in 2022)

To simulate this from 2018/2022 data, we:
- Added 16 plausible expansion teams (Norway, Sweden, Egypt, Algeria,
  Hungary, Czech Republic, Romania, Austria, Nigeria, Ivory Coast,
  Slovakia, Iraq, UAE, Uzbekistan, Jamaica, Panama) with assumed
  group-stage records (mostly weak; forward-looking estimates, not history).
- Credited every team that historically made R16+ with +1 W (the R32 match
  they would have won to reach R16 in the new format).
- Added a `bonus_r32 = 1` point award for any team reaching R32.

## Full balance summary

Greedy-optimal $100 roster under each scoring system, capped at 12 picks:

| Preset                              | Year | Format | All-team | All-player | Best mix | Gap   | Mix+ |
|-------------------------------------|------|--------|---------:|-----------:|---------:|------:|-----:|
| **A. Simple baseline**              | 2022 | **2026** |  **239** |    **230** |  **315** | **4%** | **32%** |
| **A. Simple baseline**              | 2018 | **2026** |  **238** |    **220** |  **302** | **8%** | **27%** |
| A. Simple baseline                  | 2022 | 32T    |      201 |        230 |      310 |  13%  |  35% |
| A. Simple baseline                  | 2018 | 32T    |      210 |        220 |      294 |   5%  |  34% |
| B. Team-boosted                     | 2022 | 2026   |      394 |        230 |      449 |  42%  |  14% |
| B. Team-boosted                     | 2018 | 2026   |      389 |        220 |      434 |  43%  |  12% |
| C. Team-boosted, $2 floor           | 2022 | 2026   |      394 |        230 |      439 |  42%  |  11% |
| C. Team-boosted, $2 floor           | 2018 | 2026   |      389 |        220 |      429 |  43%  |  10% |
| D. Light team boost, $2 floor       | 2022 | 2026   |      297 |        230 |      359 |  23%  |  21% |
| D. Light team boost, $2 floor       | 2018 | 2026   |      295 |        220 |      351 |  25%  |  19% |

- **Gap:** how much one pure strategy beats the other. Lower = balanced.
- **Mix+:** how much smart mixing beats either pure strategy. Higher = skill matters.

**Preset A under 2026 format wins on both metrics.** Boosting team weights
(B/C/D) was the right call for the 32-team format but *overshoots* in 2026
because the format expansion already gives teams extra value via the new
R32 match.

## Recommended starting weights

```python
ScoringWeights(
    # team — per match
    team_win = 3,
    team_draw = 1,
    # team — advancement bonuses (cumulative as team progresses)
    bonus_r32 = 1,
    bonus_r16 = 2,
    bonus_qf = 4,
    bonus_sf = 6,
    bonus_final = 10,
    bonus_champion = 15,
    # player — per event
    player_goal = 5,
    player_assist = 0,       # DROPPED — no free source for assists; user accepted goals-only
    player_clean_sheet = 4,  # PENDING USER DECISION — automatic to compute from match score,
                             # but need to decide who gets credit (GK only? GK+defenders?
                             # whole team?) and seed position data accordingly.
)
```

**Note on the assist decision (2026-05-24):** Initially the simulation
assumed `player_assist = 3` but had no assist data to score against
(group standings + goalscorers list only). When the live game would have
gone in, automated assist data required a paid API (€29/mo football-data
Deep Data). User opted to drop assists from scoring rather than pay.
Re-running with `player_assist = 0` produces identical balance results
(4-8% gap, 27-32% mix advantage) because the original sim's score
contribution from assists was already zero.

## Recommended starting prices ($100 budget, 12-pick max, $2 player floor)

**Teams** (5 tiers, 48 teams total):

| Tier | Price | ~Teams | Examples (2026 candidates) |
|------|-------|-------:|-----------------------------|
| 1    | $15   |    5–6 | top contenders (Brazil, France, Argentina, England, Spain, Germany) |
| 2    | $12   |    6–8 | strong dark horses (Portugal, Netherlands, Belgium, Croatia, Uruguay, …) |
| 3    | $9    |    9–10| outside chance (Mexico, USA, Switzerland, Senegal, Morocco, Japan, Korea, …) |
| 4    | $6    |   12–14| unlikely upsetters (Norway, Sweden, Iran, Wales, Algeria, …) |
| 5    | $3    |   10–12| hosts unmatched / qualifiers (Canada, Saudi Arabia, Costa Rica, Ghana, …) |

**Players** (5 tiers, $2 minimum):

| Tier | Price | Description |
|------|-------|-------------|
| 1    | $10   | global superstars (Mbappé, Haaland, Bellingham-tier) |
| 2    | $7    | top international starters |
| 3    | $5    | strong starters |
| 4    | $3    | dependable squad players |
| 5    | $2    | floor — every other notable player |

## Repricing between rounds (first cut — refine after sim has assists data)

When a team or player is **eliminated**, price drops to **$0** (no salvage).

For survivors, propose:

```
new_price = base_price + min(performance_premium, base_price * 0.5)
```

Where `performance_premium = points_so_far * k`, k tuned so the highest-
scoring asset is at most ~1.5x its original price. Appreciation is real but
capped, so the hot picks reward you both with points scored AND a higher
sell price, but no one becomes unaffordably expensive.

Update cadence: **once per round** (after R32, after R16, after QF, after SF).
Matches the natural transfer windows the user described.

## Caveats — work to do before launch

1. **Assists dropped from scoring** (2026-05-24, user decision). No free
   API source has reliable assist data, and user opted not to pay for the
   one that does. Clean sheet decision still pending: data is free to
   compute but we need to decide who receives credit and seed player
   positions accordingly.

2. **Pricing tiers are hand-curated from public knowledge.** Before kickoff,
   snap each 2026 team and notable player to a tier using pre-tournament
   FIFA ranking + bookmaker odds.

3. **The goalscorer dataset only includes players who scored.** The 2026
   priced player list should include ~150–200 named players across all 48
   squads (stars + key starters + GKs). Players who don't score are still
   priced and tradeable.

4. **Expansion-team records are forward-looking estimates**, not historical.
   The 16 added teams in the 2026 sim are placed at $3 with weak records;
   actual 2026 expansion teams may overperform (e.g., Norway with Haaland)
   and may need to be re-tiered in advance.

5. **12-pick roster cap** confirms with users — adds a constraint on top of
   "fully flexible budget" but data shows it's necessary to prevent
   degenerate spam strategies.

## How to reproduce

```bash
cd "Fantasy World Cup"
./venv/bin/python scripts/fetch_wc_data.py   # pulls Wikipedia data (one-off)
./venv/bin/python scripts/simulate.py        # runs all 4 presets x 2 formats x 2 years
```
