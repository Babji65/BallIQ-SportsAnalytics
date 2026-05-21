"""
balliq — fetch_data.py
Pulls NFL + NCAAF data from ESPN's unofficial API (no key needed).
Writes data.json for the frontend.
"""

import json, time, requests
from datetime import datetime, timezone

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; balliq/1.0)"}
NFL   = "nfl"
NCAAF = "college-football"

# ── helpers ────────────────────────────────────────────────────────────────

def get(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=12)
            r.raise_for_status()
            time.sleep(0.3)
            return r.json()
        except Exception as e:
            print(f"  [warn] attempt {attempt+1} failed for {url}: {e}")
            time.sleep(2)
    return {}

def safe(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, (dict, list)):
            return default
        try:
            d = d[k]
        except (KeyError, IndexError, TypeError):
            return default
    return d if d is not None else default

def resolve_ref(obj):
    """If an object only has a $ref, fetch it."""
    if obj and list(obj.keys()) == ["$ref"]:
        return get(obj["$ref"]) or {}
    return obj

# ── weather ────────────────────────────────────────────────────────────────

STADIUM_COORDS = {
    "KC": (39.0489,-94.4839), "BAL": (39.2780,-76.6227),
    "PHI": (39.9008,-75.1675), "SF": (37.4033,-121.9694),
    "GB": (44.5013,-88.0622), "BUF": (42.7738,-78.787),
    "CHI": (41.8623,-87.6167), "CLE": (41.5061,-81.6995),
    "CIN": (39.0955,-84.516), "PIT": (40.4468,-80.0158),
    "DEN": (39.7439,-105.020), "SEA": (47.5952,-122.332),
    "TEN": (36.1665,-86.7713), "JAX": (30.3239,-81.6373),
    "MIA": (25.958,-80.2389), "TB": (27.9759,-82.5033),
    "CAR": (35.2258,-80.8528), "WAS": (38.9079,-76.8644),
    "NE": (42.0909,-71.2643),
    # dome teams — no weather needed
    "DAL": None, "LAC": None, "LAR": None, "ARI": None,
    "LV": None, "NO": None, "ATL": None, "MIN": None, "IND": None,
}

def get_weather(lat, lon, game_date_str):
    try:
        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,precipitation_sum,windspeed_10m_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "auto", "forecast_days": 16,
        }
        data = requests.get("https://api.open-meteo.com/v1/forecast",
                            params=params, timeout=8).json()
        dates = data.get("daily", {}).get("time", [])
        target = game_date_str[:10]
        if target in dates:
            i = dates.index(target)
            return {
                "temp_f":    data["daily"]["temperature_2m_max"][i],
                "precip_in": round(data["daily"]["precipitation_sum"][i] * 0.0394, 2),
                "wind_mph":  data["daily"]["windspeed_10m_max"][i],
            }
    except Exception as e:
        print(f"  [warn] weather failed: {e}")
    return None

# ── injuries — team-level endpoint (works when league-level 404s) ──────────

def fetch_team_injuries(league, team_id):
    """Fetch injuries for one team. Returns {athlete_id: {status, detail}}"""
    url = (f"https://sports.core.api.espn.com/v2/sports/football"
           f"/leagues/{league}/teams/{team_id}/injuries")
    data = get(url)
    out = {}
    for item in data.get("items", []):
        item = resolve_ref(item)
        athlete_ref = safe(item, "athlete", "$ref", default="")
        athlete_id  = athlete_ref.rstrip("/").split("/")[-1].split("?")[0] if athlete_ref else None
        if athlete_id:
            out[athlete_id] = {
                "status": safe(item, "status", "type", "description", default="Questionable"),
                "detail": safe(item, "type", "description", default=""),
            }
    return out

# ── game logs ──────────────────────────────────────────────────────────────

STAT_KEYS = {
    "passing":   ["passingYards","passingTouchdowns","interceptions","QBRating"],
    "rushing":   ["rushingYards","rushingTouchdowns","rushingAttempts"],
    "receiving": ["receptions","receivingYards","receivingTouchdowns","targets"],
}
FRIENDLY = {
    "passingYards":"pass_yds","passingTouchdowns":"pass_td","interceptions":"int",
    "QBRating":"qbr","rushingYards":"rush_yds","rushingTouchdowns":"rush_td",
    "rushingAttempts":"rush_att","receptions":"rec","receivingYards":"rec_yds",
    "receivingTouchdowns":"rec_td","targets":"targets",
}

