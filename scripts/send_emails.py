"""Send daily-digest or round-recap emails to opted-in users via Resend.

Two modes:
  --mode daily      : daily digest with rank, deltas, today's results
  --mode round      : round-end recap, fired when a round completes

Targets users with user.emailNotificationsEnabled == True. Reads user
data from Firestore, formats per-user content, sends via Resend's
HTTP API. Each email contains a link back to /profile.html for
unsubscribe (toggle the opt-in off there).

Setup (one-time):
  1. Sign up at https://resend.com (free up to 3000/month)
  2. Get an API key from the dashboard
  3. Add it as a GitHub Secret named RESEND_API_KEY
  4. (Optional) Configure a custom sender domain. For v1 we use the
     default resend.dev sender which works without verification.

Run from project root:
  RESEND_API_KEY=re_xxx \\
  GOOGLE_APPLICATION_CREDENTIALS=$PWD/<sa>.json \\
    ./venv/bin/python scripts/send_emails.py --mode daily --dry-run

The cron in .github/workflows/email_digest.yml triggers this daily.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fwc_lib import firestore_client

RESEND_URL = "https://api.resend.com/emails"
SENDER = "Fantasy WC <noreply@mail.fakeronjan.com>"

SITE_URL = "https://fakeronjan.github.io/fantasy-world-cup/"
PROFILE_URL = SITE_URL + "profile.html"
LEADERBOARD_URL = SITE_URL + "leaderboard.html"
TRANSFER_URL = SITE_URL + "transfer.html"
ARCHIVE_URL = SITE_URL + "archive-2026.html"

PROJECTIONS_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "projections.json"

ROUND_NAMES = {
    "group": "group stage", "R32": "Round of 32", "R16": "Round of 16",
    "QF": "quarter-finals", "SF": "semi-finals", "F": "final",
}

# Deadlines are shown in US Eastern (the WC2026 host region). June/July is EDT
# (UTC-4); a fixed offset is fine for the tournament window and avoids a tz dep.
EASTERN = timezone(timedelta(hours=-4))

ROSTER_BUDGET = 60  # salary cap; matches leaderboard.html / transfer.html


def budget_remaining(u: dict) -> int:
    """Unspent budget, mirroring leaderboard.html's cash-on-hand calc: the
    stored currentBudget (tracks auto-sell refunds) falling back to the cap
    minus currently-held purchase prices."""
    if u.get("currentBudget") is not None:
        return int(u["currentBudget"])
    held = sum(int(p.get("purchasePrice") or 0)
               for p in (u.get("roster") or []) if p.get("currentlyHeld") is not False)
    return ROSTER_BUDGET - held


def _to_dt(v):
    """Coerce a Firestore timestamp (admin SDK datetime) or ISO string to an
    aware datetime, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fmt_dt(dt) -> str:
    if not dt:
        return ""
    return dt.astimezone(EASTERN).strftime("%a %b %-d, %-I:%M %p ET")


def load_game_state(db) -> dict:
    """Mirror docs/shared.js getGameState() so the email's transfer CTA matches
    what the site shows: open now vs opening soon vs pre-kickoff vs done."""
    c = db.collection("config").document("global").get().to_dict() or {}
    round_ = c.get("currentRound") or "pre"
    kdt = _to_dt(c.get("kickoffTimestamp"))
    before_kickoff = (kdt is None) or (datetime.now(timezone.utc) < kdt)
    if round_ == "done":
        return {"state": "done", "round": round_}
    if before_kickoff:
        return {"state": "pre-kickoff", "round": round_, "kickoff": kdt}
    if c.get("transitionState") is True and c.get("transferWindowOpen") is not True:
        return {"state": "transition-settling", "round": round_}
    if c.get("transferWindowOpen") is True:
        return {"state": "window-open", "round": round_,
                "closesAt": _to_dt(c.get("windowClosesAt"))}
    return {"state": "round-in-progress", "round": round_}


def load_projections() -> dict:
    """uid -> projection row from the committed projections.json (best-effort;
    absence degrades the email gracefully to no odds/keys block)."""
    try:
        data = json.loads(PROJECTIONS_PATH.read_text())
        return {r["uid"]: r for r in data.get("users", [])}
    except (OSError, ValueError, KeyError):
        return {}


def name_for(u: dict) -> str:
    return (u.get("leagueNickname") or u.get("displayName") or u.get("email") or "Player").strip()


def flag_for(u: dict) -> str:
    return (u.get("countryFlag") or "").strip()


# Calendar dates use Hawaii time (UTC-10, no DST) so a late-night-Eastern game
# stays on its intended matchday - matches the ingest's hawaii_date logic.
HAWAII_TZ = timezone(timedelta(hours=-10))

def load_recap_matches(db, date_iso: str) -> list[dict]:
    """Finished matches whose Hawaii matchday == date_iso, full detail."""
    finished = []
    for m in db.collection("matches").stream():
        d = m.to_dict() or {}
        if d.get("status") != "FINISHED":
            continue
        utc = d.get("utcDate") or d.get("kickoff") or ""
        try:
            mh = datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(HAWAII_TZ).strftime("%Y-%m-%d")
        except ValueError:
            continue
        if mh != date_iso:
            continue
        finished.append({"id": m.id, **d})
    finished.sort(key=lambda m: m.get("utcDate", ""))
    return finished


def goals_summary_text(match: dict, players_cache: dict) -> str:
    """Comma-joined scorer names for an email match line."""
    goals = match.get("goals") or []
    if not goals: return ""
    parts = []
    for g in goals:
        scorer = g.get("scorerName") or "?"
        minute = g.get("minute")
        marker = f" {minute}'" if minute else ""
        parts.append(f"{scorer}{marker}")
    return " · ".join(parts)


