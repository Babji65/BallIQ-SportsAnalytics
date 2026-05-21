"""
balliq — fetch_data.py
Pulls NFL + NCAAF data from ESPN's unofficial API (no key needed)
and writes data.json for the frontend to consume.

ESPN endpoints used:
  Scoreboard:   https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard
  Game summary: https://site.api.espn.com/apis/site/v2/sports/football/{league}/summary?event={game_id}
  Roster:       https://site.api.espn.com/apis/site/v2/sports/football/{league}/teams/{team_id}/roster
  Injuries:     https://sports.core.api.espn.com/v2/sports/football/leagues/{league}/injuries
  Athlete:      https://sports.core.api.espn.com/v2/sports/football/leagues/{league}/athletes/{athlete_id}
  Weather:      https://api.open-meteo.com/v1/forecast (free, no key)
"""

import json
import time
import requests
from datetime import datetime, timezone

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; balliq/1.0)"}
NFL   = "nfl"
NCAAF = "college-football"

# ── helpers ────────────────────────────────────────────────────────────────

def get(url, params=None):
    """GET with retries and a polite delay."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            r.raise_for_status()
            time.sleep(0.4)          # be polite to ESPN
            return r.json()
        except Exception as e:
            print(f"  [warn] attempt {attempt+1} failed for {url}: {e}")
            time.sleep(2)
    return {}


def safe(d, *keys, default=None):
    """Safely navigate nested dicts."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


# ── weather ────────────────────────────────────────────────────────────────

STADIUM_COORDS = {
    # NFL — outdoor stadiums only (domes excluded)
    "KC":  (39.0489, -94.4839),   # Arrowhead
    "BAL": (39.2780, -76.6227),   # M&T Bank
    "PHI": (39.9008, -75.1675),   # Lincoln Financial
    "DAL": None,                  # AT&T Stadium — dome
    "SF":  (37.4033, -121.9694),  # Levi's
    "GB":  (44.5013, -88.0622),   # Lambeau
    "BUF": (42.7738, -78.7870),   # Highmark
    "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745),
    "CHI": (41.8623, -87.6167),
    "CLE": (41.5061, -81.6995),
    "CIN": (39.0955, -84.5160),
    "PIT": (40.4468, -80.0158),
    "DEN": (39.7439, -105.0201),
    "LAC": None,                  # SoFi — dome
    "LAR": None,
    "ARI": None,
    "LV":  None,                  # Allegiant — dome
    "SEA": (47.5952, -122.3316),
    "TEN": (36.1665, -86.7713),
    "JAX": (30.3239, -81.6373),
    "MIA": (25.9580, -80.2389),
    "TB":  (27.9759, -82.5033),
    "NO":  None,                  # Caesars — dome
    "ATL": None,
    "CAR": (35.2258, -80.8528),
    "WAS": (38.9079, -76.8644),
    "NE":  (42.0909, -71.2643),
    "HOU": None,
}

def get_weather(lat, lon, game_date_str):
    """Fetch forecast for game day from Open-Meteo (free, no key)."""
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,precipitation_sum,windspeed_10m_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 7,
        }
        data = requests.get(url, params=params, timeout=8).json()
        dates = data.get("daily", {}).get("time", [])
        target = game_date_str[:10]   # YYYY-MM-DD
        if target in dates:
            idx = dates.index(target)
            return {
                "temp_f":    data["daily"]["temperature_2m_max"][idx],
                "precip_in": round(data["daily"]["precipitation_sum"][idx] * 0.0394, 2),
                "wind_mph":  data["daily"]["windspeed_10m_max"][idx],
            }
    except Exception as e:
        print(f"  [warn] weather fetch failed: {e}")
    return None


# ── injuries ───────────────────────────────────────────────────────────────

def fetch_injuries(league):
    """
    Returns dict: { athlete_id: { name, status, detail, updated } }
    ESPN injury endpoint paginates — we walk all pages.
    """
    injuries = {}
    url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/{league}/injuries"
    params = {"limit": 100, "page": 1}
    while url:
        data = get(url, params=params)
        params = None                 # only used on first call
        for item in data.get("items", []):
            ref = item.get("$ref", "")
            detail_data = get(ref) if ref else {}
            athlete_ref = safe(detail_data, "athlete", "$ref", default="")
            athlete_id  = athlete_ref.split("/")[-1].split("?")[0] if athlete_ref else None
            if athlete_id:
                injuries[athlete_id] = {
                    "status":  safe(detail_data, "status", "type", "description", default="Questionable"),
                    "detail":  safe(detail_data, "type", "description", default=""),
                    "updated": safe(detail_data, "date", default=""),
                }
        # pagination
        next_page = safe(data, "pageIndex", default=0)
        page_count = safe(data, "pageCount", default=1)
        if next_page and next_page < page_count:
            url = f"https://sports.core.api.espn.com/v2/sports/football/leagues/{league}/injuries"
            params = {"limit": 100, "page": next_page + 1}
        else:
            url = None
    print(f"  → {len(injuries)} injuries loaded for {league}")
    return injuries


# ── game logs ──────────────────────────────────────────────────────────────

