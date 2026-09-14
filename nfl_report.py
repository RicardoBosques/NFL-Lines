"""
nfl_report.py

Weekly NFL matchup + odds report. Same overall pattern as the MLB pitcher/odds
script: free/keyless stats API + The Odds API for lines, self-tracked
"opening" odds (committed back to the repo by the workflow), full report by
email, short "ready" ping via ntfy.sh.

Data sources
------------
- ESPN's public (undocumented) API — site.api.espn.com/apis/site/v2/sports/football/nfl
  Free, no key, no published rate limit, but unofficial: ESPN can change or
  break it without notice. Used for: week's scoreboard/schedule, team
  records (overall + home/away + last 5), and team-level offense/defense
  ranks. There is no clean "probable starter" concept in football the way
  MLB has starting pitchers, so this starts at the team-stat level (points
  for/against per game, yards per game, turnover margin) rather than
  QB-level detail — same "add stats in stages" approach used for MLB.
- The Odds API — api.the-odds-api.com, sport key `americanfootball_nfl`.
  Same free tier (500 requests/month) and same opening-odds self-tracking
  pattern as the MLB version, since the free tier has no historical odds.

Secrets expected (GitHub repo Settings -> Secrets and variables -> Actions):
  ODDS_API_KEY
  GMAIL_ADDRESS
  GMAIL_APP_PASSWORD
  NTFY_TOPIC        (kept as a secret rather than hardcoded, unlike the MLB
                      version, so the topic name isn't sitting in the repo)

Files this script reads/writes:
  opening_odds.json  — auto-committed by the workflow each run, same as MLB
"""

import json
import os
import smtplib
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
OPENING_ODDS_FILE = "opening_odds.json"

REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def get_json(url, params=None, retries=2):
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nfl-report-script/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"WARNING: request failed after retries: {url} ({last_err})")
    return None


# ---------------------------------------------------------------------------
# ESPN: schedule / scoreboard for the current week
# ---------------------------------------------------------------------------

def get_week_scoreboard():
    """Returns the raw ESPN scoreboard payload for the current NFL week."""
    data = get_json(f"{ESPN_BASE}/scoreboard")
    return data


def parse_games(scoreboard):
    """Pulls out the list of this week's games with team names/ids/records."""
    games = []
    if not scoreboard:
        return games
    for event in scoreboard.get("events", []):
        try:
            comp = event["competitions"][0]
            competitors = comp["competitors"]
            home = next(c for c in competitors if c["homeAway"] == "home")
            away = next(c for c in competitors if c["homeAway"] == "away")
            games.append({
                "id": event["id"],
                "name": event.get("shortName", event.get("name", "")),
                "date": event.get("date", ""),
                "home_team": home["team"]["displayName"],
                "home_abbr": home["team"]["abbreviation"],
                "home_id": home["team"]["id"],
                "home_record": home.get("records", [{}])[0].get("summary", "N/A") if home.get("records") else "N/A",
                "away_team": away["team"]["displayName"],
                "away_abbr": away["team"]["abbreviation"],
                "away_id": away["team"]["id"],
                "away_record": away.get("records", [{}])[0].get("summary", "N/A") if away.get("records") else "N/A",
                "venue": comp.get("venue", {}).get("fullName", ""),
                "status": event.get("status", {}).get("type", {}).get("shortDetail", ""),
            })
        except (KeyError, StopIteration, IndexError):
            continue
    return games


# ---------------------------------------------------------------------------
# ESPN: team-level stats (points/yards per game, turnover margin, splits)
# ---------------------------------------------------------------------------

_team_stats_cache = {}