def points_today_for_pick(pick: dict, today_matches: list[dict],
                          teams_cache: dict, players_cache: dict) -> tuple[int, list[str]]:
    """For one pick, sum points + reasons earned across today's matches.
    Returns (pts, [reason_strings]). Empty list if no activity today."""
    pts = 0
    reasons: list[str] = []
    if pick.get("kind") == "team":
        tid = pick.get("assetId")
        for m in today_matches:
            if m.get("team1Id") != tid and m.get("team2Id") != tid:
                continue
            is_home = m.get("team1Id") == tid
            my_s  = m.get("score1") if is_home else m.get("score2")
            opp_s = m.get("score2") if is_home else m.get("score1")
            opp_n = m.get("team2Name") if is_home else m.get("team1Name")
            if my_s is None or opp_s is None:
                continue
            if my_s > opp_s:
                pts += 3; reasons.append(f"+3 win vs {opp_n}")
            elif my_s < opp_s:
                reasons.append(f"lost {my_s}-{opp_s} vs {opp_n}")
            else:
                pts += 1; reasons.append(f"+1 draw vs {opp_n}")
    elif pick.get("kind") == "player":
        pid = pick.get("assetId")
        asset = players_cache.get(pid) or {}
        fdid = asset.get("fdId")
        team_id = asset.get("teamId")
        pos = asset.get("position")
        for m in today_matches:
            if m.get("team1Id") != team_id and m.get("team2Id") != team_id:
                continue
            is_home = m.get("team1Id") == team_id
            lineup = (m.get("lineup1") if is_home else m.get("lineup2")) or []
            if fdid not in lineup:
                continue
            my_s  = m.get("score1") if is_home else m.get("score2")
            opp_s = m.get("score2") if is_home else m.get("score1")
            # Goals
            goals_scored = sum(1 for g in (m.get("goals") or []) if g.get("scorerFdId") == fdid)
            if goals_scored:
                pts += 5 * goals_scored
                reasons.append(f"+{5*goals_scored} ({goals_scored} goal{'s' if goals_scored > 1 else ''})")
            # Assists
            assists = sum(1 for g in (m.get("goals") or []) if g.get("assistFdId") == fdid)
            if assists:
                pts += 3 * assists
                reasons.append(f"+{3*assists} ({assists} assist{'s' if assists > 1 else ''})")
            # Win share
            if my_s is not None and opp_s is not None and my_s > opp_s:
                pts += 1; reasons.append("+1 win share")
            # CS bonus
            if opp_s == 0:
                if pos == "GK":
                    pts += 5; reasons.append("+5 clean sheet (GK)")
                elif pos == "DEF":
                    pts += 2; reasons.append("+2 clean sheet (DEF)")
    return pts, reasons


def name_for_pick(pick: dict, teams_cache: dict, players_cache: dict) -> tuple[str, str]:
    """(display_name, emoji) for a pick."""
    if pick.get("kind") == "team":
        a = teams_cache.get(pick["assetId"]) or {}
        return (a.get("name") or pick["assetId"], a.get("emoji") or "")
    a = players_cache.get(pick["assetId"]) or {}
    team = teams_cache.get(a.get("teamId")) or {}
    return (a.get("name") or pick["assetId"], team.get("emoji") or "")


def render_top5_block_html(label: str, members: list[dict], current_uid: str) -> str:
    if not members:
        return ""
    rows = ""
    for i, u in enumerate(members[:5]):
        uflag = flag_for(u)
        uname = name_for(u)
        is_me = u["uid"] == current_uid
        rows += (f'<tr>'
                 f'<td style="padding:4px 8px; color:#666; width:24px">{i+1}</td>'
                 f'<td style="padding:4px 8px">{(uflag + " ") if uflag else ""}{escape_html(uname)}'
                 + (' <strong style="color:#ff6eb4">(you)</strong>' if is_me else '')
                 + f'</td>'
                 f'<td style="padding:4px 8px; text-align:right; font-weight:700">{int(u.get("totalPoints") or 0)}</td>'
                 f'</tr>')
    return (f'<h3 style="margin:18px 0 6px; font-size:13px; color:#1a6b8a">{escape_html(label)}</h3>'
            f'<table style="width:100%; border-collapse:collapse; font-size:13px">{rows}</table>')


def render_leaderboards_html(user: dict, all_users: list[dict]) -> str:
    """One top-5 block per group the user is in. If user has no groups,
    show a single global top-5."""
    groups = user.get("groupIds") or ([user["groupId"]] if user.get("groupId") else [])
    if not groups:
        return render_top5_block_html("League leaderboard (top 5)", all_users, user["uid"])
    out = ""
    for g in groups:
        members = [u for u in all_users if g in (u.get("groupIds") or [])
                                         or u.get("groupId") == g]
        members.sort(key=lambda u: -(u.get("totalPoints") or 0))
        out += render_top5_block_html(f"{g} (top 5)", members, user["uid"])
    return out


def render_roster_html(roster: list[dict], teams_cache: dict, players_cache: dict) -> str:
    if not roster:
        return '<p style="color:#888; font-size:13px; font-style:italic">No picks in your roster yet.</p>'
    rows = ""
    for pick in roster:
        name, emoji = name_for_pick(pick, teams_cache, players_cache)
        kind_chip = ('<span style="background:#1a6b8a; color:#fff; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:700">T</span>'
                     if pick.get("kind") == "team"
                     else '<span style="background:#ff6eb4; color:#fff; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:700">P</span>')
        cache = teams_cache if pick["kind"] == "team" else players_cache
        asset = cache.get(pick["assetId"]) or {}
        total_pts = int(asset.get("totalPoints") or 0)
        paid = int(pick.get("purchasePrice") or 0)
        rows += (f'<tr>'
                 f'<td style="padding:3px 8px; width:24px">{kind_chip}</td>'
                 f'<td style="padding:3px 8px">{(emoji + " ") if emoji else ""}{escape_html(name)}</td>'
                 f'<td style="padding:3px 8px; text-align:right; color:#666; font-size:12px">paid ${paid}</td>'
                 f'<td style="padding:3px 8px; text-align:right; font-weight:700">{total_pts} pts</td>'
                 f'</tr>')
    return (f'<h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Your roster ({len(roster)} picks)</h3>'
            f'<table style="width:100%; border-collapse:collapse; font-size:13px">{rows}</table>')


def render_picks_today_html(roster: list[dict], today_matches: list[dict],
                             teams_cache: dict, players_cache: dict) -> str:
    if not today_matches:
        return ""
    rows = ""
    for pick in roster:
        pts, reasons = points_today_for_pick(pick, today_matches, teams_cache, players_cache)
        if not reasons:
            continue
        name, emoji = name_for_pick(pick, teams_cache, players_cache)
        reasons_str = " · ".join(reasons)
        rows += (f'<tr>'
                 f'<td style="padding:5px 8px">{(emoji + " ") if emoji else ""}<strong>{escape_html(name)}</strong></td>'
                 f'<td style="padding:5px 8px; text-align:right; font-weight:700; color:#ff6eb4; font-size:14px">+{pts}</td>'
                 f'<td style="padding:5px 8px; color:#444; font-size:12px">{escape_html(reasons_str)}</td>'
                 f'</tr>')
    if not rows:
        return ""
    return (f'<h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Your picks in yesterday\'s action</h3>'
            f'<table style="width:100%; border-collapse:collapse; font-size:13px">{rows}</table>')