STAT_KEYS = {
    # ESPN category name → our friendly name
    "passing":   ["completions/passingAttempts", "passingYards", "yardsPerPassAttempt",
                  "passingTouchdowns", "interceptions", "QBRating"],
    "rushing":   ["rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns"],
    "receiving": ["receptions", "receivingYards", "yardsPerReception", "receivingTouchdowns",
                  "targets", "longReception"],
}

FRIENDLY = {
    "passingYards":        "pass_yds",
    "passingTouchdowns":   "pass_td",
    "interceptions":       "int",
    "QBRating":            "qbr",
    "rushingYards":        "rush_yds",
    "rushingTouchdowns":   "rush_td",
    "rushingAttempts":     "rush_att",
    "receptions":          "rec",
    "receivingYards":      "rec_yds",
    "receivingTouchdowns": "rec_td",
    "targets":             "targets",
}

def parse_stat_line(categories):
    """Pull useful stats out of ESPN's nested categories list."""
    out = {}
    for cat in categories:
        cat_name = cat.get("name", "")
        if cat_name not in STAT_KEYS:
            continue
        keys_wanted = STAT_KEYS[cat_name]
        stat_names  = cat.get("names", [])
        stat_values = cat.get("values", [])
        for k in keys_wanted:
            if k in stat_names:
                idx = stat_names.index(k)
                friendly = FRIENDLY.get(k, k)
                val = stat_values[idx] if idx < len(stat_values) else 0
                try:
                    out[friendly] = round(float(val), 1)
                except (TypeError, ValueError):
                    out[friendly] = 0
    return out


def fetch_player_game_logs(league, athlete_id, last_n=10):
    """
    Fetch last N game logs for a player via ESPN athlete gamelog endpoint.
    Returns list of stat dicts, newest first.
    """
    url = (f"https://sports.core.api.espn.com/v2/sports/football"
           f"/leagues/{league}/athletes/{athlete_id}/gamelog")
    data = get(url)
    logs = []
    entries = data.get("entries", [])
    # entries are oldest-first; reverse for newest-first
    for entry in reversed(entries):
        stats_raw = entry.get("stats", [])
        # ESPN sometimes uses 'statistics' instead
        if not stats_raw:
            stats_raw = entry.get("statistics", [])
        game_date = safe(entry, "game", "date", default="")
        stat_line = parse_stat_line(stats_raw) if isinstance(stats_raw, list) else {}
        if stat_line:
            stat_line["date"] = game_date[:10] if game_date else ""
            logs.append(stat_line)
        if len(logs) >= last_n:
            break
    return logs


# ── edge / model ───────────────────────────────────────────────────────────

def compute_edge(logs, prop_type, line):
    """
    Given recent game logs and a prop type + line (entered by user),
    return hit_rate, avg, trend list, and edge %.
    prop_type: 'pass_yds' | 'pass_td' | 'rec_yds' | 'rec' | 'rush_yds' etc.
    """
    vals = [g.get(prop_type, 0) for g in logs if prop_type in g]
    if not vals:
        return None
    avg   = round(sum(vals) / len(vals), 1)
    hits  = sum(1 for v in vals if v > line)
    hit_rate = round(hits / len(vals) * 100)
    # simple edge: how far avg sits above/below line, scaled to implied prob
    edge = round((avg - line) / max(line, 1) * 20, 1)   # normalised, not true EV
    verdict = "bet" if edge > 3 and hit_rate >= 60 else \
              "pass" if edge < 0 or hit_rate < 40 else "watch"
    return {
        "avg":      avg,
        "hit_rate": hit_rate,
        "trend":    vals,
        "edge":     edge,
        "verdict":  verdict,
    }


# ── main fetch ─────────────────────────────────────────────────────────────

TRACKED_POSITIONS = {"QB", "WR", "RB", "TE"}

# Default prop lines — user can override these in the HTML.
# Format: { position: [ (stat_key, friendly_name, default_line) ] }
DEFAULT_PROPS = {
    "QB": [
        ("pass_yds", "Passing Yards",  250.5),
        ("pass_td",  "Passing TDs",      1.5),
        ("rush_yds", "Rush Yards",       20.5),
    ],
    "WR": [
        ("rec_yds", "Receiving Yards", 55.5),
        ("rec",     "Receptions",        4.5),
        ("rec_td",  "Receiving TDs",     0.5),
    ],
    "RB": [
        ("rush_yds", "Rush Yards",      55.5),
        ("rec_yds",  "Receiving Yards", 20.5),
        ("rush_td",  "Rush TDs",         0.5),
    ],
    "TE": [
        ("rec_yds", "Receiving Yards", 40.5),
        ("rec",     "Receptions",        3.5),
        ("rec_td",  "Receiving TDs",     0.5),
    ],
}


