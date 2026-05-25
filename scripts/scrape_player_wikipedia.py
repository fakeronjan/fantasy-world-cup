"""Scrape Wikipedia for per-player FIFA World Cup history.

For each player in docs/data/seed_players.json:
  1. Search Wikipedia for the most relevant page (player name + nationality)
  2. Fetch the article HTML
  3. Extract:
       - WC participation years (from intro + career-stats text)
       - Titles + runner-up appearances (from Honours section)
  4. Save to docs/data/player_history_scraped.json

This is BEST-EFFORT. Wikipedia infobox structures vary, players have
disambiguation issues, and not every player has a page. After running,
review the output and hand-fix the top 50.

Usage:
  ./venv/bin/python scripts/scrape_player_wikipedia.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SEED_PLAYERS = ROOT / "docs" / "data" / "seed_players.json"
SEED_TEAMS = ROOT / "docs" / "data" / "seed_teams.json"
OUT_PATH = ROOT / "docs" / "data" / "player_history_scraped.json"

UA = "FakeRonjanFantasyWC/1.0 (rjsikdar@gmail.com) personal project"
SLEEP_BETWEEN = 0.6  # seconds between requests - be polite to Wikipedia


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def find_wikipedia_url(player_name: str, team_country: str) -> str | None:
    """Use Wikipedia OpenSearch API to find the best match for this player.

    Tries progressively wider queries. For each result set, scores titles
    by relevance - prefers "footballer" in title, matches the player's
    country, skips disambiguation pages.
    """
    queries = [
        f"{player_name} ({team_country.lower()} footballer)",
        f"{player_name} footballer",
        player_name,
    ]
    country_lower = team_country.lower()
    seen_urls = set()

    for q in queries:
        url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=opensearch&search={quote(q)}&limit=8&format=json"
        )
        try:
            data = json.loads(_fetch(url))
        except Exception:
            continue
        candidates = data[1] if len(data) > 1 else []
        urls = data[3] if len(data) > 3 else []
        if not candidates:
            continue

        # Score each candidate. Higher = better.
        scored = []
        for title, page_url in zip(candidates, urls):
            if page_url in seen_urls:
                continue
            seen_urls.add(page_url)
            tl = title.lower()
            if "disambiguation" in tl:
                continue
            score = 0
            if "footballer" in tl: score += 5
            if country_lower in tl: score += 3
            if title.lower() == player_name.lower(): score += 2
            if title.lower().startswith(player_name.lower()): score += 1
            scored.append((score, title, page_url))

        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        return scored[0][2]

    return None


WC_TITLE_RE   = re.compile(r"(?:FIFA\s+World\s+Cup|World\s+Cup)[^.]{0,80}?(?:Champion|winner|gold|title)", re.I)
WC_YEAR_RE    = re.compile(r"(19[3-9]\d|20[0-2]\d)\s*(?:FIFA\s+)?World\s+Cup", re.I)
RUNNERUP_RE   = re.compile(r"(?:FIFA\s+World\s+Cup|World\s+Cup)[^.]{0,80}?(?:Runner[s]?[- ]up|silver|second)", re.I)
WC_COUNT_RE   = re.compile(r"(?:played|appeared|featured)\s+(?:in|at)\s+(?:his\s+)?(\w+)\s+(?:FIFA\s+)?World\s+Cup", re.I)
WC_GOALS_RE   = re.compile(r"(?:scoring|scored)\s+(\d+)\s+(?:goals?\s+)?(?:at|in)\s+(?:the\s+)?(?:FIFA\s+)?World\s+Cup", re.I)


WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
}


def extract_history(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Get intro text (first few paragraphs)
    intro_text = ""
    for p in soup.select("div.mw-parser-output > p")[:8]:
        intro_text += " " + p.get_text(" ", strip=True)

    # Extract Honours section
    honours_text = ""
    honours_h = None
    for tag in soup.find_all(["h2", "h3"]):
        t = tag.get_text(" ", strip=True).lower()
        if "honour" in t or "awards" in t or "honors" in t:
            honours_h = tag
            break
    if honours_h:
        for sib in honours_h.find_all_next():
            if sib.name == "h2" and sib is not honours_h:
                break
            honours_text += " " + sib.get_text(" ", strip=True)

    out = {
        "wcsPlayed": 0,
        "wcYears": [],
        "goals": None,
        "titles": 0,
        "titleYears": [],
        "runnerUps": 0,
        "runnerUpYears": [],
        "tag": None,
        "sourceUrl": None,
    }

    # WC years mentioned anywhere in intro
    years = set()
    for m in WC_YEAR_RE.finditer(intro_text):
        y = int(m.group(1))
        if 1930 <= y <= 2026:
            years.add(y)
    out["wcYears"] = sorted(years)
    out["wcsPlayed"] = len(out["wcYears"])

    # Titles - find "(year) FIFA World Cup" near a champion/winner mention
    title_years = set()
    for sentence in re.split(r"[.\n]", honours_text + " " + intro_text):
        if WC_TITLE_RE.search(sentence):
            for m in WC_YEAR_RE.finditer(sentence):
                title_years.add(int(m.group(1)))
    out["titleYears"] = sorted(title_years)
    out["titles"] = len(out["titleYears"])

    # Runner-ups
    runnerup_years = set()
    for sentence in re.split(r"[.\n]", honours_text + " " + intro_text):
        if RUNNERUP_RE.search(sentence):
            for m in WC_YEAR_RE.finditer(sentence):
                runnerup_years.add(int(m.group(1)))
    out["runnerUpYears"] = sorted(runnerup_years)
    out["runnerUps"] = len(out["runnerUpYears"])

    # Goals at World Cups (any sentence)
    goal_total = 0
    for m in WC_GOALS_RE.finditer(intro_text):
        try:
            goal_total = max(goal_total, int(m.group(1)))
        except ValueError:
            pass
    if goal_total > 0:
        out["goals"] = goal_total

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N players (for testing)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip players already in the output file")
    args = parser.parse_args()

    players = json.loads(SEED_PLAYERS.read_text())

    out: dict[str, dict] = {}
    if args.resume and OUT_PATH.exists():
        out = json.loads(OUT_PATH.read_text())
        print(f"Resuming - {len(out)} players already scraped")

    total = len(players) if args.limit is None else min(args.limit, len(players))
    for i, p in enumerate(players[:total], 1):
        key = p["id"]
        if key in out:
            continue
        try:
            url = find_wikipedia_url(p["name"], p["teamName"])
            if not url:
                out[key] = {"sourceUrl": None, "notFound": True}
                print(f"  [{i}/{total}] {p['name']:<30} ({p['teamName']:<20}) - no page")
            else:
                time.sleep(SLEEP_BETWEEN)
                html = _fetch(url)
                hist = extract_history(html)
                hist["sourceUrl"] = url
                out[key] = hist
                summary = f"{hist['wcsPlayed']} WCs"
                if hist["titles"]:    summary += f", {hist['titles']} title(s)"
                if hist["runnerUps"]: summary += f", {hist['runnerUps']} RU"
                if hist["goals"]:     summary += f", {hist['goals']} goals"
                print(f"  [{i}/{total}] {p['name']:<30} ({p['teamName']:<20}) - {summary}")
        except Exception as e:
            out[key] = {"sourceUrl": None, "error": str(e)}
            print(f"  [{i}/{total}] {p['name']:<30} ({p['teamName']:<20}) - ERROR: {e}")

        time.sleep(SLEEP_BETWEEN)

        # Save every 25 players in case of interruption
        if i % 25 == 0:
            OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nDone. {len(out)} entries written to {OUT_PATH}")


if __name__ == "__main__":
    main()