def render_today_matches_html(today_matches: list[dict], players_cache: dict) -> str:
    if not today_matches:
        return '<p style="color:#888; font-size:13px; font-style:italic; margin-top:16px">No matches yesterday.</p>'
    items = ""
    for m in today_matches:
        line = f"{escape_html(m.get('team1Name') or '?')} {m.get('score1', '?')}-{m.get('score2', '?')} {escape_html(m.get('team2Name') or '?')}"
        goal_summary = goals_summary_text(m, players_cache)
        goal_html = f'<br><span style="color:#666; font-size:11px">{escape_html(goal_summary)}</span>' if goal_summary else ""
        items += f'<li style="margin:6px 0; font-size:13px"><strong>{escape_html(m.get("round") or "?")}</strong> · {line}{goal_html}</li>'
    return f'<h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Yesterday\'s results</h3><ul style="padding-left:18px; margin:0">{items}</ul>'


def render_footer_html(is_final: bool = False) -> str:
    """Email footer whose LAST line is a per-send timestamp. Gmail threads our
    daily emails (same sender + constant 'Fantasy WC' subject prefix) and
    collapses any identical trailing block behind its 'trim repeated content'
    toggle - which was hiding the leaderboard + the previously-static footer. A
    unique final line makes every email's trailing block distinct, so there's
    nothing for Gmail to collapse.

    is_final=True is the Final recap, which bypasses the opt-in check (goes
    to every user who played, not just emailNotificationsEnabled=True) - the
    normal "you opted in" line would be false for anyone who didn't opt in."""
    sent = datetime.now(timezone.utc).astimezone(EASTERN).strftime("%b %-d, %Y at %-I:%M %p ET")
    reason = (
        "You're getting this because you played Fantasy World Cup 2026 - thanks for playing!"
        if is_final else
        "You're getting this because you opted into Fantasy WC emails on your profile."
    )
    return f"""
  <hr style="margin:32px 0; border:none; border-top:1px solid #ddd">
  <p style="font-size:11px; color:#888">
    {reason}
    <a href="{PROFILE_URL}" style="color:#1a6b8a">Manage email preferences</a>.
  </p>
  <p style="font-size:10px; color:#bbb; margin-top:6px">Sent {sent}</p>"""


def render_transfer_cta_html(gs: dict, budget: int = 0) -> str:
    """Time-aware transfer-market CTA mirroring the leaderboard's states:
    OPEN now (with the closing deadline) vs opening soon vs pre-kickoff draft.
    Returns '' when there's nothing to act on (tournament done / unknown).
    When the user is carrying unspent budget during an actionable window,
    the CTA leads with that dollar figure instead of burying it."""
    state = (gs or {}).get("state")
    rn = ROUND_NAMES.get(gs.get("round"), gs.get("round")) if gs else ""
    if state == "window-open":
        closes = _fmt_dt(gs.get("closesAt"))
        deadline = (f'<div style="font-size:11px; color:#9d174d; margin-top:10px; font-weight:600">'
                    f'&#9201; Window closes {closes}</div>') if closes else ""
        if budget > 0:
            headline = f"&#128257; Transfer market is OPEN &mdash; you have ${budget} to spend"
            body = (f'<strong style="color:#9d174d">${budget} of your budget is sitting unused.</strong> '
                    f'That\'s pure upside left on the table &ndash; spend it on up to <strong>3</strong> new '
                    f'players / countries for the {escape_html(rn)} before the window closes.')
        else:
            headline = "&#128257; Transfer market is OPEN"
            body = f'Sell underperformers and buy up to <strong>3</strong> new players / countries for the {escape_html(rn)}.'
        return f"""
  <div style="background:#fff5fa; border:1px solid #ff6eb4; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:13px; font-weight:800; color:#9d174d; text-transform:uppercase; letter-spacing:0.5px">{headline}</div>
    <div style="font-size:13px; color:#444; margin-top:6px">{body}</div>
    <div style="margin-top:12px"><a href="{TRANSFER_URL}" style="background:#ff6eb4; color:#fff; padding:9px 16px; border-radius:4px; text-decoration:none; font-weight:700; font-size:13px">{'Spend your $' + str(budget) + ' &rarr;' if budget > 0 else 'Make transfers &rarr;'}</a></div>
    {deadline}
  </div>"""
    if state in ("round-in-progress", "transition-settling"):
        budget_line = (f'<div style="font-size:12px; color:#9d174d; margin-top:6px; font-weight:700">'
                        f'&#128176; You\'ll be sitting on ${budget} unspent when it reopens &ndash; have your targets picked out.</div>'
                        ) if budget > 0 else ""
        return f"""
  <div style="background:#f8f8f6; border:1px solid #ddd; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:13px; font-weight:800; color:#1a6b8a; text-transform:uppercase; letter-spacing:0.5px">&#128257; Transfer market opening soon</div>
    <div style="font-size:13px; color:#444; margin-top:6px">Rosters are locked during the {escape_html(rn)}. The market reopens once the round finishes &ndash; line up your moves now.</div>
    {budget_line}
    <div style="margin-top:12px"><a href="{TRANSFER_URL}" style="background:#1a6b8a; color:#fff; padding:9px 16px; border-radius:4px; text-decoration:none; font-weight:700; font-size:13px">Preview the market &rarr;</a></div>
  </div>"""
    if state == "pre-kickoff":
        body = (f'You still have <strong style="color:#075985">${budget}</strong> of your budget unspent &ndash; fill out your roster before kickoff.'
                 if budget > 0 else "Lock in your roster before kickoff.")
        return f"""
  <div style="background:#e0f2fe; border:1px solid #7dd3fc; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:13px; font-weight:800; color:#075985; text-transform:uppercase; letter-spacing:0.5px">&#9203; Draft window open</div>
    <div style="font-size:13px; color:#444; margin-top:6px">{body}</div>
    <div style="margin-top:12px"><a href="{SITE_URL}draft.html" style="background:#075985; color:#fff; padding:9px 16px; border-radius:4px; text-decoration:none; font-weight:700; font-size:13px">Draft your team &rarr;</a></div>
  </div>"""
    return ""


