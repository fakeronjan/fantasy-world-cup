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
# For v1, send from Resend's default sandbox sender. To use a custom
# domain, change this to e.g. "Fantasy WC <noreply@your-domain.com>"
# after adding + verifying the domain in the Resend dashboard.
SENDER = "Fantasy WC <onboarding@resend.dev>"

SITE_URL = "https://fakeronjan.github.io/fantasy-world-cup/"
PROFILE_URL = SITE_URL + "profile.html"
LEADERBOARD_URL = SITE_URL + "leaderboard.html"


def name_for(u: dict) -> str:
    return (u.get("leagueNickname") or u.get("displayName") or u.get("email") or "Player").strip()


def flag_for(u: dict) -> str:
    return (u.get("countryFlag") or "").strip()


def load_today_match_summary(db, today_iso: str) -> list[dict]:
    """Return any matches that finished today (UTC) with a brief summary."""
    finished = []
    for m in db.collection("matches").stream():
        d = m.to_dict() or {}
        if d.get("status") != "FINISHED":
            continue
        utc = d.get("utcDate", "")
        if utc[:10] != today_iso:
            continue
        finished.append({
            "home":  d.get("team1Name") or "?",
            "away":  d.get("team2Name") or "?",
            "score": f"{d.get('score1', '?')}-{d.get('score2', '?')}",
            "round": d.get("round", "?"),
            "goals": d.get("goals") or [],
        })
    finished.sort(key=lambda m: m.get("round", ""))
    return finished


def render_daily_html(user: dict, leaderboard: list[dict], today_matches: list[dict],
                      yesterday_points: int | None) -> tuple[str, str, str]:
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

    # Top 5 of (their visible) leaderboard
    top5 = leaderboard[:5]
    top5_html = "\n".join(
        f'<tr><td style="padding:4px 8px; color:#666">{i+1}</td>'
        f'<td style="padding:4px 8px">{(flag_for(u) + " ") if flag_for(u) else ""}{escape_html(name_for(u))}'
        + (' <strong style="color:#ff6eb4">(you)</strong>' if u["uid"] == user["uid"] else '')
        + f'</td>'
        f'<td style="padding:4px 8px; text-align:right; font-weight:700">{int(u.get("totalPoints") or 0)}</td></tr>'
        for i, u in enumerate(top5)
    )

    # Today's matches block (only if any)
    if today_matches:
        matches_html = '<h3 style="margin:24px 0 8px; font-size:14px; color:#1a6b8a">Today\'s matches</h3>'
        matches_html += '<ul style="padding-left:18px; margin:0">'
        for m in today_matches:
            matches_html += f'<li style="margin:4px 0; font-size:13px"><strong>{escape_html(m["round"])}</strong> · {escape_html(m["home"])} {m["score"]} {escape_html(m["away"])}</li>'
        matches_html += '</ul>'
    else:
        matches_html = '<p style="color:#888; font-size:13px; font-style:italic">No matches today.</p>'

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width:560px; margin:0 auto; padding:24px; color:#1a1a1a">
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

  {matches_html}

  <h3 style="margin:24px 0 8px; font-size:14px; color:#1a6b8a">League leaderboard (top 5)</h3>
  <table style="width:100%; border-collapse:collapse; font-size:13px">
    {top5_html}
  </table>

  <p style="margin-top:32px">
    <a href="{LEADERBOARD_URL}" style="background:#1a6b8a; color:#fff; padding:10px 18px; border-radius:4px; text-decoration:none; font-weight:600; font-size:13px">View full leaderboard →</a>
  </p>

  <hr style="margin:32px 0; border:none; border-top:1px solid #ddd">
  <p style="font-size:11px; color:#888">
    You're getting this because you opted into Fantasy WC emails on your profile.
    <a href="{PROFILE_URL}" style="color:#1a6b8a">Manage email preferences</a>.
  </p>
</body></html>"""

    plain = f"""Fantasy World Cup · {datetime.utcnow().strftime("%A, %B %d")}

Hi {name},

Your standing: {'#' + str(rank) if rank else 'unranked'} · {pts} pts{delta_str}

{('Today: ' + ', '.join(f"{m['round']} {m['home']} {m['score']} {m['away']}" for m in today_matches)) if today_matches else 'No matches today.'}

Top 5: {' · '.join(f"{i+1}. {name_for(u)} ({int(u.get('totalPoints') or 0)})" for i, u in enumerate(top5))}

Leaderboard: {LEADERBOARD_URL}
Manage emails: {PROFILE_URL}
"""
    return subject, html, plain


def render_round_recap_html(user: dict, leaderboard: list[dict], round_name: str) -> tuple[str, str, str]:
    """Round-end recap email. Lighter content than daily; emphasis on the
    completed round + the freshly-opened transfer window."""
    name = name_for(user)
    flag = flag_for(user)
    pts  = int(user.get("totalPoints") or 0)
    rank = next((i + 1 for i, u in enumerate(leaderboard) if u["uid"] == user["uid"]), None)

    subject = f"Fantasy WC · {round_name} complete · transfer window OPEN"

    top5_html = "\n".join(
        f'<tr><td style="padding:4px 8px; color:#666">{i+1}</td>'
        f'<td style="padding:4px 8px">{(flag_for(u) + " ") if flag_for(u) else ""}{escape_html(name_for(u))}'
        + (' <strong style="color:#ff6eb4">(you)</strong>' if u["uid"] == user["uid"] else '')
        + f'</td>'
        f'<td style="padding:4px 8px; text-align:right; font-weight:700">{int(u.get("totalPoints") or 0)}</td></tr>'
        for i, u in enumerate(leaderboard[:5])
    )

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width:560px; margin:0 auto; padding:24px; color:#1a1a1a">
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

  <h3 style="margin:24px 0 8px; font-size:14px; color:#1a6b8a">League leaderboard (top 5)</h3>
  <table style="width:100%; border-collapse:collapse; font-size:13px">
    {top5_html}
  </table>

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

    # Load all users sorted by points (this is the "leaderboard" — group
    # filtering is client-side only today; everyone sees the global rank
    # in the email for v1).
    all_users = []
    for udoc in db.collection("users").stream():
        u = {"uid": udoc.id, **(udoc.to_dict() or {})}
        all_users.append(u)
    all_users.sort(key=lambda u: -(u.get("totalPoints") or 0))

    today_matches = []
    if args.mode == "daily":
        today_matches = load_today_match_summary(db, today_iso)

    n_sent = n_skipped = n_failed = 0
    for u in all_users:
        if not u.get("emailNotificationsEnabled"):
            n_skipped += 1
            continue
        if not u.get("email"):
            n_skipped += 1
            continue

        yesterday_pts = (u.get("pointsByDate") or {}).get(yesterday_iso)
        if args.mode == "daily":
            subject, html, plain = render_daily_html(u, all_users, today_matches, yesterday_pts)
        else:
            subject, html, plain = render_round_recap_html(u, all_users, args.round_name)

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
