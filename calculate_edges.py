"""
balliq — calculate_edges.py

The model engine. Reads data.json (written by fetch_data.py) and enriches
every player prop with a full multi-factor score. Writes back to data.json.

Factors used
────────────
1. Weighted recent form   — exponential decay, last 3 games weigh 3x more
2. Consistency score      — penalises boom/bust players (std dev relative to mean)
3. Trend direction        — is the player improving or declining week-over-week?
4. Home / away split      — players often have meaningful home/away differentials
5. Injury risk            — downgrade props for questionable/doubtful players
6. Weather impact         — wind >15mph and precip penalise passing props
7. Matchup rating         — derived from opponent's defensive stats vs that position
8. Line value (EV)        — only calculated when user enters odds; modelled here
                             using -110 as placeholder so picks are still ranked

Final outputs per prop
──────────────────────
- model_prob    : model's estimated probability player hits the over (0-1)
- implied_prob  : book's implied probability at -110 default (0.5238)
- ev_pct        : expected value % per $100 at default odds
- confidence    : 0-100 score combining all factors
- grade         : S / A / B / C / D
- verdict       : "bet" / "watch" / "pass"
- reasons       : list of human-readable reasons for the grade
"""

import json, math, statistics
from datetime import datetime, timezone

# ── constants ──────────────────────────────────────────────────────────────

# Default book juice assumed when user hasn't entered odds yet
DEFAULT_OVER_ODDS  = -110
DEFAULT_UNDER_ODDS = -110

# Injury status weights (multiplier on confidence)
INJURY_WEIGHT = {
    "Active":       1.00,
    "Full":         1.00,
    "Limited":      0.85,
    "Questionable": 0.75,
    "Doubtful":     0.50,
    "Out":          0.00,
    "IR":           0.00,
}

# Weather penalty for passing stats (wind mph thresholds)
WIND_PASS_PENALTY  = {15: 0.03, 20: 0.07, 25: 0.12, 30: 0.18}  # subtract from model_prob
PRECIP_PASS_PENALTY = 0.04   # if precip > 0.1"

# Passing-affected stat keys
PASS_STATS = {"pass_yds", "pass_td", "int", "rec_yds", "rec_td", "rec", "targets"}

# Exponential decay weights for last N games (newest = highest weight)
# For 10 games: [0.19, 0.17, 0.15, 0.13, 0.11, 0.09, 0.07, 0.05, 0.03, 0.01] (approx)
def decay_weights(n):
    raw = [math.exp(0.2 * i) for i in range(n)]
    total = sum(raw)
    return [w / total for w in raw]   # newest first after we reverse the list


# ── helpers ────────────────────────────────────────────────────────────────

def american_to_prob(odds):
    """Convert American odds to implied probability (including vig)."""
    o = float(odds)
    return (-o / (-o + 100)) if o < 0 else (100 / (o + 100))

def prob_to_ev(model_prob, over_odds):
    """Expected value per $1 wagered on the over."""
    implied = american_to_prob(over_odds)
    payout  = (abs(over_odds) / 100) if over_odds < 0 else (over_odds / 100)
    return model_prob * payout - (1 - model_prob) * 1

def kelly_fraction(model_prob, over_odds, fraction=0.25):
    """
    Quarter-Kelly stake as % of bankroll.
    Full Kelly is aggressive; quarter-Kelly is standard for sports betting.
    Returns 0 if bet has no edge.
    """
    implied = american_to_prob(over_odds)
    b = (abs(over_odds) / 100) if over_odds < 0 else (over_odds / 100)
    q = 1 - model_prob
    p = model_prob
    full_kelly = (b * p - q) / b
    return max(0, round(full_kelly * fraction * 100, 1))  # as % of bankroll

def grade_from_confidence(conf):
    if conf >= 80: return "S"
    if conf >= 65: return "A"
    if conf >= 50: return "B"
    if conf >= 35: return "C"
    return "D"

def verdict_from_confidence(conf, ev_pct):
    if conf >= 65 and ev_pct > 3:  return "bet"
    if conf < 35 or ev_pct < -5:   return "pass"
    return "watch"


# ── factor 1: weighted recent form ────────────────────────────────────────

def weighted_avg(values):
    """Exponentially weighted average, most recent game = highest weight."""
    if not values:
        return 0
    weights = decay_weights(len(values))
    # values[0] = most recent, weights[0] = highest
    return sum(v * w for v, w in zip(values, weights))