def parse_stat_line(categories):
    out = {}
    for cat in (categories or []):
        cat_name = cat.get("name","")
        if cat_name not in STAT_KEYS:
            continue
        names  = cat.get("names", [])
        values = cat.get("values", [])
        for k in STAT_KEYS[cat_name]:
            if k in names:
                idx = names.index(k)
                try:
                    out[FRIENDLY[k]] = round(float(values[idx]), 1)
                except (IndexError, TypeError, ValueError):
                    out[FRIENDLY[k]] = 0
    return out

def fetch_player_game_logs(league, athlete_id, last_n=10):
    url = (f"https://sports.core.api.espn.com/v2/sports/football"
           f"/leagues/{league}/athletes/{athlete_id}/gamelog")
    data = get(url)
    logs = []
    for entry in reversed(data.get("entries", [])):
        stats_raw = entry.get("stats") or entry.get("statistics") or []
        stat_line = parse_stat_line(stats_raw) if isinstance(stats_raw, list) else {}
        if stat_line:
            stat_line["date"] = safe(entry, "game", "date", default="")[:10]
            logs.append(stat_line)
        if len(logs) >= last_n:
            break
    return logs

# ── edge model ─────────────────────────────────────────────────────────────

def compute_edge(logs, stat_key, line):
    vals = [g[stat_key] for g in logs if stat_key in g]
    if not vals:
        return None
    avg      = round(sum(vals) / len(vals), 1)
    hits     = sum(1 for v in vals if v > line)
    hit_rate = round(hits / len(vals) * 100)
    edge     = round((avg - line) / max(abs(line), 1) * 20, 1)
    verdict  = ("bet"   if edge > 3 and hit_rate >= 60 else
                "pass"  if edge < 0 or hit_rate < 40   else "watch")
    return {"avg": avg, "hit_rate": hit_rate, "trend": vals,
            "edge": edge, "verdict": verdict}

# ── positions + props ──────────────────────────────────────────────────────

TRACKED = {"QB","WR","RB","TE"}
DEFAULT_PROPS = {
    "QB": [("pass_yds","Passing Yards",250.5),("pass_td","Passing TDs",1.5),("rush_yds","Rush Yards",20.5)],
    "WR": [("rec_yds","Receiving Yards",55.5),("rec","Receptions",4.5),("rec_td","Receiving TDs",0.5)],
    "RB": [("rush_yds","Rush Yards",55.5),("rec_yds","Receiving Yards",20.5),("rush_td","Rush TDs",0.5)],
    "TE": [("rec_yds","Receiving Yards",40.5),("rec","Receptions",3.5),("rec_td","Receiving TDs",0.5)],
}

# ── roster: use summary endpoint which embeds full team data ───────────────

def fetch_roster(league, team_id):
    """
    ESPN roster endpoint. team_id must be the numeric ID (e.g. '12').
    Returns list of athlete dicts.
    """
    url = (f"https://site.api.espn.com/apis/site/v2/sports/football"
           f"/{league}/teams/{team_id}/roster")
    data = get(url)
    all_athletes = []
    athletes_raw = data.get("athletes", [])
    for group in athletes_raw:
        group = resolve_ref(group)
        if "items" in group:
            all_athletes.extend(group["items"])
        elif "id" in group:
            all_athletes.append(group)
    return all_athletes

# ── main ───────────────────────────────────────────────────────────────────