def _transfer_cta_plain(gs: dict, budget: int = 0) -> str:
    state = (gs or {}).get("state")
    rn = ROUND_NAMES.get(gs.get("round"), gs.get("round")) if gs else ""
    if state == "window-open":
        closes = _fmt_dt(gs.get("closesAt"))
        tail = f" Closes {closes}." if closes else ""
        if budget > 0:
            return (f"TRANSFER MARKET OPEN: you have ${budget} sitting unspent - that's points left on "
                     f"the table. Spend it (up to 3 new picks) for the {rn}.{tail} {TRANSFER_URL}")
        return f"TRANSFER MARKET OPEN: buy up to 3 new picks for the {rn}.{tail} {TRANSFER_URL}"
    if state in ("round-in-progress", "transition-settling"):
        tail = f" You'll have ${budget} unspent when it reopens - have your targets ready." if budget > 0 else ""
        return f"Transfer market opens after the {rn}.{tail} Line up your moves: {TRANSFER_URL}"
    if state == "pre-kickoff":
        if budget > 0:
            return f"Draft window open - you still have ${budget} unspent, fill out your roster: {SITE_URL}draft.html"
        return f"Draft window open - lock in your roster: {SITE_URL}draft.html"
    return ""


def _pct_str(v) -> str:
    if v is None:
        return "&ndash;"
    if v >= 1:
        return f"{round(v)}%"
    return "&lt;1%" if v > 0 else "&ndash;"


def render_odds_keys_html(proj: dict, teams_cache: dict, players_cache: dict) -> str:
    """Title odds (win % / top-3 %) + the manager's keys to win, from
    projections.json. Hidden if the user has no projection row."""
    if not proj:
        return ""
    chips = ""
    for k in (proj.get("keys") or []):
        if k.get("kind") == "team":
            a = teams_cache.get(k["id"]) or {}
            nm, emoji, color = (a.get("name") or k["id"]), (a.get("emoji") or ""), "#1a6b8a"
        else:
            a = players_cache.get(k["id"]) or {}
            nm = (a.get("name") or k["id"]).split()[-1]
            emoji = (teams_cache.get(a.get("teamId")) or {}).get("emoji") or ""
            color = "#ff6eb4"
        chips += (f'<span style="display:inline-block; margin:2px 10px 2px 0; font-weight:700; '
                  f'color:{color}; font-size:13px">{(emoji + " ") if emoji else ""}{escape_html(nm)}</span>')
    goal = proj.get("keysGoal") or "none"
    keys_label = ("Keys to win" if goal == "win"
                  else "Keys to reach the top 3" if goal == "top3" else "")
    keys_block = (f'<div style="margin-top:12px; border-top:1px solid #e5e5e5; padding-top:10px">'
                  f'<span style="font-size:11px; color:#888; text-transform:uppercase; letter-spacing:0.5px">{keys_label} &#128081;</span>'
                  f'<div style="margin-top:4px">{chips}</div></div>') if chips and keys_label else ""
    return f"""
  <div style="background:#f8f8f6; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:11px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:10px">Title odds &#183; if your current roster plays out</div>
    <table style="width:100%; border-collapse:collapse; text-align:center">
      <tr>
        <td style="width:50%; padding:6px 8px"><div style="font-size:26px; font-weight:800; color:#ff6eb4">{_pct_str(proj.get("winPct"))}</div><div style="font-size:11px; color:#888">to win it all</div></td>
        <td style="width:50%; padding:6px 8px; border-left:1px solid #e5e5e5"><div style="font-size:26px; font-weight:800; color:#1a6b8a">{_pct_str(proj.get("top3Pct"))}</div><div style="font-size:11px; color:#888">top-3 finish</div></td>
      </tr>
      <tr>
        <td style="width:50%; padding:6px 8px; border-top:1px solid #e5e5e5"><div style="font-size:26px; font-weight:800; color:#1a6b8a">{_pct_str(proj.get("top5Pct"))}</div><div style="font-size:11px; color:#888">top-5 finish</div></td>
        <td style="width:50%; padding:6px 8px; border-left:1px solid #e5e5e5; border-top:1px solid #e5e5e5"><div style="font-size:26px; font-weight:800; color:#999">{_pct_str(proj.get("lastPct"))}</div><div style="font-size:11px; color:#888">last place</div></td>
      </tr>
    </table>
    {keys_block}
  </div>"""


def _odds_keys_plain(proj: dict, teams_cache: dict, players_cache: dict) -> str:
    if not proj:
        return ""
    def _plain_pct(key):
        return _pct_str(proj.get(key)).replace("&lt;", "<").replace("&ndash;", "-")
    win, top3, top5, last = (_plain_pct("winPct"), _plain_pct("top3Pct"),
                             _plain_pct("top5Pct"), _plain_pct("lastPct"))
    names = []
    for k in (proj.get("keys") or []):
        if k.get("kind") == "team":
            names.append((teams_cache.get(k["id"]) or {}).get("name") or k["id"])
        else:
            names.append(((players_cache.get(k["id"]) or {}).get("name") or k["id"]).split()[-1])
    goal = proj.get("keysGoal") or "none"
    label = "Keys to win" if goal == "win" else "Keys to reach top 3" if goal == "top3" else ""
    line = f"Title odds: {win} to win · {top3} top-3 · {top5} top-5 · {last} last place"
    if names and label:
        line += f"\n{label}: {', '.join(names)}"
    return line


FLAGS_URL = SITE_URL + "flags.html"
_FLAG_RL = {"wildcard": "Wildcard", "R32": "Round of 32", "R16": "Round of 16",
            "QF": "Quarter-finals", "SF": "Semi-finals", "F": "Final"}


def load_flag_state(db) -> dict | None:
    """Flag Knockout state, or None if not initialized / unreadable."""
    try:
        d = db.collection("flagContest").document("state").get()
        return d.to_dict() if d.exists else None
    except Exception:
        return None


def _flag_promo_parts(fs: dict | None) -> tuple[str, str, str]:
    """(headline, subtext, cta_label) for the flag promo, or ('','','') if it
    shouldn't show. Only shows once the contest is launched (same gate as the
    website). CTA label is state-aware so a done contest never says "Vote"."""
    if not fs or not fs.get("launched"):
        return "", "", ""
    champ = fs.get("champion") or {}
    if fs.get("status") == "done" and champ:
        return (f"&#127942; {escape_html(champ.get('name', '?'))} has the best flag in the world",
                "See how the Flag Knockout bracket played out",
                "See the results &rarr;")
    if fs.get("votingOpen"):
        rn = _FLAG_RL.get(fs.get("currentRound"), "voting")
        return ("&#127987;&#65039; Vote: what's the best flag in the world?",
                f"Flag Knockout - {escape_html(rn)} is LIVE, out of the pool? get your votes in",
                "Vote in the Flag Knockout &rarr;")
    return ("&#127987;&#65039; Vote: what's the best flag in the world?",
            "Flag Knockout - results are in, see which flags advanced",
            "See the results &rarr;")


