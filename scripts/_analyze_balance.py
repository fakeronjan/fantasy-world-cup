"""Team-vs-player balance analysis off the local dump. Read-only, no Firestore."""
import json
from collections import Counter, defaultdict

S = json.load(open("/tmp/fwc_state.json"))
teams, players, users = S["teams"], S["players"], S["matches"],
teams, players, users, matches = S["teams"], S["players"], S["users"], S["matches"]
W = S["config"].get("scoringWeights") or {}

def pts(x): return int(x.get("totalPoints") or 0)
def bp(x):  return float(x.get("basePrice") or 0)

line = lambda: print("-" * 72)

# ---- 1. Tournament progress ----
print("=" * 72); print("  TOURNAMENT PROGRESS"); print("=" * 72)
fin = [m for m in matches if m.get("status") == "FINISHED"]
by_stage = Counter(m.get("stage") for m in fin)
all_stage = Counter(m.get("stage") for m in matches)
print(f"currentRound: {S['config'].get('currentRound')}   finished: {len(fin)}/{len(matches)} matches")
for st in all_stage:
    print(f"   {st:<18} {by_stage.get(st,0):>3}/{all_stage[st]:>3} played")

# ---- 2. Realized points, all assets ----
print(); print("=" * 72); print("  REALIZED POINTS SO FAR (all assets)"); print("=" * 72)
tp = sum(pts(t) for t in teams); pp = sum(pts(p) for p in players)
tspend = sum(bp(t) for t in teams); pspend = sum(bp(p) for p in players)
print(f"{'':14}{'#assets':>9}{'tot pts':>9}{'$ (base)':>10}{'pts/asset':>11}{'pts/$':>9}")
for label, arr in [("TEAMS", teams), ("PLAYERS", players)]:
    n=len(arr); P=sum(pts(x) for x in arr); D=sum(bp(x) for x in arr)
    print(f"{label:14}{n:>9}{P:>9}{D:>10.0f}{P/n:>11.2f}{(P/D if D else 0):>9.3f}")
print(f"\n  team:player point share = {tp/(tp+pp):.0%} : {pp/(tp+pp):.0%}")
print(f"  pts/$  players are {(pp/pspend)/(tp/tspend):.2f}x teams right now")

# ---- 3. Ownership-weighted (what managers actually hold) ----
print(); print("=" * 72); print("  OWNERSHIP-WEIGHTED (the 20 live rosters)"); print("=" * 72)
tid = {t["id"]: t for t in teams}; pid = {p["id"]: p for p in players}
own = {"team": {"slots":0,"pts":0,"spend":0.0}, "player": {"slots":0,"pts":0,"spend":0.0}}
for u in users:
    for pk in u["roster"]:
        kind = pk.get("kind"); a = (tid if kind=="team" else pid).get(pk.get("assetId"))
        if not a: continue
        own[kind]["slots"] += 1
        own[kind]["pts"]   += int(pk.get("points") or pts(a))
        own[kind]["spend"] += bp(a)
print(f"{'':14}{'slots':>7}{'% slots':>9}{'pts':>8}{'% pts':>8}{'$ spent':>9}{'pts/$':>8}")
tot_slots = sum(o["slots"] for o in own.values()); tot_pts = sum(o["pts"] for o in own.values())
for kind in ("team","player"):
    o=own[kind]
    print(f"{kind.upper():14}{o['slots']:>7}{o['slots']/tot_slots:>9.0%}{o['pts']:>8}"
          f"{o['pts']/tot_pts:>8.0%}{o['spend']:>9.0f}{(o['pts']/o['spend'] if o['spend'] else 0):>8.3f}")

# ---- 4. Concentration / top-heavy test ----
print(); print("=" * 72); print("  CONCENTRATION  (is player scoring top-heavy?)"); print("=" * 72)
ps = sorted(players, key=pts, reverse=True)
ts = sorted(teams, key=pts, reverse=True)
def share(arr, k):
    tot=sum(pts(x) for x in arr); return sum(pts(x) for x in arr[:k])/tot if tot else 0
print(f"  player pts: top4={share(ps,4):.0%}  top10={share(ps,10):.0%}  top20={share(ps,20):.0%}  (of all player pts)")
print(f"  team   pts: top4={share(ts,4):.0%}  top10={share(ts,10):.0%}  top20={share(ts,20):.0%}  (of all team pts)")
print(f"\n  Top 12 scoring ASSETS overall (T=team P=player):")
allassets = [("T",t) for t in teams] + [("P",p) for p in players]
for tag, a in sorted(allassets, key=lambda z: pts(z[1]), reverse=True)[:12]:
    extra = (f"{a.get('goals',0)}G {a.get('assists',0)}A" if tag=="P"
             else f"{a.get('matchesWon',0)}W {a.get('matchesDrawn',0)}D")
    print(f"    {tag} {a['name']:<22} {pts(a):>3} pts   ${bp(a):>2.0f}   {extra}")

# ---- 5. Back-loading model: how much of each type's EV is even REACHABLE yet ----
print(); print("=" * 72); print("  STRUCTURAL BACK-LOADING (why this is expected)"); print("=" * 72)
bonus = {"R32":W.get("bonus_r32",1),"R16":W.get("bonus_r16",2),"QF":W.get("bonus_qf",3),
         "SF":W.get("bonus_sf",5),"F":W.get("bonus_final",8),"W":W.get("bonus_champion",12)}
champ_bonus = sum(bonus.values())
champ_results = W.get("team_win",3)*5 + W.get("team_draw",1)*1   # ~5 wins + 1 draw over 7 games
champ_total = champ_bonus + champ_results
print(f"  Eventual CHAMPION team EV ~= {champ_total} pts = {champ_results} from results + {champ_bonus} from advancement bonuses")
print(f"    -> advancement bonuses ({champ_bonus} pts, {champ_bonus/champ_total:.0%} of a champion's value) are KNOCKOUT-ONLY.")
print(f"    -> through the group stage a team can earn at most ~{W.get('team_win',3)*3} pts (3 wins), ZERO bonus.")
grp = all_stage.get("GROUP_STAGE",0) or sum(v for k,v in all_stage.items() if "GROUP" in (k or ""))
print(f"  Group stage = {grp}/{len(matches)} matches ({grp/len(matches):.0%} of the tournament).")
print(f"  Players accrue goals/assists/win-share EVERY match (group + knockout) -> roughly linear.")
print(f"  Teams load {champ_bonus/champ_total:.0%} of their value into the {len(matches)-grp} knockout matches still ahead.")
