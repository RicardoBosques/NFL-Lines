"""
nfl_report.py

Weekly NFL matchup + odds report.

Data sources
------------
- nflverse's GitHub-hosted data releases (nflverse/nflverse-data), specifically:
    * schedules/games.csv        -> weekly matchups, scores, win-loss records
    * stats_team/stats_team_week_<season>.csv -> per-team per-week offensive/
      defensive box-score stats, used here for yards/game and turnover margin
  These are plain file downloads from github.com/releases, not a live scraped
  API, so they don't carry the bot-protection risk ESPN's undocumented site
  API does. (An earlier version of this script used ESPN's scoreboard API
  directly; it kept getting blocked or timing out from GitHub Actions' cloud
  IPs, so this version switches to nflverse's GitHub-hosted CSVs instead.)
  There's no "probable starter" equivalent in football the way MLB has
  starting pitchers, so this starts at the team-stat level (points, yards,
  turnover margin, record/splits) rather than QB-level detail.
- The Odds API — api.the-odds-api.com, sport key `americanfootball_nfl`.
  Same free tier (500 requests/month) and same opening-odds self-tracking
  pattern as the MLB version, since the free tier has no historical odds.

Secrets expected (GitHub repo Settings -> Secrets and variables -> Actions):
  ODDS_API_KEY
  GMAIL_ADDRESS
  GMAIL_APP_PASSWORD
  NTFY_TOPIC

Files this script reads/writes:
  opening_odds.json  — auto-committed by the workflow each run, same as MLB
"""

import csv
import io
import json
import os
import smtplib
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, date
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMES_CSV_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
TEAM_STATS_CSV_URL_TMPL = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
)

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
OPENING_ODDS_FILE = "opening_odds.json"

REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_text(url, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nfl-report-script/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"WARNING: request failed after retries: {url} ({last_err})")
    return None


def fetch_csv(url):
    """Downloads a CSV file and returns a list of dict rows, or [] on failure."""
    text = fetch_text(url)
    if text is None:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def fetch_json(url, params=None, retries=2):
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
# Schedule / season-week detection / team records
# ---------------------------------------------------------------------------

def determine_current_season_and_week(games, today=None):
    """
    Picks the season/week to report on: the earliest REG-season week that
    still has a game today or in the future. Falls back to the most recent
    completed week if the season/data has otherwise wrapped up.
    """
    today = today or date.today()
    reg_games = [g for g in games if g.get("game_type") == "REG" and g.get("gameday")]
    if not reg_games:
        return None, None

    seasons = sorted({int(g["season"]) for g in reg_games})
    season = max(s for s in seasons if s <= today.year) if any(s <= today.year for s in seasons) else seasons[-1]

    season_games = [g for g in reg_games if int(g["season"]) == season]
    upcoming_weeks = sorted(
        {int(g["week"]) for g in season_games
         if datetime.strptime(g["gameday"], "%Y-%m-%d").date() >= today}
    )
    if upcoming_weeks:
        return season, upcoming_weeks[0]

    # Season's fully in the past (e.g. running the script in the off-season
    # gap) — fall back to the last week that was actually played.
    played_weeks = sorted({int(g["week"]) for g in season_games})
    return (season, played_weeks[-1]) if played_weeks else (None, None)


def team_record_and_splits(games, season, team, before_week):
    """
    Computes overall/home/away win-loss record and points for/against per
    game from completed games (score fields populated) before `before_week`.
    """
    played = [
        g for g in games
        if int(g["season"]) == season and g.get("game_type") == "REG"
        and int(g["week"]) < before_week
        and g.get("home_score") not in (None, "") and g.get("away_score") not in (None, "")
        and (g["home_team"] == team or g["away_team"] == team)
    ]

    def record_str(rows):
        w = l = t = 0
        pts_for = pts_against = 0
        for g in rows:
            home = g["home_team"] == team
            own = int(g["home_score"]) if home else int(g["away_score"])
            opp = int(g["away_score"]) if home else int(g["home_score"])
            pts_for += own
            pts_against += opp
            if own > opp:
                w += 1
            elif own < opp:
                l += 1
            else:
                t += 1
        record = f"{w}-{l}" + (f"-{t}" if t else "")
        games_played = len(rows)
        avg_for = round(pts_for / games_played, 1) if games_played else "N/A"
        avg_against = round(pts_against / games_played, 1) if games_played else "N/A"
        return record, avg_for, avg_against

    overall_record, pts_for_avg, pts_against_avg = record_str(played)
    home_record, _, _ = record_str([g for g in played if g["home_team"] == team])
    away_record, _, _ = record_str([g for g in played if g["away_team"] == team])

    return {
        "record": overall_record if played else "0-0",
        "pts_for": pts_for_avg,
        "pts_against": pts_against_avg,
        "home_record": home_record if played else "0-0",
        "away_record": away_record if played else "0-0",
    }


def week_matchups(games, season, week):
    return [
        g for g in games
        if int(g["season"]) == season and g.get("game_type") == "REG" and int(g["week"]) == week
    ]


# ---------------------------------------------------------------------------
# Team weekly box-score stats -> yards/game, turnover margin
# ---------------------------------------------------------------------------