def render_flag_promo_html(fs: dict | None) -> str:
    head, sub, cta = _flag_promo_parts(fs)
    if not head:
        return ""
    return f'''
  <div style="background:#6d28d9; background:linear-gradient(120deg,#1a6b8a,#6d28d9 45%,#ff6eb4 80%,#f59e0b); border-radius:8px; padding:16px; margin-bottom:16px; color:#fff">
    <div style="font-size:15px; font-weight:800">{head}</div>
    <div style="font-size:13px; opacity:0.95; margin-top:3px">{sub}</div>
    <div style="margin-top:12px"><a href="{FLAGS_URL}" style="background:#fff; color:#6d28d9; padding:9px 16px; border-radius:999px; text-decoration:none; font-weight:800; font-size:13px">{cta}</a></div>
  </div>'''


def _flag_promo_plain(fs: dict | None) -> str:
    head, sub, cta = _flag_promo_parts(fs)
    if not head:
        return ""
    # Strip HTML entities (emoji etc.) and collapse whitespace for plain text.
    # The sub already carries the "Flag Knockout" brand, so no prefix needed.
    import re as _re
    clean = _re.sub(r"\s+", " ", _re.sub(r"&#\d+;|&\w+;", "", head)).strip()
    return f"{clean} - {sub}\n{FLAGS_URL}"


_TIEBREAK_NOTE = {
    "points": "Tied on votes - decided by voters' combined fantasy score.",
    "seed":   "Tied on votes and voter score - decided by seed.",
}


def _flag_final_matchup(fs: dict) -> tuple[dict, dict, int, int, str] | None:
    """(winner, runner_up, winner_pct, loser_pct, tiebreak_note) for the Flag
    Knockout final, or None if the final game isn't recorded (e.g. contest
    never launched). tiebreak_note is '' unless the vote was tied."""
    games = ((fs.get("rounds") or {}).get("F") or {}).get("games") or []
    g = games[0] if games else None
    if not g or g.get("votesA") is None or g.get("votesB") is None:
        return None
    a, b = g.get("a") or {}, g.get("b") or {}
    va, vb = g.get("votesA", 0), g.get("votesB", 0)
    total = va + vb or 1
    pa = round(va / total * 100)
    a_won = (g.get("winner") or {}).get("name") == a.get("name")
    winner, runner_up = (a, b) if a_won else (b, a)
    winner_pct = pa if a_won else 100 - pa
    tiebreak_note = _TIEBREAK_NOTE.get(g.get("tiebreak") or "", "")
    return winner, runner_up, winner_pct, 100 - winner_pct, tiebreak_note


def _flag_emoji(team: dict) -> str:
    """team['emoji'] when present, else computed from the ISO code (each
    letter -> its Unicode regional-indicator symbol). Several Flag Knockout
    entries (e.g. Congo DR, Cape Verde Islands) have an empty stored emoji,
    so this is the reliable path rather than trusting the field alone."""
    e = (team.get("emoji") or "").strip()
    if e:
        return e
    iso = (team.get("iso") or "").strip().upper()
    if len(iso) != 2 or not iso.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def render_flag_results_html(fs: dict | None) -> str:
    """Full Flag Knockout results (champion + final matchup), inline in the
    email - not just a teaser link. Only for the finished contest."""
    if not fs or not fs.get("launched") or fs.get("status") != "done":
        return ""
    champ = fs.get("champion") or {}
    if not champ:
        return ""
    matchup = _flag_final_matchup(fs)
    final_line = ""
    if matchup:
        _, runner_up, winner_pct, loser_pct, tiebreak_note = matchup
        note_html = f' {escape_html(tiebreak_note)}' if tiebreak_note else ''
        final_line = (f'<div style="font-size:13px; opacity:0.95; margin-top:8px">'
                      f'Beat {escape_html(runner_up.get("name", "?"))} in the final, '
                      f'{winner_pct}%-{loser_pct}% of the vote.{note_html}</div>')
    return f'''
  <div style="background:#6d28d9; background:linear-gradient(120deg,#1a6b8a,#6d28d9 45%,#ff6eb4 80%,#f59e0b); border-radius:8px; padding:16px; margin-bottom:16px; color:#fff">
    <div style="font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.6px; opacity:0.9">Flag Knockout results</div>
    <div style="font-size:20px; font-weight:800; margin-top:4px">{_flag_emoji(champ)} {escape_html(champ.get("name", "?"))}</div>
    <div style="font-size:13px; opacity:0.95; margin-top:2px">crowned best flag in the world &ndash; 48 flags entered, one champion</div>
    {final_line}
    <div style="margin-top:12px"><a href="{FLAGS_URL}" style="color:#fff; text-decoration:underline; font-size:12px; opacity:0.9">See the full bracket &rarr;</a></div>
  </div>'''


def _flag_results_plain(fs: dict | None) -> str:
    if not fs or not fs.get("launched") or fs.get("status") != "done":
        return ""
    champ = fs.get("champion") or {}
    if not champ:
        return ""
    matchup = _flag_final_matchup(fs)
    final_line = ""
    if matchup:
        _, runner_up, winner_pct, loser_pct, tiebreak_note = matchup
        note = f" {tiebreak_note}" if tiebreak_note else ""
        final_line = f"Beat {runner_up.get('name', '?')} in the final, {winner_pct}%-{loser_pct}% of the vote.{note}\n"
    return (f"FLAG KNOCKOUT RESULTS\n"
            f"Champion: {champ.get('name', '?')} - crowned best flag in the world (48 entered, one champion)\n"
            f"{final_line}Full bracket: {FLAGS_URL}")


def _flag_round_matchups(fs: dict, round_key: str) -> list[dict]:
    """Completed matchups for one round: [{a, b, winner, votesA, votesB,
    tiebreak_note}, ...]. Skips any game whose votes aren't recorded yet."""
    games = ((fs.get("rounds") or {}).get(round_key) or {}).get("games") or []
    out = []
    for g in games:
        if g.get("votesA") is None or g.get("votesB") is None:
            continue
        out.append({
            "a": g.get("a") or {}, "b": g.get("b") or {},
            "winner": g.get("winner") or {},
            "votesA": g.get("votesA", 0), "votesB": g.get("votesB", 0),
            "tiebreak_note": _TIEBREAK_NOTE.get(g.get("tiebreak") or "", ""),
        })
    return out


_BRACKET_ROUND_LABEL = {"SF": "Semifinal", "F": "Final"}