# ── factor 2: consistency ─────────────────────────────────────────────────

def consistency_score(values):
    """
    Returns 0-1. 1 = perfectly consistent, 0 = extremely volatile.
    Uses coefficient of variation (std / mean).
    """
    if len(values) < 2:
        return 0.7   # default if not enough data
    mean = statistics.mean(values)
    if mean == 0:
        return 0.5
    cv = statistics.stdev(values) / abs(mean)
    # CV of 0 = perfect consistency = score 1.0
    # CV of 1 = std equals mean = score 0.5
    # CV of 2+ = very inconsistent = score ~0
    return max(0, min(1, 1 - (cv / 2)))


# ── factor 3: trend direction ─────────────────────────────────────────────

def trend_direction(values):
    """
    Fit a simple linear regression to detect improving/declining trend.
    Returns slope normalised to -1..+1 range.
    """
    n = len(values)
    if n < 3:
        return 0
    # x = 0,1,2... where 0 = oldest, n-1 = newest
    xs = list(range(n))
    # values[0] = most recent, so reverse for regression
    ys = list(reversed(values))
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num   = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return 0
    slope = num / denom
    # Normalise: slope as % of mean per game
    norm = slope / max(abs(y_mean), 1)
    return max(-1, min(1, norm))


# ── factor 4: home / away split ───────────────────────────────────────────

def home_away_factor(game_logs, stat_key, is_home):
    """
    Compare player's home avg vs away avg for this stat.
    Returns a multiplier: >1 means favourable for the current game location.
    """
    home_vals = [g[stat_key] for g in game_logs if stat_key in g and g.get("home")]
    away_vals = [g[stat_key] for g in game_logs if stat_key in g and not g.get("home")]

    if not home_vals or not away_vals:
        return 1.0   # not enough split data

    home_avg = statistics.mean(home_vals)
    away_avg = statistics.mean(away_vals)
    overall  = statistics.mean(home_vals + away_vals)

    if overall == 0:
        return 1.0

    relevant_avg = home_avg if is_home else away_avg
    return relevant_avg / overall   # e.g. 1.12 = 12% better at home


# ── factor 5: matchup rating ──────────────────────────────────────────────

def matchup_rating(opponent_def_stats, stat_key, league):
    """
    Compare opponent's defensive stats against league context.
    opponent_def_stats: dict of stat averages allowed by the opponent's defence.
    Returns 1-5 rating (5 = great matchup for the player).

    Since we don't have true defensive rankings from ESPN for free,
    we derive a proxy from the opponent's season defensive averages
    stored in data.json (fetched from ESPN team stats endpoint).
    """
    allowed = opponent_def_stats.get(stat_key)
    if allowed is None:
        return 3   # neutral default

    # Rough league average baselines per stat
    baselines = {
        "pass_yds": 240, "pass_td": 1.6, "rec_yds": 65,
        "rec": 5.0, "rush_yds": 110, "rush_td": 0.8,
        "rec_td": 0.45, "targets": 6.5,
    }
    baseline = baselines.get(stat_key, 1)
    if baseline == 0:
        return 3

    ratio = allowed / baseline  # >1 = defence allows more than average = good matchup
    if ratio >= 1.25:  return 5
    if ratio >= 1.10:  return 4
    if ratio >= 0.90:  return 3
    if ratio >= 0.75:  return 2
    return 1


# ── main model ─────────────────────────────────────────────────────────────

