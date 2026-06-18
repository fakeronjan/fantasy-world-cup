"""Read-only one-pass dump of live Firestore game state for balance analysis.
Writes /tmp/fwc_state.json. Does NOT write anything back to Firestore."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client

db = firestore_client()

def pick(d, keys):
    return {k: d.get(k) for k in keys}

cfg = (db.collection("config").document("global").get().to_dict() or {})

matches = []
for m in db.collection("matches").stream():
    md = m.to_dict() or {}
    matches.append(pick(md, ["status", "stage", "matchday", "group"]))

teams = []
for t in db.collection("teams").stream():
    td = t.to_dict() or {}
    row = pick(td, ["name", "totalPoints", "basePrice", "currentPrice", "marketValue",
                    "eliminated", "finalRound", "matchesWon", "matchesDrawn", "goalsFor"])
    row["id"] = t.id
    teams.append(row)

players = []
for p in db.collection("players").stream():
    pd = p.to_dict() or {}
    row = pick(pd, ["name", "position", "teamId", "totalPoints", "goals", "assists",
                    "winsPlayedIn", "cleanSheetsPlayedIn", "basePrice", "currentPrice",
                    "marketValue", "eliminated"])
    row["id"] = p.id
    players.append(row)

users = []
for u in db.collection("users").stream():
    ud = u.to_dict() or {}
    roster = []
    for pk in (ud.get("roster") or []):
        roster.append(pick(pk, ["kind", "assetId", "points", "pointsAtPurchase"]))
    users.append({
        "uid": u.id,
        "displayName": ud.get("displayName") or ud.get("email"),
        "totalPoints": ud.get("totalPoints"),
        "bankedPoints": ud.get("bankedPoints", 0),
        "roster": roster,
        "exitedPicks": ud.get("exitedPicks") or [],
    })

out = {"config": {k: cfg.get(k) for k in ["scoringWeights", "currentRound",
        "kickoffTimestamp", "groupStageEndsAt"]},
       "matches": matches, "teams": teams, "players": players, "users": users}
Path("/tmp/fwc_state.json").write_text(json.dumps(out, default=str))
print(f"dumped: {len(teams)} teams, {len(players)} players, {len(users)} users, {len(matches)} matches")
