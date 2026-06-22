"""Offline unit tests for the knockout-transition bracket logic.

Pure-function tests over synthetic match/team data - NO Firestore, NO network.
Covers the failure mode that previously made the group->R32 transition skip
elimination + repricing, plus the 3rd-place-match safety case.

Run:
  ./venv/bin/python scripts/test_knockout_transition.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import (
    round_fully_seeded, advancer_slugs_for_round,
    eliminated_slugs, team_pending_counts,
    schedule_drift, scheduled_kickoff, transition_overdue,
    ROUND_FIRST_KICKOFF_UTC, parse_iso_utc,
    window_close_at, MIN_OPEN_TRADING_SECONDS,
)

_failures = []

def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _failures.append(name)


def m(round_label, t1, t2, status="TIMED"):
    return {"round": round_label, "team1Id": t1, "team2Id": t2, "status": status}


# ---------------------------------------------------------------------------
# Scenario A: group stage complete, R32 bracket fully seeded.
#   48 teams t0..t47. Each plays one FINISHED group match.
#   Survivors t0..t31 are slotted into 16 R32 fixtures; t32..t47 are out.
# ---------------------------------------------------------------------------
def scenario_group_to_r32(seed_r32=True, partial=False):
    matches = []
    # 24 finished group matches => every team has played
    for i in range(0, 48, 2):
        matches.append(m("group", f"t{i}", f"t{i+1}", status="FINISHED"))
    if seed_r32:
        # 16 R32 fixtures pairing the 32 survivors t0..t31
        for k, i in enumerate(range(0, 32, 2)):
            a, b = f"t{i}", f"t{i+1}"
            if partial and k == 0:
                b = None  # one slot not yet seeded
            matches.append(m("R32", a, b))
    # empty later-round fixtures exist as placeholders
    for _ in range(8):
        matches.append(m("R16", None, None))
    return matches


print("Scenario A1: group done, R32 fully seeded")
ms = scenario_group_to_r32(seed_r32=True)
adv = advancer_slugs_for_round(ms, "R32")
elim = eliminated_slugs(ms, adv, None)
check("R32 bracket reads as fully seeded", round_fully_seeded(ms, "R32") is True)
check("exactly 32 advancers", len(adv) == 32)
check("advancers are t0..t31", adv == {f"t{i}" for i in range(32)})
check("exactly 16 eliminated", len(elim) == 16)
check("eliminated are t32..t47", elim == {f"t{i}" for i in range(32, 48)})
check("no overlap advancers/eliminated", adv.isdisjoint(elim))
check("advancers + eliminated cover all 48", len(adv | elim) == 48)

print("\nScenario A2: group done, R32 only PARTIALLY seeded -> must hold")
ms = scenario_group_to_r32(seed_r32=True, partial=True)
check("partial bracket is NOT fully seeded (hold)", round_fully_seeded(ms, "R32") is False)

print("\nScenario A3: group done, R32 NOT seeded yet -> must hold")
ms = scenario_group_to_r32(seed_r32=False)
check("empty bracket is NOT fully seeded (hold)", round_fully_seeded(ms, "R32") is False)
check("no advancers when unseeded", advancer_slugs_for_round(ms, "R32") == set())


# ---------------------------------------------------------------------------
# Scenario B: SF -> F. Beaten semifinalists must NOT be eliminated until the
# 3rd-place match is played.
# ---------------------------------------------------------------------------
print("\nScenario B: SF complete, F + 3rd-place seeded")
def scenario_sf_to_f(third_done=False):
    return [
        m("SF", "f0", "s0", status="FINISHED"),
        m("SF", "f1", "s1", status="FINISHED"),
        m("F",  "f0", "f1"),
        m("third", "s0", "s1", status="FINISHED" if third_done else "TIMED"),
    ]

ms = scenario_sf_to_f(third_done=False)
adv = advancer_slugs_for_round(ms, "F")
elim = eliminated_slugs(ms, adv, None)
check("F advancers are the two finalists", adv == {"f0", "f1"})
check("F bracket fully seeded", round_fully_seeded(ms, "F") is True)
check("beaten semifinalists NOT eliminated pre-3rd-place", elim == set())

ms = scenario_sf_to_f(third_done=True)
adv = advancer_slugs_for_round(ms, "F")
elim = eliminated_slugs(ms, adv, None)
check("after 3rd-place match, both semifinal losers eliminated", elim == {"s0", "s1"})


# ---------------------------------------------------------------------------
# Scenario C: champion is never flagged eliminated.
# ---------------------------------------------------------------------------
print("\nScenario C: champion excluded from elimination")
ms = [m("F", "f0", "f1", status="FINISHED")]
elim = eliminated_slugs(ms, advancer_ids=set(), champion_slug="f0")
check("champion f0 not eliminated", "f0" not in elim)
check("runner-up f1 eliminated", "f1" in elim)


# ---------------------------------------------------------------------------
# Scenario D: canonical schedule anchoring (no guessing about WHEN).
# ---------------------------------------------------------------------------
print("\nScenario D: schedule anchoring + drift cross-check")
import json
from datetime import timedelta
cache = json.load(open(Path(__file__).resolve().parent.parent / "data" / "wc2026_matches_cache.json"))
# Map the cache's stage labels into our round labels for the drift check.
_stage = {"LAST_32": "R32", "LAST_16": "R16", "QUARTER_FINALS": "QF",
          "SEMI_FINALS": "SF", "FINAL": "F"}
cache_rounds = [{"round": _stage.get(m.get("stage")), "utcDate": m.get("utcDate")} for m in cache]
drift = schedule_drift(cache_rounds)
check("hardcoded schedule matches the stored fixtures (no drift)", drift == [])

r32_ko = scheduled_kickoff("R32")
check("R32 kickoff parses to the known date", r32_ko is not None and r32_ko.isoformat().startswith("2026-06-28"))

# transition_overdue is anchored to the KNOWN kickoff, not a guessed grace window.
just_before = parse_iso_utc("2026-06-28T16:00:00Z")   # 3h before R32 kickoff
well_before  = parse_iso_utc("2026-06-27T00:00:00Z")  # >1 day before
check("overdue fires within 6h of known R32 kickoff when unseeded",
      transition_overdue("R32", bracket_seeded=False, now=just_before) is True)
check("overdue does NOT fire a day out",
      transition_overdue("R32", bracket_seeded=False, now=well_before) is False)
check("overdue never fires once the bracket is seeded",
      transition_overdue("R32", bracket_seeded=True, now=just_before) is False)


# ---------------------------------------------------------------------------
# Scenario E: window close time guarantees >= 4h open trading.
# ---------------------------------------------------------------------------
print("\nScenario E: guaranteed >=4h open-trading window")
MIN_H = MIN_OPEN_TRADING_SECONDS / 3600
# R32 scheduled close = kickoff (2026-06-28T19:00Z) - 1h = 18:00Z.
# Normal case: bracket seeds overnight, window opens 06:00Z -> close = scheduled 18:00Z.
open_early = parse_iso_utc("2026-06-28T06:00:00Z")
close_early = window_close_at(open_early, "R32", lead_seconds=3600)
check("normal open uses scheduled close (18:00Z)", close_early.isoformat().startswith("2026-06-28T18:00"))
check("normal case gives well over 4h", (close_early - open_early).total_seconds() >= MIN_OPEN_TRADING_SECONDS)

# Late seed: window opens 16:00Z -> scheduled 18:00Z would be only 2h, so push to open+4h.
open_late = parse_iso_utc("2026-06-28T16:00:00Z")
close_late = window_close_at(open_late, "R32", lead_seconds=3600)
check("late open pushes close to open+4h", (close_late - open_late).total_seconds() == MIN_OPEN_TRADING_SECONDS)
check(f"late open still guarantees {MIN_H:.0f}h", (close_late - open_late).total_seconds() >= MIN_OPEN_TRADING_SECONDS)
check("late open close lands past scheduled (into the round)", close_late > parse_iso_utc("2026-06-28T18:00:00Z"))


print()
if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("All knockout-transition tests passed.")