def score_prop(prop, player, game):
    """
    Full model scoring for a single prop.
    Enriches the prop dict in-place and returns it.
    """
    stat_key = prop["stat_key"]
    line     = prop["line"]
    values   = prop["trend"]          # list of recent values, newest first
    reasons  = []

    if not values:
        prop.update({"model_prob": 0.5, "confidence": 30, "grade": "D",
                     "verdict": "pass", "ev_pct": 0, "kelly_pct": 0,
                     "reasons": ["Insufficient data"]})
        return prop

    # ── 1. weighted average ──
    w_avg = weighted_avg(values)
    base_prob = 0.5 + (w_avg - line) / max(abs(line), 1) * 0.4
    base_prob = max(0.1, min(0.9, base_prob))

    # ── 2. consistency ──
    consist = consistency_score(values)
    # Consistent players: probability moves closer to base
    # Inconsistent: regress toward 0.5
    model_prob = base_prob * consist + 0.5 * (1 - consist)

    if consist > 0.75:
        reasons.append(f"Consistent performer (CV {consist:.0%})")
    elif consist < 0.4:
        reasons.append("High variance — boom/bust risk")

    # ── 3. trend direction ──
    trend = trend_direction(values)
    trend_adj = trend * 0.06   # max ±6% probability shift
    model_prob += trend_adj
    if trend > 0.15:
        reasons.append(f"Trending up over last {len(values)} games")
    elif trend < -0.15:
        reasons.append("Trending down recently")

    # ── 4. home/away ──
    is_home   = player.get("team") == game.get("home")
    ha_factor = home_away_factor(player.get("game_logs", []), stat_key, is_home)
    ha_adj    = (ha_factor - 1) * 0.15   # scale effect
    model_prob += ha_adj
    if ha_factor > 1.08:
        loc = "home" if is_home else "away"
        reasons.append(f"Strong {loc} performer (+{(ha_factor-1)*100:.0f}% vs average)")
    elif ha_factor < 0.92:
        loc = "home" if is_home else "away"
        reasons.append(f"Weaker {loc} performer ({(ha_factor-1)*100:.0f}% vs average)")

    # ── 5. injury ──
    inj_status = player.get("injury_status", "Active")
    inj_mult   = INJURY_WEIGHT.get(inj_status, 1.0)
    if inj_mult < 1.0:
        reasons.append(f"Injury concern: {inj_status} ({player.get('injury_detail','')})")
    # Regress toward 0.5 based on injury severity
    model_prob = model_prob * inj_mult + 0.5 * (1 - inj_mult)

    # ── 6. weather ──
    weather = game.get("weather")
    if weather and stat_key in PASS_STATS:
        wind = weather.get("wind_mph", 0)
        rain = weather.get("precip_in", 0)
        penalty = 0
        for threshold, pen in sorted(WIND_PASS_PENALTY.items()):
            if wind >= threshold:
                penalty = pen
        if rain > 0.1:
            penalty += PRECIP_PASS_PENALTY
        if penalty > 0:
            model_prob -= penalty
            reasons.append(f"Weather penalty: {wind}mph wind / {rain}\" precip")

    # ── 7. matchup ──
    opp_def = game.get("opponent_def_stats", {})
    m_rating = matchup_rating(opp_def, stat_key, game.get("league",""))
    matchup_adj = (m_rating - 3) * 0.025   # ±5% for best/worst matchup
    model_prob += matchup_adj
    prop["matchup_rating"] = m_rating
    if m_rating >= 4:
        reasons.append(f"Favourable matchup (rating {m_rating}/5)")
    elif m_rating <= 2:
        reasons.append(f"Tough matchup (rating {m_rating}/5)")

    # clamp
    model_prob = max(0.05, min(0.95, model_prob))

    # ── line value / EV ──
    over_odds  = prop.get("over_odds")  or DEFAULT_OVER_ODDS
    under_odds = prop.get("under_odds") or DEFAULT_UNDER_ODDS
    implied    = american_to_prob(over_odds)
    ev         = prob_to_ev(model_prob, over_odds)
    ev_pct     = round(ev * 100, 1)
    kelly      = kelly_fraction(model_prob, over_odds)

    # ── hit rate from weighted prob, not raw count ──
    raw_hits     = sum(1 for v in values if v > line)
    raw_hit_rate = round(raw_hits / len(values) * 100)

    # ── confidence score (0-100) ──
    # Combines: model_prob distance from 0.5, consistency, sample size
    prob_conf    = abs(model_prob - 0.5) * 2 * 100   # 0-100
    sample_conf  = min(1, len(values) / 10)           # penalise small samples
    confidence   = round(prob_conf * 0.6 + consist * 100 * 0.3 + sample_conf * 100 * 0.1)
    confidence   = int(max(0, min(100, confidence * inj_mult)))

    grade   = grade_from_confidence(confidence)
    verdict = verdict_from_confidence(confidence, ev_pct)

    # human-readable hit rate reason
    if raw_hit_rate >= 70:
        reasons.append(f"Hit rate {raw_hit_rate}% in last {len(values)} games")
    elif raw_hit_rate <= 40:
        reasons.append(f"Only hit {raw_hit_rate}% in last {len(values)} games")

    # weighted avg vs line
    diff = round(w_avg - line, 1)
    if diff > 0:
        reasons.append(f"Weighted avg {w_avg:.1f} is +{diff} above the line")
    else:
        reasons.append(f"Weighted avg {w_avg:.1f} is {diff} below the line")

    prop.update({
        "model_prob":    round(model_prob, 4),
        "implied_prob":  round(implied, 4),
        "ev_pct":        ev_pct,
        "kelly_pct":     kelly,
        "confidence":    confidence,
        "grade":         grade,
        "verdict":       verdict,
        "hit_rate":      raw_hit_rate,
        "weighted_avg":  round(w_avg, 1),
        "trend_dir":     round(trend, 3),
        "consistency":   round(consist, 3),
        "matchup_rating": m_rating,
        "reasons":       reasons[:5],   # top 5 reasons
    })

    return prop