def team_yardage_and_turnovers(team_week_stats, season, team, before_week):
    rows = [
        r for r in team_week_stats
        if r.get("team") == team and int(r.get("season", 0)) == season
        and int(r.get("week", 0)) < before_week
    ]
    if not rows:
        return {"yards_per_game": "N/A", "turnover_margin": "N/A"}

    def to_int(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    total_yards = sum(to_int(r.get("passing_yards")) + to_int(r.get("rushing_yards")) for r in rows)
    takeaways = sum(to_int(r.get("def_interceptions")) + to_int(r.get("fumble_recovery_opp")) for r in rows)
    giveaways = sum(to_int(r.get("passing_interceptions")) + to_int(r.get("fumbles_lost_total")) for r in rows)

    games_played = len(rows)
    return {
        "yards_per_game": round(total_yards / games_played, 1),
        "turnover_margin": f"{takeaways - giveaways:+d}",
    }


# ---------------------------------------------------------------------------
# The Odds API: current lines + self-tracked opening lines
# ---------------------------------------------------------------------------

def get_current_odds():
    if not ODDS_API_KEY:
        print("WARNING: ODDS_API_KEY not set, skipping odds.")
        return []
    data = fetch_json(
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
    for bm in game_odds.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") == market_key:
                return market.get("outcomes", [])
    return None


def build_odds_lookup(odds_list, opening):
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
        price_str = f"{price:+d}" if isinstance(price, int) else str(price)
        if point is not None:
            parts.append(f"{name} {point:+g} ({price_str})")
        else:
            parts.append(f"{name} ({price_str})")
    return " / ".join(parts)


# nflverse team abbreviations mostly line up with The Odds API's full team
# names via a simple lookup — this covers the common relocations/renamings.
TEAM_FULL_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LA": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def build_report():
    games = fetch_csv(GAMES_CSV_URL)
    if not games:
        return "Could not load schedule data (nflverse games.csv fetch failed).", "unknown"

    season, week = determine_current_season_and_week(games)
    if season is None:
        return "Could not determine the current NFL week from schedule data.", "unknown"

    team_week_stats = fetch_csv(TEAM_STATS_CSV_URL_TMPL.format(season=season))
    matchups = week_matchups(games, season, week)

    odds_list = get_current_odds()
    opening = load_opening_odds()
    odds_lookup = build_odds_lookup(odds_list, opening)
    save_opening_odds(opening)

    week_label = f"{season} Week {week}"
    lines = []
    lines.append(f"NFL WEEKLY REPORT — {week_label}")
    lines.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    if not matchups:
        lines.append("\nNo games found for the current week (bye week, or schedule data not yet updated).")

    for g in matchups:
        home, away = g["home_team"], g["away_team"]
        home_rec = team_record_and_splits(games, season, home, week)
        away_rec = team_record_and_splits(games, season, away, week)
        home_yd = team_yardage_and_turnovers(team_week_stats, season, home, week)
        away_yd = team_yardage_and_turnovers(team_week_stats, season, away, week)

        home_full = TEAM_FULL_NAMES.get(home, home)
        away_full = TEAM_FULL_NAMES.get(away, away)

        odds_key_guess = None
        for key in odds_lookup:
            if home_full in key and away_full in key:
                odds_key_guess = key
                break
        odds_entry = odds_lookup.get(odds_key_guess, {"current": {}, "opening": {}})

        lines.append(f"\n{away_full} ({away_rec['record']}) @ {home_full} ({home_rec['record']})")
        lines.append(f"  {g.get('gameday', '')}  {g.get('gametime', '')}  |  {g.get('stadium', '')}")
        lines.append(
            f"  {away} — {away_rec['pts_for']} pts/gm scored, {away_rec['pts_against']} allowed, "
            f"{away_yd['yards_per_game']} yds/gm, TO margin {away_yd['turnover_margin']}  "
            f"|  Home/Away: {away_rec['home_record']}/{away_rec['away_record']}"
        )
        lines.append(
            f"  {home} — {home_rec['pts_for']} pts/gm scored, {home_rec['pts_against']} allowed, "
            f"{home_yd['yards_per_game']} yds/gm, TO margin {home_yd['turnover_margin']}  "
            f"|  Home/Away: {home_rec['home_record']}/{home_rec['away_record']}"
        )
        lines.append(f"  Moneyline (current): {format_outcomes(odds_entry['current'].get('moneyline'))}")
        lines.append(f"  Moneyline (opening): {format_outcomes(odds_entry['opening'].get('moneyline'))}")
        lines.append(f"  Spread (current):    {format_outcomes(odds_entry['current'].get('spread'))}")
        lines.append(f"  Spread (opening):    {format_outcomes(odds_entry['opening'].get('spread'))}")
        lines.append(f"  Total (current):     {format_outcomes(odds_entry['current'].get('total'))}")
        lines.append(f"  Total (opening):     {format_outcomes(odds_entry['opening'].get('total'))}")
        if g.get("spread_line"):
            lines.append(f"  (nflverse closing-line reference — spread {g.get('spread_line')}, total {g.get('total_line')})")

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
    print(report)
    subject = f"NFL Report — {week_label}"
    send_email(subject, report)
    send_ntfy_ping(f"{subject} is in your inbox.")


if __name__ == "__main__":
    main()