def render_flag_bracket_html(fs: dict | None) -> str:
    """Semifinal + Final bracket summary with flag emoji - the deeper recap
    (beyond just the champion callout above) that goes lower in the Final
    email. Emoji rather than the ./flags/*.svg images the site uses: those
    are remote images that depend on the recipient's mail client loading
    them at all."""
    if not fs or not fs.get("launched") or fs.get("status") != "done":
        return ""
    round_matchups = [(r, _flag_round_matchups(fs, r)) for r in ("SF", "F")]
    round_matchups = [(r, ms) for r, ms in round_matchups if ms]
    if not round_matchups:
        return ""

    def side_cell(team: dict, votes: int, won: bool) -> str:
        name_style = "font-weight:800; color:#1a6b8a" if won else "color:#999"
        check = " &#10003;" if won else ""
        return (f'<td style="padding:8px 6px; text-align:center; width:33%; vertical-align:top">'
                f'<div style="font-size:30px; line-height:1">{_flag_emoji(team)}</div>'
                f'<div style="font-size:11px; margin-top:4px; {name_style}">{escape_html(team.get("name", "?"))}{check}</div>'
                f'<div style="font-size:10px; color:#aaa">{votes} vote{"s" if votes != 1 else ""}</div></td>')

    rows = ""
    for round_key, matchups in round_matchups:
        rows += (f'<tr><td colspan="3" style="padding:10px 0 4px; font-size:10px; font-weight:700; '
                 f'text-transform:uppercase; letter-spacing:0.5px; color:#aaa">{_BRACKET_ROUND_LABEL[round_key]}</td></tr>')
        for m in matchups:
            a, b, winner = m["a"], m["b"], m["winner"]
            a_won = winner.get("name") == a.get("name")
            note = (f'<div style="font-size:10px; color:#aaa; padding:0 6px 10px; text-align:center">{escape_html(m["tiebreak_note"])}</div>'
                    if m["tiebreak_note"] else "")
            rows += (f'<tr>{side_cell(a, m["votesA"], a_won)}'
                     f'<td style="padding:8px 2px; text-align:center; color:#ccc; font-size:11px; font-weight:700; vertical-align:middle">VS</td>'
                     f'{side_cell(b, m["votesB"], not a_won)}</tr>'
                     f'<tr><td colspan="3">{note}</td></tr>')

    return f'''
  <h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Flag Knockout bracket</h3>
  <table style="width:100%; border-collapse:collapse; margin-bottom:8px">{rows}</table>'''


def _flag_bracket_plain(fs: dict | None) -> str:
    if not fs or not fs.get("launched") or fs.get("status") != "done":
        return ""
    round_matchups = [(r, _flag_round_matchups(fs, r)) for r in ("SF", "F")]
    round_matchups = [(r, ms) for r, ms in round_matchups if ms]
    if not round_matchups:
        return ""
    lines = ["FLAG KNOCKOUT BRACKET"]
    for round_key, matchups in round_matchups:
        lines.append(f"{_BRACKET_ROUND_LABEL[round_key].upper()}:")
        for m in matchups:
            a, b, winner = m["a"], m["b"], m["winner"]
            note = f" ({m['tiebreak_note']})" if m["tiebreak_note"] else ""
            lines.append(f"  {a.get('name', '?')} ({m['votesA']}) vs {b.get('name', '?')} ({m['votesB']}) "
                          f"- {winner.get('name', '?')} advances{note}")
    return "\n".join(lines)


def render_daily_html(user: dict, leaderboard: list[dict], today_matches: list[dict],
                      roster: list[dict],
                      teams_cache: dict, players_cache: dict,
                      proj: dict = None, game_state: dict = None,
                      flag_promo_html: str = "", flag_promo_plain: str = "") -> tuple[str, str, str]:
    """Returns (subject, html_body, plain_text_body)."""
    name  = name_for(user)
    flag  = flag_for(user)
    pts   = int(user.get("totalPoints") or 0)
    # "gain" = points the roster earned in yesterday's matches (the recap window)
    gain  = sum(points_today_for_pick(p, today_matches, teams_cache, players_cache)[0] for p in roster)
    rank  = next((i + 1 for i, u in enumerate(leaderboard) if u["uid"] == user["uid"]), None)

    delta_str = f" (+{gain})" if gain > 0 else ""
    budget = budget_remaining(user)

    subject_parts = ["Fantasy WC", datetime.utcnow().strftime("%b %d")]
    if rank: subject_parts.append(f"Rank #{rank}{delta_str}")
    if budget > 0 and (game_state or {}).get("state") == "window-open":
        subject_parts.append(f"${budget} unspent!")
    subject = " · ".join(subject_parts)

    matches_block      = render_today_matches_html(today_matches, players_cache)
    picks_today_block  = render_picks_today_html(roster, today_matches, teams_cache, players_cache)
    roster_block       = render_roster_html(roster, teams_cache, players_cache)
    leaderboards_block = render_leaderboards_html(user, leaderboard)
    cta_block          = render_transfer_cta_html(game_state or {}, budget)
    odds_keys_block    = render_odds_keys_html(proj, teams_cache, players_cache)

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width:600px; margin:0 auto; padding:24px; color:#1a1a1a">
  <h1 style="color:#1a6b8a; font-size:22px; letter-spacing:1px; margin:0 0 4px">FANTASY WORLD CUP</h1>
  <p style="color:#888; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin:0 0 24px">{datetime.utcnow().strftime("%A, %B %d")}</p>

  <p style="font-size:15px; margin:0 0 16px">Hi {flag}{(" " if flag else "")}{escape_html(name)}, here's your daily roundup.</p>

  <div style="background:#f8f8f6; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:11px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px">Your standing</div>
    <div style="font-size:32px; font-weight:800; color:#ff6eb4">
      {'#' + str(rank) if rank else 'unranked'}
      <span style="font-size:18px; color:#666; font-weight:600">· {pts} pts{delta_str}</span>
    </div>
  </div>
  {flag_promo_html}
  {odds_keys_block}
  {cta_block}
  {matches_block}
  {picks_today_block}
  {roster_block}
  {leaderboards_block}

  <p style="margin-top:32px">
    <a href="{LEADERBOARD_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">View full leaderboard →</a>
  </p>

  {render_footer_html()}
</body></html>"""

    # Plain-text fallback (kept tight)
    roster_plain = "\n".join(
        f"  - {name_for_pick(p, teams_cache, players_cache)[0]} (paid ${int(p.get('purchasePrice') or 0)})"
        for p in roster
    )
    odds_keys_plain = _odds_keys_plain(proj, teams_cache, players_cache)
    cta_plain = _transfer_cta_plain(game_state or {}, budget)
    plain = f"""Fantasy World Cup · {datetime.utcnow().strftime("%A, %B %d")}

Hi {name},