def get_team_stats(team_id):
    """
    Pulls team season stats. Cached per run since a team shows up once as
    home and once as away lookups aren't needed twice.
    """
    if team_id in _team_stats_cache:
        return _team_stats_cache[team_id]

    data = get_json(f"{ESPN_BASE}/teams/{team_id}/statistics")
    stats = {"pts_for": "N/A", "pts_against": "N/A", "yards_per_game": "N/A", "turnover_margin": "N/A"}

    if data:
        try:
            categories = data.get("results", {}).get("stats", {}).get("categories", [])
            flat = {}
            for cat in categories:
                for stat in cat.get("stats", []):
                    flat[stat.get("name")] = stat.get("displayValue")
            stats["pts_for"] = flat.get("totalPointsPerGame", "N/A")
            stats["yards_per_game"] = flat.get("yardsPerGame", "N/A")
            stats["turnover_margin"] = flat.get("turnOverDifferential", "N/A")
        except (KeyError, AttributeError):
            pass

    _team_stats_cache[team_id] = stats
    return stats


def get_team_record_splits(team_id):
    """Home/away and last-5 splits from the team's record endpoint."""
    data = get_json(f"{ESPN_BASE}/teams/{team_id}")
    splits = {"home": "N/A", "away": "N/A", "last5": "N/A"}
    if not data:
        return splits
    try:
        items = data.get("team", {}).get("record", {}).get("items", [])
        for item in items:
            desc = item.get("description", item.get("type", ""))
            summary = item.get("summary", "N/A")
            if desc.lower() == "home":
                splits["home"] = summary
            elif desc.lower() == "road" or desc.lower() == "away":
                splits["away"] = summary
    except (KeyError, AttributeError):
        pass
    return splits


# ---------------------------------------------------------------------------
# ESPN: injuries (best-effort — coverage/format can vary week to week)
# ---------------------------------------------------------------------------

def get_team_injuries(team_id):
    data = get_json(f"{ESPN_BASE}/teams/{team_id}/injuries")
    injuries = []
    if not data:
        return injuries
    try:
        for item in data.get("injuries", []):
            for entry in item.get("injuries", []):
                athlete = entry.get("athlete", {}).get("displayName", "Unknown")
                status = entry.get("status", "Unknown")
                injuries.append(f"{athlete} ({status})")
    except (KeyError, AttributeError):
        pass
    return injuries[:8]  # cap so one banged-up roster doesn't blow out the report


# ---------------------------------------------------------------------------
# The Odds API: current lines + self-tracked opening lines
# ---------------------------------------------------------------------------

def get_current_odds():
    if not ODDS_API_KEY:
        print("WARNING: ODDS_API_KEY not set, skipping odds.")
        return []
    data = get_json(
        f"{ODDS_API_BASE}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
        },
    )
    return data or []


