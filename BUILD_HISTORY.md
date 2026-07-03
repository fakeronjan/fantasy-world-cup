# Fantasy World Cup 2026 — Build History (source notes)

**Purpose:** raw source material for a "how it was made" write-up, assembled
from the commit history, in-repo docs, and design decisions. This is a factual
scaffold — timeline, milestones, decisions, numbers, and anecdotes — **not a
drafted piece.** The prose is yours to write; this is what you write *from*.

Cutoff: **2026-07-03** (R16 about to begin). See the [Append log](#append-log)
at the bottom for later additions.

---

## At a glance (by the numbers)

- **~6 weeks**, first commit **2026-05-24**, still shipping **2026-07-03**
- **241 commits** (≈150 substantive, the rest automated projection refreshes)
- **~17,000 lines** across the site (HTML/JS) and Python scripts
- **14 pull requests** for the discrete feature milestones
- **4 World Cups** of historical data used to validate scoring on day one
- **1,204 players** seeded from squads → narrowed to **259 draftable** (tiers 1–4)
- **48 teams**, **$60 budget**, **12-pick cap**
- **20,000 Monte Carlo runs** behind the live projections
- Biggest files: `leaderboard.html` (1,288 LOC), `simulate_2026.py` (1,051),
  `simulate.py` (981), `ingest_results.py` (879)

**Stack:** static site on **GitHub Pages** · **Firebase** (Google auth +
Firestore) · **Python** data/ops scripts · **GitHub Actions** (15-min ingest
cron) · **Resend** for email. No backend server — everything is a static
front-end talking to Firestore, plus cron jobs that write to it.

---

## Timeline / phases

### Phase 1 — Genesis weekend (May 24–26, ~83 commits)
The entire game stood up in three days.
- **Day one (May 24):** landing page, shared styles, Firebase wiring, admin
  console, roster view, live-refresh leaderboard, the scoring foundation, the
  ingest pipeline — and the **scoring balance validated the same day** against 4
  World Cups (weights kept getting tuned through the weekend). 1,204 players
  seeded from WC2026 squads.
- Rapid iteration on the draftable pool (1,204 → 259, tiers 1–4, per-team floor
  of 3), team/player WC-history blurbs, flags-as-emoji.
- **May 25** was the pricing/scoring furnace: continuous pricing, repeated
  rebalances to flatten points-per-dollar ROI, odds-driven team prices, the
  forward-looking reprice engine and the transfer UI (Paid/Earned/MV/Buy/Sell),
  the universal game-state banner, auto round-transitions in the ingest.
- **May 26:** multi-group support + leaderboard tabs, profile/settings page,
  email opt-in + Resend sender + daily digest v2. Tagline: *"Twelve picks.
  Sixty dollars. One World Cup."*

### Phase 2 — Pre-kickoff polish (June 6–11)
- Verified sending domain (`mail.fakeronjan.com`), daily email cron enabled,
  tiebreaker (roster goals + assists).
- **June 11 = World Cup kickoff.** Sign-ups close at kickoff; the draft page
  locks once the tournament starts. Stats page ships (ownership, top scorers,
  best-value picks, transfer activity). Leaderboard gains depth: per-pick
  points, rank movement, differentials, participant detail modal, recent-results
  feed. Firestore read quota tuned (poll 30s → 5min, change-detection + caching).

### Phase 3 — Group stage, live (June 12–24)
- Clickable asset detail modals (points breakdown that reconciles to total +
  game-by-game log), forward-only transfer scoring (banked points +
  points-at-purchase), exited-pick handling (grayed/struck everywhere).
- **Hawaii-time date bucketing** to stop late-night games landing on the wrong
  day. Daily email moved to ~11 AM Eastern, recapping *yesterday's* games.
- Machine **migration mid-project** (old Mac → M5) around June 17–19.
- **The knockout-transition machinery** built ahead of R32 (PR #1): read
  advancers from the bracket, anchor timing to the hardcoded WC2026 schedule,
  add the settle-lock + guaranteed-4h open + snapshot/revert safeguard.
- Form-aware transfer reprice (PR #2): results-to-date drive player value.

### Phase 4 — Knockouts, live (June 26 – July 3)
- **Monte Carlo projections** on the leaderboard (PR #3), using the *real*
  WC2026 bracket rather than standings-seeding; bumped 1k → 20k runs.
- **"Keys to win"** rooting guide (PR #6), title odds + keys + status-aware CTA
  in email (PR #7), Gmail-clipping fix (PR #8).
- Per-player goals/game pricing model, acquisition-round stamping, a run of
  stats-table detail PRs (#10–#14).
- **July 3 sprint:** R16 transfer-CTA reactivated then made self-driving off the
  published schedule; CI actions bumped to Node 24; the **blue-shell gag** (and
  its whole life-cycle — see anecdotes); **Top 5 % / Last %** odds columns on
  the leaderboard and in email.

---

## Key decisions & rationale (the "why")

- **Scoring balance validated up front across 4 World Cups.** WC2018 + WC2022
  data scaled to the 2026 48-team format. The finding (see `SIMULATION_FINDINGS.md`):
  the format expansion does the balancing work — *simple* weights beat
  boosted-team variants. Under the validated baseline, pure all-team and pure
  all-player rosters land within **4–8%** of each other while a smart mix beats
  both by **27–32%** — balanced, with a real skill premium for thoughtful
  drafting. Exact weights were then refined over the genesis weekend
  (real-soccer win 4→3, Fibonacci round bonuses, assist points, DEF-only clean
  sheets). *(Verify the final locked numbers against the code before publishing.)*
- **$100 → $60 budget, 12-pick cap, $2 player floor.** The cap forces real
  tradeoffs; the floor kills the "buy 80 $1 players who scored once" degenerate.
- **Forward-looking market pricing.** Every asset's market value = expected
  *remaining* points ÷ 2 (a chosen calibration anchor, not a measured average —
  the explainer says so explicitly). Buys/sells carry a 10% vig; the "2" is
  tuned so post-vig ROI (~1.7 pts/$) matches the game's overall average.
- **Transfer economy:** 3 buys for the *whole tournament* (not per window),
  unlimited free sells, eliminated picks auto-sold at **25%** of purchase price.
- **Automated, reversible knockout transitions.** When a round's bracket is
  fully seeded the cron snapshots state, reprices, eliminates, auto-sells, flips
  the round, then holds a 30-min settle-lock before opening trading with a
  guaranteed ≥4h window. Every transition is revertible from a snapshot.
- **No backend.** Static front-end + Firestore + cron. Cheap, simple, and it
  scales to a friends-league fine.

---

## Memorable moments / anecdotes

- **The blue-shell gag (July 3).** A Mario Kart rubber-banding joke: when a
  manager's Monte Carlo title odds pass 60%, a cosmetic "🔵🐚 BLUE SHELL
  incoming, -100 points!!" banner targets the runaway leader (Cris V, 67%). No
  points are actually touched. It grew a hand-drawn inline-SVG spiny shell with
  a wobble animation, then a scripted three-act send-off: *incoming* → a gold
  *"grabbed a STAR and dodged it"* payoff → full retirement, all date-gated to
  fire on their own days.
- **Recurring GitHub Pages deploy flakiness.** The built-in `pages-build-deployment`
  intermittently failed at the finalize step ("Deployment failed, try again
  later") with the status page green — a re-run always cleared it. Happened
  several times during the July 3 sprint.
- **The em-dash purge.** A single commit ("Eliminate every emdash project-wide")
  — a standing style rule, enforced retroactively.
- **A Firebase SDK bump + mobile-auth redirect experiment, both reverted** same
  day (May 25) — popup auth stayed.
- **A literal `</script>` inside a JS string** broke the admin page parser early
  on — an easy footgun in a single-file static page.

---

## Architecture snapshot

```
docs/            → GitHub Pages root (the whole front-end)
  index / draft / roster / leaderboard / transfer / stats / profile / admin .html
  shared.js      → Firebase init + auth + shared render helpers
  data/*.json    → seed teams/players, projections.json (cron-refreshed)
scripts/
  ingest_results.py   → 15-min cron: poll scores, update Firestore, auto-transition
  reprice.py          → between-round reprice / eliminate / auto-sell
  project_standings.py→ Monte Carlo projections → projections.json
  send_emails.py      → daily digest + round recaps via Resend
  simulate*.py        → the day-one scoring-balance analysis
.github/workflows/
  ingest.yml          → the 15-min ingest cron
  email_digest.yml    → daily digest (+ manual round-recap dispatch)
```

Reference docs already in the repo: `README.md`, `SIMULATION_FINDINGS.md`,
`docs/MANUAL_KNOCKOUT_TRANSITION.md`.

---

## Append log

Later additions get logged here as the tournament plays out, so the write-up
can cover the full run.

- **2026-07-03** — Doc created. Cutoff at end of group stage / start of R16.
  Still to capture: R16 → QF → SF → Final, the blue-shell send-off actually
  firing, and however the title race (Cris V's runaway lead) resolves.