def fetch_league_games(league_key, espn_league):
    """Fetch upcoming/current-week games + player data for one league."""
    print(f"\n=== {league_key} ===")
    scoreboard_url = (f"https://site.api.espn.com/apis/site/v2/sports"
                      f"/football/{espn_league}/scoreboard")
    board = get(scoreboard_url)
    events = board.get("events", [])
    print(f"  {len(events)} games found")

    injuries = fetch_injuries(espn_league)
    games_out = []

    for event in events[:8]:          # cap at 8 games per league
        game_id   = event.get("id", "")
        game_name = event.get("name", "")
        game_date = event.get("date", "")
        status    = safe(event, "status", "type", "name", default="pre")
        competitors = safe(event, "competitions", 0, "competitors", default=[])

        home_team = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away_team = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home_abbr = safe(home_team, "team", "abbreviation", default="")
        away_abbr = safe(away_team, "team", "abbreviation", default="")
        home_id   = safe(home_team, "team", "id", default="")
        away_id   = safe(away_team, "team", "id", default="")
        spread    = safe(event, "competitions", 0, "odds", 0, "details", default="")
        venue     = safe(event, "competitions", 0, "venue", "fullName", default="")

        # weather (outdoor games only)
        weather = None
        coords  = STADIUM_COORDS.get(home_abbr)
        if coords:
            weather = get_weather(coords[0], coords[1], game_date)

        print(f"  Game: {away_abbr} @ {home_abbr}  [{game_date[:10]}]")

        # fetch top players from both rosters
        players_out = []
        for team_id, team_abbr in [(home_id, home_abbr), (away_id, away_abbr)]:
            roster_url = (f"https://site.api.espn.com/apis/site/v2/sports"
                          f"/football/{espn_league}/teams/{team_id}/roster")
            roster = get(roster_url)
            athletes = roster.get("athletes", [])
            # flatten position groups
            all_athletes = []
            for group in athletes:
                if isinstance(group, dict) and "items" in group:
                    all_athletes.extend(group["items"])
                elif isinstance(group, dict) and "id" in group:
                    all_athletes.append(group)

            # filter to tracked positions, take top players by jersey number presence
            tracked = [a for a in all_athletes
                       if safe(a, "position", "abbreviation", default="") in TRACKED_POSITIONS][:12]

            for athlete in tracked:
                athlete_id  = athlete.get("id", "")
                name        = athlete.get("fullName", "")
                pos         = safe(athlete, "position", "abbreviation", default="")
                jersey      = athlete.get("jersey", "")
                initials    = "".join(p[0].upper() for p in name.split()[:2]) if name else "??"

                # injury status
                inj = injuries.get(str(athlete_id), {})
                injury_status = inj.get("status", "Active")
                injury_detail = inj.get("detail", "")

                # game logs
                logs = fetch_player_game_logs(espn_league, athlete_id, last_n=10)
                if not logs:
                    print(f"    [skip] {name} — no game logs")
                    continue

                # build season averages from logs
                season_avgs = {}
                for stat in ["pass_yds", "pass_td", "rec_yds", "rec", "rush_yds", "rush_td", "qbr"]:
                    vals = [g[stat] for g in logs if stat in g]
                    if vals:
                        season_avgs[stat] = round(sum(vals) / len(vals), 1)

                # build props using default lines
                props = []
                for stat_key, label, default_line in DEFAULT_PROPS.get(pos, []):
                    result = compute_edge(logs, stat_key, default_line)
                    if result:
                        props.append({
                            "type":         label,
                            "stat_key":     stat_key,
                            "line":         default_line,
                            "hit_rate":     result["hit_rate"],
                            "trend":        result["trend"],
                            "avg":          result["avg"],
                            "edge":         result["edge"],
                            "verdict":      result["verdict"],
                            # user will enter book odds in the UI
                            "over_odds":    None,
                            "under_odds":   None,
                        })

                if not props:
                    continue

                # overall edge = mean of prop edges
                mean_edge = round(sum(p["edge"] for p in props) / len(props), 1)
                edge_str  = f"+{mean_edge}" if mean_edge > 0 else str(mean_edge)

                players_out.append({
                    "id":             athlete_id,
                    "name":           name,
                    "pos":            pos,
                    "team":           team_abbr,
                    "jersey":         jersey,
                    "initials":       initials,
                    "injury_status":  injury_status,
                    "injury_detail":  injury_detail,
                    "season_avgs":    season_avgs,
                    "game_logs":      logs,
                    "props":          props,
                    "overall_edge":   edge_str,
                })
                print(f"    ✓ {name} ({pos}) — {len(props)} props, edge {edge_str}%")

        games_out.append({
            "id":       game_id,
            "home":     home_abbr,
            "away":     away_abbr,
            "league":   league_key,
            "date":     game_date[:10],
            "time":     game_date[11:16] + " UTC" if len(game_date) > 10 else "",
            "spread":   spread,
            "venue":    venue,
            "status":   status,
            "weather":  weather,
            "players":  players_out,
        })

    return games_out


def main():
    print("balliq data fetch starting…")
    all_games = []
    all_games += fetch_league_games("NFL",   NFL)
    all_games += fetch_league_games("NCAAF", NCAAF)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "games":      all_games,
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅  data.json written — {len(all_games)} games, "
          f"{sum(len(g['players']) for g in all_games)} players")


if __name__ == "__main__":
    main()