def load_opening_odds():
    if os.path.exists(OPENING_ODDS_FILE):
        with open(OPENING_ODDS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_opening_odds(opening):
    with open(OPENING_ODDS_FILE, "w") as f:
        json.dump(opening, f, indent=2, sort_keys=True)


def extract_line(game_odds, market_key):
    """Pulls a representative price from the first bookmaker that has it."""
    for bm in game_odds.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") == market_key:
                return market.get("outcomes", [])
    return None


def build_odds_lookup(odds_list, opening):
    """
    Returns {espn_matchup_key: {"current": {...}, "opening": {...}}}.
    Matches by team names since The Odds API and ESPN don't share IDs.
    Also updates `opening` in place with any newly-seen games.
    """
    lookup = {}
    for game in odds_list:
        key = f"{game.get('away_team')}@{game.get('home_team')}_{game.get('commence_time', '')[:10]}"
        current = {
            "moneyline": extract_line(game, "h2h"),
            "spread": extract_line(game, "spreads"),
            "total": extract_line(game, "totals"),
        }
        if key not in opening:
            opening[key] = current
        lookup[key] = {"current": current, "opening": opening[key]}
    return lookup


def format_outcomes(outcomes):
    if not outcomes:
        return "N/A"
    parts = []
    for o in outcomes:
        name = o.get("name", "")
        price = o.get("price", "")
        point = o.get("point")
        if point is not None:
            parts.append(f"{name} {point:+g} ({price:+d})" if isinstance(price, int) else f"{name} {point:+g} ({price})")
        else:
            parts.append(f"{name} ({price:+d})" if isinstance(price, int) else f"{name} ({price})")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def build_report():
    scoreboard = get_week_scoreboard()
    games = parse_games(scoreboard)
    odds_list = get_current_odds()
    opening = load_opening_odds()
    odds_lookup = build_odds_lookup(odds_list, opening)
    save_opening_odds(opening)

    week_label = ""
    if scoreboard:
        week_label = scoreboard.get("week", {}).get("text", "") or f"Week {scoreboard.get('week', {}).get('number', '?')}"

    lines = []
    lines.append(f"NFL WEEKLY REPORT — {week_label}")
    lines.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    if not games:
        lines.append("\nNo games found for the current week (bye week, or ESPN endpoint changed shape).")

    for g in games:
        home_stats = get_team_stats(g["home_id"])
        away_stats = get_team_stats(g["away_id"])
        home_splits = get_team_record_splits(g["home_id"])
        away_splits = get_team_record_splits(g["away_id"])
        home_inj = get_team_injuries(g["home_id"])
        away_inj = get_team_injuries(g["away_id"])

        odds_key_guess = None
        for key in odds_lookup:
            if g["home_team"] in key and g["away_team"] in key:
                odds_key_guess = key
                break
        odds_entry = odds_lookup.get(odds_key_guess, {"current": {}, "opening": {}})

        lines.append(f"\n{g['away_team']} ({g['away_record']}) @ {g['home_team']} ({g['home_record']})")
        lines.append(f"  {g['date']}  |  {g['venue']}  |  {g['status']}")
        lines.append(
            f"  {g['away_abbr']} — {away_stats['pts_for']} pts/gm, {away_stats['yards_per_game']} yds/gm, "
            f"TO margin {away_stats['turnover_margin']}  |  Home/Away: {away_splits['home']}/{away_splits['away']}"
        )
        lines.append(
            f"  {g['home_abbr']} — {home_stats['pts_for']} pts/gm, {home_stats['yards_per_game']} yds/gm, "
            f"TO margin {home_stats['turnover_margin']}  |  Home/Away: {home_splits['home']}/{home_splits['away']}"
        )
        lines.append(f"  Moneyline (current): {format_outcomes(odds_entry['current'].get('moneyline'))}")
        lines.append(f"  Moneyline (opening): {format_outcomes(odds_entry['opening'].get('moneyline'))}")
        lines.append(f"  Spread (current):    {format_outcomes(odds_entry['current'].get('spread'))}")
        lines.append(f"  Spread (opening):    {format_outcomes(odds_entry['opening'].get('spread'))}")
        lines.append(f"  Total (current):     {format_outcomes(odds_entry['current'].get('total'))}")
        lines.append(f"  Total (opening):     {format_outcomes(odds_entry['opening'].get('total'))}")
        if away_inj:
            lines.append(f"  {g['away_abbr']} injuries: {', '.join(away_inj)}")
        if home_inj:
            lines.append(f"  {g['home_abbr']} injuries: {', '.join(home_inj)}")

    return "\n".join(lines), week_label


# ---------------------------------------------------------------------------
# Delivery: Gmail (full report) + ntfy (short ping)
# ---------------------------------------------------------------------------

def send_email(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("WARNING: Gmail creds not set, skipping email.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def send_ntfy_ping(message):
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC not set, skipping push notification.")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "NFL Report Ready"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except urllib.error.URLError as e:
        print(f"WARNING: ntfy ping failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    report, week_label = build_report()
    print(report)  # also lands in the GitHub Actions log for debugging
    subject = f"NFL Report — {week_label or datetime.now().strftime('%Y-%m-%d')}"
    send_email(subject, report)
    send_ntfy_ping(f"{subject} is in your inbox.")


if __name__ == "__main__":
    main()
