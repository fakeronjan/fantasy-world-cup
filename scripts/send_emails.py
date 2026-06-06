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


def name_for(u: dict) -> str:
    return (u.get("leagueNickname") or u.get("displayName") or u.get("email") or "Player").strip()


def flag_for(u: dict) -> str:
    return (u.get("countryFlag") or "").strip()


def load_today_matches(db, today_iso: str) -> list[dict]:
    """Return finished matches from today with full detail (raw match docs)."""
    finished = []
    for m in db.collection("matches").stream():
        d = m.to_dict() or {}
        if d.get("status") != "FINISHED":
            continue
        utc = d.get("utcDate", "")
        if utc[:10] != today_iso:
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
    return (f'<h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Your picks in today\'s action</h3>'
            f'<table style="width:100%; border-collapse:collapse; font-size:13px">{rows}</table>')


def render_today_matches_html(today_matches: list[dict], players_cache: dict) -> str:
    if not today_matches:
        return '<p style="color:#888; font-size:13px; font-style:italic; margin-top:16px">No matches today.</p>'
    items = ""
    for m in today_matches:
        line = f"{escape_html(m.get('team1Name') or '?')} {m.get('score1', '?')}-{m.get('score2', '?')} {escape_html(m.get('team2Name') or '?')}"
        goal_summary = goals_summary_text(m, players_cache)
        goal_html = f'<br><span style="color:#666; font-size:11px">{escape_html(goal_summary)}</span>' if goal_summary else ""
        items += f'<li style="margin:6px 0; font-size:13px"><strong>{escape_html(m.get("round") or "?")}</strong> · {line}{goal_html}</li>'
    return f'<h3 style="margin:24px 0 6px; font-size:14px; color:#1a6b8a">Today\'s matches</h3><ul style="padding-left:18px; margin:0">{items}</ul>'


def render_daily_html(user: dict, leaderboard: list[dict], today_matches: list[dict],
                      yesterday_points: int | None, roster: list[dict],
                      teams_cache: dict, players_cache: dict) -> tuple[str, str, str]:
    """Returns (subject, html_body, plain_text_body)."""
    name  = name_for(user)
    flag  = flag_for(user)
    pts   = int(user.get("totalPoints") or 0)
    delta = (pts - yesterday_points) if yesterday_points is not None else None
    rank  = next((i + 1 for i, u in enumerate(leaderboard) if u["uid"] == user["uid"]), None)

    delta_str = ""
    if delta is not None and delta != 0:
        delta_str = f" ({'+' if delta > 0 else ''}{delta})"

    subject_parts = ["Fantasy WC", datetime.utcnow().strftime("%b %d")]
    if rank: subject_parts.append(f"Rank #{rank}{delta_str}")
    subject = " · ".join(subject_parts)

    matches_block      = render_today_matches_html(today_matches, players_cache)
    picks_today_block  = render_picks_today_html(roster, today_matches, teams_cache, players_cache)
    roster_block       = render_roster_html(roster, teams_cache, players_cache)
    leaderboards_block = render_leaderboards_html(user, leaderboard)

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

  {matches_block}
  {picks_today_block}
  {roster_block}
  {leaderboards_block}

  <p style="margin-top:32px">
    <a href="{LEADERBOARD_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">View full leaderboard →</a>
  </p>

  <hr style="margin:32px 0; border:none; border-top:1px solid #ddd">
  <p style="font-size:11px; color:#888">
    You're getting this because you opted into Fantasy WC emails on your profile.
    <a href="{PROFILE_URL}" style="color:#1a6b8a">Manage email preferences</a>.
  </p>
</body></html>"""

    # Plain-text fallback (kept tight)
    roster_plain = "\n".join(
        f"  - {name_for_pick(p, teams_cache, players_cache)[0]} (paid ${int(p.get('purchasePrice') or 0)})"
        for p in roster
    )
    plain = f"""Fantasy World Cup · {datetime.utcnow().strftime("%A, %B %d")}

Hi {name},

Your standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts{delta_str}

Today: {(', '.join(f"{m['round']} {m['team1Name']} {m.get('score1','?')}-{m.get('score2','?')} {m['team2Name']}" for m in today_matches)) if today_matches else 'no matches'}

Your roster ({len(roster)} picks):
{roster_plain}

Leaderboard: {LEADERBOARD_URL}
Manage emails: {PROFILE_URL}
"""
    return subject, html, plain


def render_round_recap_html(user: dict, leaderboard: list[dict], round_name: str,
                              roster: list[dict] = None,
                              teams_cache: dict = None, players_cache: dict = None) -> tuple[str, str, str]:
    """Round-end recap email. Lighter content than daily; emphasis on the
    completed round + the freshly-opened transfer window."""
    name = name_for(user)
    flag = flag_for(user)
    pts  = int(user.get("totalPoints") or 0)
    rank = next((i + 1 for i, u in enumerate(leaderboard) if u["uid"] == user["uid"]), None)

    subject = f"Fantasy WC · {round_name} complete · transfer window OPEN"

    leaderboards_block = render_leaderboards_html(user, leaderboard)
    roster_block = render_roster_html(roster or [], teams_cache or {}, players_cache or {}) if roster is not None else ""

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

  {roster_block}
  {leaderboards_block}

  <p style="margin-top:32px">
    <a href="{SITE_URL}transfer.html" style="background:#ff6eb4; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">Make transfers →</a>
    &nbsp;
    <a href="{LEADERBOARD_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">Full leaderboard</a>
  </p>

  <hr style="margin:32px 0; border:none; border-top:1px solid #ddd">
  <p style="font-size:11px; color:#888">
    You're getting this because you opted into Fantasy WC emails on your profile.
    <a href="{PROFILE_URL}" style="color:#1a6b8a">Manage email preferences</a>.
  </p>
</body></html>"""

    plain = f"""{round_name} complete.

Hi {name},

{round_name} is in the books. Eliminated picks have been auto-sold. The next transfer window is OPEN.

Your standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts

Top 5: {' · '.join(f"{i+1}. {name_for(u)} ({int(u.get('totalPoints') or 0)})" for i, u in enumerate(leaderboard[:5]))}

Transfer page: {SITE_URL}transfer.html
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
    args = ap.parse_args()

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not args.dry_run and not api_key:
        sys.exit("RESEND_API_KEY env var required (or use --dry-run)")

    db = firestore_client()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

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
        today_matches = load_today_matches(db, today_iso)

    n_sent = n_skipped = n_failed = 0
    for u in all_users:
        if not u.get("emailNotificationsEnabled"):
            n_skipped += 1
            continue
        if not u.get("email"):
            n_skipped += 1
            continue

        yesterday_pts = (u.get("pointsByDate") or {}).get(yesterday_iso)
        roster = u.get("roster") or []
        if args.mode == "daily":
            subject, html, plain = render_daily_html(
                u, all_users, today_matches, yesterday_pts,
                roster, teams_cache, players_cache,
            )
        else:
            subject, html, plain = render_round_recap_html(
                u, all_users, args.round_name,
                roster=roster, teams_cache=teams_cache, players_cache=players_cache,
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
