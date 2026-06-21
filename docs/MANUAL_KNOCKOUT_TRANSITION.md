# Manual knockout-transition procedure (backup)

The cron (`ingest_results.py`) now transitions each round **automatically and
safely**: it waits until the next round's bracket is fully seeded, reprices,
eliminates the knocked-out teams, auto-sells them off rosters, and only then
flips `currentRound` and opens the transfer window. See
`maybe_transition_round()`.

This doc is the **manual backstop** if the automated path is stuck (e.g. the
feed seeds the bracket in a weird partial state, the cron is down, or you just
want to drive the group→R32 turnover by hand). The group→R32 boundary is the
big one — 16 teams eliminated at once — so this is written for it, but the same
steps work for any round (`--from-round R16|QF|SF|F`).

> Run everything from the repo root with the service-account credentials set:
> ```bash
> cd ~/code/fakeronjan/games/fantasy-world-cup
> export SA="$PWD/$(ls fantasy-world-cup-2026-firebase-adminsdk-fbsvc-*.json | head -1)"
> ```

---

## 0. Has the automated path already handled it?

Don't double-apply. Check current state (read-only):

```bash
GOOGLE_APPLICATION_CREDENTIALS="$SA" ./venv/bin/python scripts/_dump_state.py
GOOGLE_APPLICATION_CREDENTIALS="$SA" ./venv/bin/python - <<'PY'
import json; s=json.load(open('/tmp/fwc_state.json'))
print("currentRound:", s['config']['currentRound'])
print("teams eliminated:", sum(1 for t in s['teams'] if t.get('eliminated')))
print("teams with marketValue set:", sum(1 for t in s['teams'] if t.get('marketValue') is not None))
PY
```

If `currentRound` is already `R32`, ~16 teams are eliminated, and survivors
have a `marketValue` — **you're done, stop here.**

---

## 1. Confirm the R32 bracket is fully seeded

The reprice is only correct once **every** R32 fixture has both teams assigned
(the feed fills these only after FIFA officially sets the bracket, which can lag
the final group whistle by hours). Check:

```bash
GOOGLE_APPLICATION_CREDENTIALS="$SA" ./venv/bin/python - <<'PY'
import sys; sys.path.insert(0,'scripts')
from _fwc_lib import firestore_client, round_fully_seeded, advancer_slugs_for_round
db = firestore_client()
ms = [m.to_dict() or {} for m in db.collection('matches').stream()]
print("R32 fully seeded:", round_fully_seeded(ms, "R32"))
print("teams slotted into R32:", len(advancer_slugs_for_round(ms, "R32")), "(expect 32)")
PY
```

If `fully seeded: False` or the count isn't 32 — **wait and re-check.** Do not
proceed; a partial bracket would wrongly eliminate real survivors.

## 2. Preview the new prices (dry run, no writes)

```bash
./venv/bin/python scripts/reprice.py --from-round R32 --runs 1000 --top 20
```

Sanity-check the top teams/players look reasonable (strong survivors priced
highest). Nothing is written.

## 3. Flip `currentRound` to R32 — but keep the window CLOSED

Open `admin.html`, **Advance round to: R32 → Set**. Leave the transfer window
**closed** for now.

Why first: it flips ingest's elimination logic into "group is over" mode, so any
cron run that lands mid-procedure keeps the knocked-out teams consistently
flagged instead of un-flagging them.

## 4. Run the live reprice + elimination + auto-sell

```bash
GOOGLE_APPLICATION_CREDENTIALS="$SA" \
  ./venv/bin/python scripts/reprice.py --from-round R32 --write
```

This (idempotent — safe to re-run): reprices the 32 survivors, zeroes + freezes
liquidation value on the 16 eliminated teams and their players, auto-sells dead
picks off every roster (refunding 25% of each holder's purchase price), and
snapshots roster values. Expected log: `32 teams advanced ... 16 eliminated`.

## 5. Verify

```bash
GOOGLE_APPLICATION_CREDENTIALS="$SA" ./venv/bin/python scripts/_dump_state.py
GOOGLE_APPLICATION_CREDENTIALS="$SA" ./venv/bin/python - <<'PY'
import json; s=json.load(open('/tmp/fwc_state.json'))
elim=[t for t in s['teams'] if t.get('eliminated')]
print("eliminated teams:", len(elim), "(expect 16)")
print("survivors priced:", sum(1 for t in s['teams'] if t.get('marketValue')), "(expect ~32)")
bad=[t['name'] for t in elim if (t.get('marketValue') or 0)!=0]
print("eliminated-but-nonzero-price (should be empty):", bad)
print("any roster still holding an eliminated team:",
      any(pk['assetId'] in {t['id'] for t in elim}
          for u in s['users'] for pk in u['roster']))
PY
```

Want: 16 eliminated, survivors priced, no eliminated team with a nonzero price,
and no roster still holding an eliminated team (auto-sell cleared them).

## 6. Open the transfer window

`admin.html` → **Open transfer window**. Users can now trade on the new prices.
The cron will auto-close it ~1h before the first R32 match
(`WINDOW_CLOSE_LEAD_SECONDS`).

---

## Rollback / re-run notes

- The reprice is **idempotent**: re-running step 4 won't double-refund or
  double-eliminate (eliminated assets carry a frozen `liquidationValue`; a pick
  is auto-sold only once because it's removed from the roster).
- If you flipped the round too early (bracket wasn't actually complete), set
  `currentRound` back via `admin.html` and keep the window closed until step 1
  passes cleanly.
- `transactions` records every auto-sell (`type: auto-sell-elimination`), so
  there's an audit trail if a user disputes a refund.
