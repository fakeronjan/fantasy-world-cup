"""Pull World Cup team and player stats from Wikipedia.

Wikipedia tables are open and parseable via pandas.read_html (which uses
lxml under the hood). We send a real User-Agent so Wikipedia doesn't block us.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

UA = "FakeRonjanFantasyWC/0.1 (rjsikdar@gmail.com) personal project"


def fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def read_tables(url: str) -> list[pd.DataFrame]:
    html = fetch(url)
    return pd.read_html(html)


def get_group_standings(year: int) -> pd.DataFrame:
    """Pull all 8 group standings tables for a given WC year."""
    rows = []
    for letter in "ABCDEFGH":
        url = f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup_Group_{letter}"
        try:
            tables = read_tables(url)
        except Exception as e:
            print(f"  ! Group {letter}: {e}", file=sys.stderr)
            continue
        # Find the standings table — typically the one with columns
        # like Pld, W, D, L, GF, GA, GD, Pts and 4 rows.
        chosen = None
        for t in tables:
            cols = [str(c).strip() for c in t.columns]
            if any("Pld" in c for c in cols) and any("Pts" in c for c in cols) and len(t) >= 4:
                chosen = t.head(4).copy()
                chosen.columns = [str(c).strip() for c in chosen.columns]
                break
        if chosen is None:
            print(f"  ! Group {letter}: no standings table found", file=sys.stderr)
            continue
        chosen["group"] = letter
        rows.append(chosen)
        print(f"  ok Group {letter}: {len(chosen)} teams")
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def get_goalscorers(year: int) -> list[dict]:
    """Try the dedicated goalscorers page; fall back to main article."""
    candidates = [
        f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup_goalscorers",
        f"https://en.wikipedia.org/wiki/List_of_{year}_FIFA_World_Cup_goalscorers",
        f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup_statistics",
        f"https://en.wikipedia.org/wiki/{year}_FIFA_World_Cup",
    ]
    for url in candidates:
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  ! {url}: {e}", file=sys.stderr)
            continue
        # Look for a list like "8 goals\n* Kylian Mbappé\n7 goals\n* Lionel Messi..."
        # Wikipedia renders these as nested lists.
        scorers = parse_goalscorers_html(html)
        if scorers:
            print(f"  ok goalscorers from {url}: {len(scorers)} entries")
            return scorers
        # try next candidate
    return []


def parse_goalscorers_html(html: str) -> list[dict]:
    """Parse a Wikipedia 'Goalscorers' section, which uses headers like
    '7 goals' followed by a bulleted list of player names."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Find the "Goalscorers" section. It's usually under a <h2> or <h3>
    # containing 'Goalscorers' or 'Top goalscorers'.
    heading = None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True).lower()
        if "goalscorer" in text:
            heading = tag
            break
    if not heading:
        return []

    out: list[dict] = []
    current_goals = None
    # Walk forward through siblings until we hit the next h2 (new section)
    for sib in heading.find_all_next():
        if sib.name == "h2" and sib is not heading:
            break
        text = sib.get_text(" ", strip=True)
        # Match "N goals" or "N goal"
        m = re.match(r"^(\d+)\s+goals?$", text)
        if m and sib.name in ("p", "h3", "h4", "div", "dt", "b"):
            current_goals = int(m.group(1))
            continue
        if sib.name == "li" and current_goals is not None:
            # The <li> usually contains the player name. Following text or
            # icons may indicate the team via a flag image.
            player = sib.get_text(" ", strip=True)
            # Trim trailing "(pen.)" notes and so on
            player = re.sub(r"\s*\(.*?\)\s*$", "", player).strip()
            if not player:
                continue
            out.append({"player": player, "goals": current_goals})
    return out


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    for year in (2022, 2018, 2014, 2010):
        print(f"\n=== {year} World Cup ===")

        print("Group standings...")
        gs = get_group_standings(year)
        if not gs.empty:
            path = out_dir / f"wc{year}_group_standings.csv"
            gs.to_csv(path, index=False)
            print(f"  saved → {path}  ({len(gs)} rows)")

        print("Goalscorers...")
        scorers = get_goalscorers(year)
        if scorers:
            path = out_dir / f"wc{year}_goalscorers.json"
            path.write_text(json.dumps(scorers, ensure_ascii=False, indent=2))
            print(f"  saved → {path}  ({len(scorers)} players)")


if __name__ == "__main__":
    main()