Your standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts{delta_str}
{(flag_promo_plain + chr(10)) if flag_promo_plain else ''}{(odds_keys_plain + chr(10)) if odds_keys_plain else ''}{(cta_plain + chr(10)) if cta_plain else ''}
Yesterday: {(', '.join(f"{m['round']} {m['team1Name']} {m.get('score1','?')}-{m.get('score2','?')} {m['team2Name']}" for m in today_matches)) if today_matches else 'no matches'}

Your roster ({len(roster)} picks):
{roster_plain}

Leaderboard: {LEADERBOARD_URL}
Manage emails: {PROFILE_URL}
"""
    return subject, html, plain


def render_round_recap_html(user: dict, leaderboard: list[dict], round_name: str,
                              roster: list[dict] = None,
                              teams_cache: dict = None, players_cache: dict = None,
                              proj: dict = None, game_state: dict = None,
                              flag_promo_html: str = "", flag_promo_plain: str = "",
                              flag_bracket_html: str = "", flag_bracket_plain: str = "") -> tuple[str, str, str]:
    """Round-end recap email. Lighter content than daily; emphasis on the
    completed round + the freshly-opened transfer window - UNLESS round_name
    is "Final", in which case this is the one-time tournament-closing send:
    no transfer CTA (there's no next window), a real "thanks for playing"
    line, the champion, and a link to the permanent 2026 archive instead of
    the live leaderboard."""
    is_final = (round_name == "Final")
    name = name_for(user)
    flag = flag_for(user)
    pts  = int(user.get("totalPoints") or 0)
    rank = next((i + 1 for i, u in enumerate(leaderboard) if u["uid"] == user["uid"]), None)

    leaderboards_block = render_leaderboards_html(user, leaderboard)
    roster_block = render_roster_html(roster or [], teams_cache or {}, players_cache or {}) if roster is not None else ""

    if is_final:
        champ = next((t for t in (teams_cache or {}).values() if t.get("finalRound") == "W"), None)
        champ_name = champ.get("name") if champ else None
        subject = f"Fantasy WC · Tournament complete{' - ' + champ_name + ' win it all!' if champ_name else '!'}"
        intro = (f'<strong>{escape_html(champ_name)} are 2026 World Cup Champions</strong> - and that\'s a wrap on '
                  f'the fantasy tournament too.' if champ_name else '<strong>The tournament is complete.</strong>') \
            + ' Thanks for playing!'
        html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width:600px; margin:0 auto; padding:24px; color:#1a1a1a">
  <h1 style="color:#1a6b8a; font-size:22px; letter-spacing:1px; margin:0 0 4px">🏆 TOURNAMENT COMPLETE</h1>
  <p style="color:#888; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin:0 0 24px">{datetime.utcnow().strftime("%A, %B %d")}</p>

  <p style="font-size:15px; margin:0 0 16px">Hi {flag}{(" " if flag else "")}{escape_html(name)},</p>

  <p style="font-size:15px; margin:0 0 16px">{intro}</p>

  <div style="background:#f8f8f6; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:11px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px">Your final standing</div>
    <div style="font-size:32px; font-weight:800; color:#ff6eb4">
      {'#' + str(rank) if rank else 'unranked'}
      <span style="font-size:18px; color:#666; font-weight:600">· {pts} pts</span>
    </div>
  </div>
  {roster_block}
  {leaderboards_block}
  {flag_promo_html}
  {flag_bracket_html}

  <p style="margin-top:32px">
    <a href="{ARCHIVE_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">See the full 2026 archive &rarr;</a>
  </p>

  {render_footer_html(is_final=True)}
</body></html>"""

        intro_plain = (f"{champ_name} are 2026 World Cup Champions - and that's a wrap on the fantasy tournament too."
                        if champ_name else "The tournament is complete.") + " Thanks for playing!"
        plain = f"""TOURNAMENT COMPLETE.

Hi {name},

{intro_plain}

Your final standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts

Top 5: {' · '.join(f"{i+1}. {name_for(u)} ({int(u.get('totalPoints') or 0)})" for i, u in enumerate(leaderboard[:5]))}
{(chr(10) + flag_promo_plain + chr(10)) if flag_promo_plain else ''}{(chr(10) + flag_bracket_plain + chr(10)) if flag_bracket_plain else ''}
2026 archive: {ARCHIVE_URL}

You're getting this because you played Fantasy World Cup 2026 - thanks for playing!
Manage email preferences: {PROFILE_URL}
"""
        return subject, html, plain

    budget = budget_remaining(user)
    subject = f"Fantasy WC · {round_name} complete · transfer window OPEN"
    if budget > 0:
        subject += f" · ${budget} unspent!"

    odds_keys_block = render_odds_keys_html(proj, teams_cache or {}, players_cache or {})
    # A round recap fires when the window opens, so the CTA renders OPEN; fall
    # back to a synthetic window-open state if we couldn't read config.
    gs_for_cta = game_state or {"state": "window-open", "round": user.get("_round")}
    cta_block = render_transfer_cta_html(gs_for_cta, budget)

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width:600px; margin:0 auto; padding:24px; color:#1a1a1a">
  <h1 style="color:#1a6b8a; font-size:22px; letter-spacing:1px; margin:0 0 4px">{escape_html(round_name)} COMPLETE</h1>
  <p style="color:#888; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin:0 0 24px">{datetime.utcnow().strftime("%A, %B %d")}</p>

  <p style="font-size:15px; margin:0 0 16px">Hi {flag}{(" " if flag else "")}{escape_html(name)},</p>

  <p style="font-size:15px; margin:0 0 16px"><strong>{escape_html(round_name)} is in the books.</strong> Eliminated picks have been auto-sold from your roster and the next transfer window is now <strong style="color:#ff6eb4">OPEN</strong>.</p>

  <div style="background:#f8f8f6; border-radius:6px; padding:16px; margin-bottom:16px">
    <div style="font-size:11px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px">Your standing</div>
    <div style="font-size:32px; font-weight:800; color:#ff6eb4">
      {'#' + str(rank) if rank else 'unranked'}
      <span style="font-size:18px; color:#666; font-weight:600">· {pts} pts</span>
    </div>
  </div>
  {flag_promo_html}
  {odds_keys_block}
  {cta_block}
  {roster_block}
  {leaderboards_block}

  <p style="margin-top:32px">
    <a href="{LEADERBOARD_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">Full leaderboard →</a>
  </p>

  {render_footer_html()}
</body></html>"""

    odds_keys_plain = _odds_keys_plain(proj, teams_cache or {}, players_cache or {})
    cta_plain = _transfer_cta_plain(gs_for_cta, budget)
    plain = f"""{round_name} complete.

Hi {name},

{round_name} is in the books. Eliminated picks have been auto-sold. The next transfer window is OPEN.

Your standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts
{(flag_promo_plain + chr(10)) if flag_promo_plain else ''}{(odds_keys_plain + chr(10)) if odds_keys_plain else ''}{(cta_plain + chr(10)) if cta_plain else ''}
Top 5: {' · '.join(f"{i+1}. {name_for(u)} ({int(u.get('totalPoints') or 0)})" for i, u in enumerate(leaderboard[:5]))}

Transfer page: {TRANSFER_URL}
Leaderboard:   {LEADERBOARD_URL}
Manage emails: {PROFILE_URL}
"""
    return subject, html, plain


def escape_html(s: str) -> str:
    return (str(s)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;"))


def send_via_resend(api_key: str, to_email: str, subject: str, html: str, plain: str) -> bool:
    payload = json.dumps({
        "from":    SENDER,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
        "text":    plain,
    }).encode("utf-8")
    req = urllib.request.Request(
        RESEND_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            # Resend sits behind Cloudflare which blocks Python's default
            # 'Python-urllib/X.Y' UA with error 1010. Pretend to be a normal
            # HTTP client.
            "User-Agent":    "fantasy-world-cup/1.0 (https://github.com/fakeronjan/fantasy-world-cup)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
            return True
    except urllib.error.HTTPError as e:
        print(f"    Resend error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        return False
    except Exception as e:
        print(f"    Send failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "round"], required=True)
    ap.add_argument("--round-name", default="Round of 32",
                    help="Required for --mode round; the round that just completed")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render emails to stdout without sending")
    ap.add_argument("--only", default="",
                    help="Send only to this email address (test mode; bypasses "
                         "the opt-in check). Empty = normal send to all opted-in.")
    args = ap.parse_args()

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not args.dry_run and not api_key:
        sys.exit("RESEND_API_KEY env var required (or use --dry-run)")

    db = firestore_client()
    # The digest sends in the morning ET, so it recaps the PREVIOUS Hawaii
    # matchday (yesterday's games), consistent with the rest of the app's dating.
    now_hawaii = datetime.now(timezone.utc).astimezone(HAWAII_TZ)
    recap_iso = (now_hawaii - timedelta(days=1)).strftime("%Y-%m-%d")

    # Load all users sorted by global points. Group-specific leaderboards
    # are filtered out of this same list per-user inside render_leaderboards_html.
    all_users = []
    for udoc in db.collection("users").stream():
        u = {"uid": udoc.id, **(udoc.to_dict() or {})}
        all_users.append(u)
    all_users.sort(key=lambda u: -(u.get("totalPoints") or 0))

    # Catalogs: teams + players, keyed by id. Used to render rosters with
    # full names + emojis, and to attribute today's per-match scoring.
    teams_cache = {}
    for tdoc in db.collection("teams").stream():
        teams_cache[tdoc.id] = {"id": tdoc.id, **(tdoc.to_dict() or {})}
    players_cache = {}
    for pdoc in db.collection("players").stream():
        players_cache[pdoc.id] = {"id": pdoc.id, **(pdoc.to_dict() or {})}

    today_matches = []
    if args.mode == "daily":
        today_matches = load_recap_matches(db, recap_iso)

    # Transfer-window state (mirrors the site) + per-user title odds / keys.
    game_state = load_game_state(db)
    flag_state = load_flag_state(db)                       # Flag Knockout promo (shared across users)
    flag_promo_html = render_flag_promo_html(flag_state)
    flag_promo_plain = _flag_promo_plain(flag_state)
    if flag_promo_html:
        print("Flag Knockout promo: ON (contest launched)")

    # The Final recap embeds the actual Flag Knockout RESULTS inline (champion
    # + final matchup tally), not just the teaser link every other email uses -
    # falls back to the teaser if the flag contest isn't done yet. It also adds
    # a Final Four bracket summary (with real flag images) further down.
    flag_bracket_html = flag_bracket_plain = ""
    if args.mode == "round" and args.round_name == "Final":
        flag_promo_html = render_flag_results_html(flag_state) or flag_promo_html
        flag_promo_plain = _flag_results_plain(flag_state) or flag_promo_plain
        flag_bracket_html = render_flag_bracket_html(flag_state)
        flag_bracket_plain = _flag_bracket_plain(flag_state)
    proj_by_uid = load_projections()
    print(f"Game state: {game_state.get('state')} ({game_state.get('round')}); "
          f"projections for {len(proj_by_uid)} users")

    only = (args.only or "").strip().lower()
    if only:
        print(f"TEST MODE: sending only to {only} (opt-in check bypassed)")

    # The Final recap is the one-time closing summary - everyone who played
    # should see it, not just users who opted into the routine daily digest.
    # Mid-tournament round-recaps (R32/R16/QF/SF) stay opt-in-gated.
    bypass_optin = (args.mode == "round" and args.round_name == "Final")
    if bypass_optin:
        print("Final recap: opt-in check bypassed - sending to every user with an email on file")

    n_sent = n_skipped = n_failed = 0
    for u in all_users:
        if only:
            # Explicit test target: match by email, ignore the opt-in toggle.
            if (u.get("email") or "").strip().lower() != only:
                n_skipped += 1
                continue
        else:
            if not bypass_optin and not u.get("emailNotificationsEnabled"):
                n_skipped += 1
                continue
            if not u.get("email"):
                n_skipped += 1
                continue

        roster = u.get("roster") or []
        proj = proj_by_uid.get(u["uid"])
        if args.mode == "daily":
            subject, html, plain = render_daily_html(
                u, all_users, today_matches,
                roster, teams_cache, players_cache,
                proj=proj, game_state=game_state,
                flag_promo_html=flag_promo_html, flag_promo_plain=flag_promo_plain,
            )
        else:
            subject, html, plain = render_round_recap_html(
                u, all_users, args.round_name,
                roster=roster, teams_cache=teams_cache, players_cache=players_cache,
                proj=proj, game_state=game_state,
                flag_promo_html=flag_promo_html, flag_promo_plain=flag_promo_plain,
                flag_bracket_html=flag_bracket_html, flag_bracket_plain=flag_bracket_plain,
            )

        if args.dry_run:
            print(f"\n--- to: {u['email']} ---")
            print(f"Subject: {subject}")
            print(plain)
        else:
            ok = send_via_resend(api_key, u["email"], subject, html, plain)
            if ok:
                n_sent += 1
                print(f"  sent → {u['email']}")
            else:
                n_failed += 1

    print(f"\nSent: {n_sent}  Skipped (not opted in or no email): {n_skipped}  Failed: {n_failed}")


if __name__ == "__main__":
    main()
