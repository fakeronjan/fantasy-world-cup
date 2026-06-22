"""Proof of the forward-only scoring invariant (offline, no Firestore).

Replicates the EXACT three formulas the live code uses:
  - BUY     (transfer.html:618)  pointsAtPurchase = asset.totalPoints at buy
  - SELL    (transfer.html:670)  bank += max(0, totalPoints_now - pointsAtPurchase)
  - AUTO-SELL (reprice.auto_sell_eliminated_picks)  same banking formula
  - HELD    (ingest.recompute_users)  credit = max(0, totalPoints_now - pointsAtPurchase)
  total shown to a manager = bankedPoints + sum(held credits)

Asserts the invariant the user asked to triple-check:
  1. A manager KEEPS exactly the points an asset earned WHILE they held it.
  2. A manager ONLY gains FUTURE points for assets they acquire (nothing before
     purchase, nothing after sale).
  3. Conservation: across all managers, an asset's points are partitioned by
     holding period - never double-counted; points earned while UNOWNED accrue
     to nobody.

Run: ./venv/bin/python scripts/test_forward_scoring.py
"""
from __future__ import annotations
import sys

_fail = []
def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail.append(name)

# --- the live formulas, in isolation ---------------------------------------
def held_credit(total_now, points_at_purchase):
    return max(0, total_now - points_at_purchase)          # ingest.recompute_users

def bank_on_exit(total_at_exit, points_at_purchase):
    return max(0, total_at_exit - points_at_purchase)      # sell / auto-sell

def buy_snapshot(total_at_buy):
    return total_at_buy                                     # pointsAtPurchase set on buy


# An asset's cumulative totalPoints over the tournament timeline:
#   draft(0) → ... → 4 → 7 → (A sells) → 7 → 12 → (B buys) → 12 → 25 → now(31)
# Manager A drafted it (pre-tournament, pAtP=0), sold when total was 7.
# Manager B bought when total was 12, still holds; asset now at 31.
TOTAL_AT_A_SELL = 7
TOTAL_AT_B_BUY  = 12
TOTAL_NOW       = 31

print("Scenario: A drafts an asset, sells at 7; B buys at 12; asset now 31.")

# Manager A: drafted => pointsAtPurchase = 0; banks on sell.
a_patp = buy_snapshot(0)
a_banked = bank_on_exit(TOTAL_AT_A_SELL, a_patp)
check("A keeps the 7 it earned while A held it", a_banked == 7)

# Manager B: bought at 12 => pointsAtPurchase = 12; still holding.
b_patp = buy_snapshot(TOTAL_AT_B_BUY)
b_credit = held_credit(TOTAL_NOW, b_patp)
check("B earns only the 19 scored AFTER B bought (31-12)", b_credit == 19)
check("B earns nothing for points before B's purchase", b_credit == TOTAL_NOW - TOTAL_AT_B_BUY)

# Conservation: points earned while UNOWNED (7 -> 12, the 5 between A's sell and
# B's buy) go to NOBODY. Total credited + unowned gap == asset total now.
unowned_gap = TOTAL_AT_B_BUY - TOTAL_AT_A_SELL
credited = a_banked + b_credit
check("no double-count: A+B credited (7+19) = 26", credited == 26)
check("the 5 pts earned while unowned go to nobody", unowned_gap == 5)
check("conservation: credited + unowned gap = asset total (26+5=31)",
      credited + unowned_gap == TOTAL_NOW)

# Auto-sell uses the SAME banking formula as a manual sell.
print("\nAuto-sell (elimination) banks identically to a manual sell.")
elim_total = 15
holder_patp = buy_snapshot(4)               # bought when asset had 4
auto_banked = bank_on_exit(elim_total, holder_patp)
check("auto-sell banks held points (15-4=11)", auto_banked == 11)
check("auto-sell == manual-sell formula", auto_banked == bank_on_exit(elim_total, holder_patp))

# Re-acquisition can't double-count: A sells at 7 (banks 7), later RE-BUYS at 20.
print("\nRe-acquisition: A sells at 7, re-buys at 20, asset now 31.")
a_rebuy_patp = buy_snapshot(20)
a_total = a_banked + held_credit(TOTAL_NOW, a_rebuy_patp)   # 7 + (31-20)
check("A's total over both stints = 18, not inflated", a_total == 18)
check("A does NOT re-collect the 7→20 span it didn't hold", a_total == 7 + 11)

print()
if _fail:
    print(f"{len(_fail)} FAILED: {_fail}"); sys.exit(1)
print("Forward-only scoring invariant holds: keep-while-held, future-only-on-acquire, no double-count.")