def fetch_league_games(league_key, espn_league):
    print(f"\n=== {league_key} ===")
    board = get(f"https://site.api.espn.com/apis/site/v2/sports/football/{espn_league}/scoreboard")
    events = board.get("events", [])
    print(f"  {len(events)} games on scoreboard")

    games_out = []

    for event in events[:8]:
        game_id   = event.get("id","")
        game_date = event.get("date","")
        status    = safe(event,"status","type","name",default="pre")

        comp = safe(event,"competitions",0,default={})
        competitors = comp.get("competitors",[])
        spread = safe(comp,"odds",0,"details",default="")
        venue  = safe(comp,"venue","fullName",default="")

        # ── resolve each competitor (may be a $ref) ──
        home_data, away_data = {}, {}
        for c in competitors:
            c = resolve_ref(c)
            team = resolve_ref(c.get("team", {}))
            # homeAway lives on competitor, team info on team
            side = c.get("homeAway","")
            if side == "home":
                home_data = {"team": team, "comp": c}
            else:
                away_data = {"team": team, "comp": c}

        home_team = home_data.get("team", {})
        away_team = away_data.get("team", {})
        home_abbr = home_team.get("abbreviation","")
        away_abbr = away_team.get("abbreviation","")
        home_id   = home_team.get("id","") or home_data.get("comp",{}).get("id","")
        away_id   = away_team.get("id","") or away_data.get("comp",{}).get("id","")

        if not home_id or not away_id:
            print(f"  [skip] could not resolve team IDs for event {game_id}")
            continue

        print(f"  Game: {away_abbr}({away_id}) @ {home_abbr}({home_id})  [{game_date[:10]}]")

        # weather
        weather = None
        coords  = STADIUM_COORDS.get(home_abbr)
        if coords:
            weather = get_weather(coords[0], coords[1], game_date)

        players_out = []
        for team_id, team_abbr in [(home_id, home_abbr), (away_id, away_abbr)]:

            # injuries for this team
            injuries = fetch_team_injuries(espn_league, team_id)

            athletes = fetch_roster(espn_league, team_id)
            tracked  = [a for a in athletes
                        if safe(a,"position","abbreviation",default="") in TRACKED][:12]

            for athlete in tracked:
                athlete_id = str(athlete.get("id",""))
                name       = athlete.get("fullName","")
                pos        = safe(athlete,"position","abbreviation",default="")
                jersey     = athlete.get("jersey","")
                initials   = "".join(p[0].upper() for p in name.split()[:2]) if name else "??"

                inj = injuries.get(athlete_id, {})
                injury_status = inj.get("status","Active")
                injury_detail = inj.get("detail","")

                logs = fetch_player_game_logs(espn_league, athlete_id, last_n=10)
                if not logs:
                    print(f"    [skip] {name} — no logs")
                    continue

                season_avgs = {}
                for stat in ["pass_yds","pass_td","rec_yds","rec","rush_yds","rush_td","qbr"]:
                    vals = [g[stat] for g in logs if stat in g]
                    if vals:
                        season_avgs[stat] = round(sum(vals)/len(vals), 1)

                props = []
                for stat_key, label, default_line in DEFAULT_PROPS.get(pos, []):
                    result = compute_edge(logs, stat_key, default_line)
                    if result:
                        props.append({
                            "type": label, "stat_key": stat_key,
                            "line": default_line,
                            "hit_rate": result["hit_rate"],
                            "trend": result["trend"],
                            "avg": result["avg"],
                            "edge": result["edge"],
                            "verdict": result["verdict"],
                            "over_odds": None, "under_odds": None,
                        })

                if not props:
                    continue

                mean_edge = round(sum(p["edge"] for p in props)/len(props), 1)
                edge_str  = f"+{mean_edge}" if mean_edge >= 0 else str(mean_edge)

                players_out.append({
                    "id": athlete_id, "name": name, "pos": pos,
                    "team": team_abbr, "jersey": jersey, "initials": initials,
                    "injury_status": injury_status, "injury_detail": injury_detail,
                    "season_avgs": season_avgs, "game_logs": logs,
                    "props": props, "overall_edge": edge_str,
                })
                print(f"    ✓ {name} ({pos}) edge {edge_str}%")

        games_out.append({
            "id": game_id, "home": home_abbr, "away": away_abbr,
            "league": league_key, "date": game_date[:10],
            "time": game_date[11:16]+" UTC" if len(game_date)>10 else "",
            "spread": spread, "venue": venue, "status": status,
            "weather": weather, "players": players_out,
        })

    return games_out


def main():
    print("balliq data fetch starting…")
    all_games = fetch_league_games("NFL", NFL) + fetch_league_games("NCAAF", NCAAF)
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "games": all_games,
    }
    with open("data.json","w") as f:
        json.dump(output, f, indent=2)
    total_players = sum(len(g["players"]) for g in all_games)
    print(f"\n✅  data.json written — {len(all_games)} games, {total_players} players")

if __name__ == "__main__":
    main()