# ── fetch opponent defensive stats from ESPN ──────────────────────────────

def fetch_def_stats(espn_league, team_id):
    """
    Pull team defensive stats (points/yards allowed) from ESPN.
    We use these as a proxy for matchup quality.
    Returns dict of stat_key -> avg allowed per game.
    """
    import requests, time
    headers = {"User-Agent": "Mozilla/5.0 (compatible; balliq/1.0)"}
    url = (f"https://sports.core.api.espn.com/v2/sports/football"
           f"/leagues/{espn_league}/teams/{team_id}/statistics")
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        time.sleep(0.3)
        data = r.json()
    except Exception as e:
        print(f"  [warn] def stats failed for team {team_id}: {e}")
        return {}

    out = {}
    splits = data.get("splits", {}).get("categories", [])
    for cat in splits:
        cat_name = cat.get("name","")
        names  = cat.get("names", [])
        values = cat.get("totals", [])   # season totals; we'll divide by games
        games_played = None
        # find games played to convert totals to per-game
        for i, n in enumerate(names):
            if n in ("games", "gamesPlayed"):
                try:
                    games_played = float(values[i])
                except (IndexError, TypeError, ValueError):
                    pass
                break

        if not games_played:
            games_played = 17   # assume full season

        stat_map = {
            "passing": {
                "passingYardsAllowed": "pass_yds",
                "passingTouchdownsAllowed": "pass_td",
            },
            "rushing": {
                "rushingYardsAllowed": "rush_yds",
                "rushingTouchdownsAllowed": "rush_td",
            },
            "receiving": {
                "receivingYardsAllowed": "rec_yds",
                "receptionsAllowed": "rec",
                "receivingTouchdownsAllowed": "rec_td",
            },
        }
        if cat_name in stat_map:
            for espn_key, friendly in stat_map[cat_name].items():
                if espn_key in names:
                    idx = names.index(espn_key)
                    try:
                        out[friendly] = round(float(values[idx]) / games_played, 1)
                    except (IndexError, TypeError, ValueError):
                        pass
    return out


# ── run enrichment ─────────────────────────────────────────────────────────

def enrich(data_path="data.json"):
    print("balliq model — enriching props…")
    with open(data_path) as f:
        data = json.load(f)

    espn_map = {"NFL": "nfl", "NCAAF": "college-football"}

    for game in data.get("games", []):
        espn_league = espn_map.get(game.get("league","NFL"), "nfl")

        # Fetch defensive stats for both teams so we can rate matchups
        # home team defends against away players, away team defends against home players
        home_def = fetch_def_stats(espn_league, game.get("home_id",""))
        away_def = fetch_def_stats(espn_league, game.get("away_id",""))

        for player in game.get("players", []):
            # Opponent defence is the other team
            is_home = player.get("team") == game.get("home")
            opp_def = away_def if is_home else home_def
            game["opponent_def_stats"] = opp_def

            for prop in player.get("props", []):
                score_prop(prop, player, game)

        # Clean up temp key
        game.pop("opponent_def_stats", None)

    data["model_updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(data_path, "w") as f:
        json.dump(data, f, indent=2)

    total_props = sum(
        len(p.get("props", []))
        for g in data["games"]
        for p in g.get("players", [])
    )
    print(f"  ✅  {total_props} props enriched across {len(data['games'])} games")


if __name__ == "__main__":
    enrich()
