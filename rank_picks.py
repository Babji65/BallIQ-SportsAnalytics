"""
balliq — rank_picks.py

Reads enriched data.json, ranks every prop by expected value + confidence,
applies filters, and writes picks.json — a clean ranked list the frontend
uses for the "Top Picks" dashboard.

Filters applied before ranking
───────────────────────────────
- Player is not Out / IR
- Confidence >= 40 (D-grade props filtered out)
- At least 5 game logs available
- EV > 0 (positive expected value only)

Ranking formula
───────────────
score = (ev_pct * 0.45) + (confidence * 0.35) + (hit_rate * 0.20)

This weights EV most heavily (that's the money), then confidence
(how sure we are), then raw hit rate (historical backing).

Tiers
─────
ELITE  : score >= 75  — highest conviction, size up
STRONG : score >= 55  — solid play, standard size
LEAN   : score >= 40  — small play or monitor
"""

import json
from datetime import datetime, timezone

# ── config ─────────────────────────────────────────────────────────────────

MIN_CONFIDENCE  = 40
MIN_GAME_LOGS   = 5
MIN_EV          = 0.0          # minimum ev_pct to include
MAX_PICKS       = 20           # total picks to surface
MAX_PER_PLAYER  = 2            # don't flood picks with one player's props

WEIGHTS = {"ev": 0.45, "conf": 0.35, "hit": 0.20}

# ── helpers ────────────────────────────────────────────────────────────────

def rank_score(prop):
    ev   = max(0, prop.get("ev_pct", 0))
    conf = prop.get("confidence", 0)
    hit  = prop.get("hit_rate", 0)
    return round(ev * WEIGHTS["ev"] + conf * WEIGHTS["conf"] + hit * WEIGHTS["hit"], 2)

def tier(score):
    if score >= 75: return "ELITE"
    if score >= 55: return "STRONG"
    return "LEAN"

def tier_sort_key(t):
    return {"ELITE": 0, "STRONG": 1, "LEAN": 2}.get(t, 3)

# ── main ───────────────────────────────────────────────────────────────────

def build_picks(data_path="data.json", out_path="picks.json"):
    print("balliq picks — building ranked list…")

    with open(data_path) as f:
        data = json.load(f)

    candidates = []
    skipped    = 0

    for game in data.get("games", []):
        game_label = f"{game.get('away','')} @ {game.get('home','')} ({game.get('date','')})"

        for player in game.get("players", []):
            # hard filter: injured out/IR
            inj = player.get("injury_status","Active")
            if inj in ("Out", "IR"):
                skipped += 1
                continue

            logs_count = len(player.get("game_logs", []))

            for prop in player.get("props", []):
                # filters
                if logs_count < MIN_GAME_LOGS:
                    skipped += 1
                    continue
                if prop.get("confidence", 0) < MIN_CONFIDENCE:
                    skipped += 1
                    continue
                if prop.get("ev_pct", 0) < MIN_EV:
                    skipped += 1
                    continue
                if prop.get("verdict") == "pass":
                    skipped += 1
                    continue

                score = rank_score(prop)

                candidates.append({
                    # identification
                    "player_id":    player.get("id"),
                    "player_name":  player.get("name"),
                    "player_pos":   player.get("pos"),
                    "player_team":  player.get("team"),
                    "player_initials": player.get("initials"),
                    "injury_status": inj,

                    # game context
                    "game":         game_label,
                    "game_id":      game.get("id"),
                    "league":       game.get("league"),
                    "game_date":    game.get("date"),
                    "weather":      game.get("weather"),

                    # prop details
                    "prop_type":    prop.get("type"),
                    "stat_key":     prop.get("stat_key"),
                    "line":         prop.get("line"),
                    "direction":    "OVER",   # our model scores the over by default
                    "over_odds":    prop.get("over_odds") or -110,
                    "under_odds":   prop.get("under_odds") or -110,

                    # model outputs
                    "model_prob":   prop.get("model_prob"),
                    "implied_prob": prop.get("implied_prob"),
                    "ev_pct":       prop.get("ev_pct"),
                    "kelly_pct":    prop.get("kelly_pct"),
                    "confidence":   prop.get("confidence"),
                    "grade":        prop.get("grade"),
                    "verdict":      prop.get("verdict"),
                    "hit_rate":     prop.get("hit_rate"),
                    "weighted_avg": prop.get("weighted_avg"),
                    "trend_dir":    prop.get("trend_dir"),
                    "consistency":  prop.get("consistency"),
                    "matchup_rating": prop.get("matchup_rating"),
                    "reasons":      prop.get("reasons", []),
                    "trend":        prop.get("trend", []),

                    # ranking
                    "rank_score":   score,
                    "tier":         tier(score),
                })

    # Sort by tier then score
    candidates.sort(key=lambda x: (tier_sort_key(x["tier"]), -x["rank_score"]))

    # Cap per player (don't show 4 props from the same player)
    player_counts = {}
    picks = []
    for c in candidates:
        pid = c["player_id"]
        if player_counts.get(pid, 0) >= MAX_PER_PLAYER:
            continue
        player_counts[pid] = player_counts.get(pid, 0) + 1
        picks.append(c)
        if len(picks) >= MAX_PICKS:
            break

    # Add rank number
    for i, pick in enumerate(picks, 1):
        pick["rank"] = i

    output = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "total_analyzed": len(candidates) + skipped,
        "total_skipped":  skipped,
        "picks_count":    len(picks),
        "picks":          picks,

        # Summary stats
        "summary": {
            "elite_count":  sum(1 for p in picks if p["tier"] == "ELITE"),
            "strong_count": sum(1 for p in picks if p["tier"] == "STRONG"),
            "lean_count":   sum(1 for p in picks if p["tier"] == "LEAN"),
            "avg_ev":       round(sum(p["ev_pct"] for p in picks) / max(len(picks),1), 1),
            "avg_conf":     round(sum(p["confidence"] for p in picks) / max(len(picks),1), 1),
        }
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n  📊 Analyzed: {output['total_analyzed']} props")
    print(f"  ✅ Picks:    {len(picks)} surfaced")
    print(f"  🏆 Elite:   {output['summary']['elite_count']}")
    print(f"  💪 Strong:  {output['summary']['strong_count']}")
    print(f"  👀 Lean:    {output['summary']['lean_count']}")
    print(f"  📈 Avg EV:  +{output['summary']['avg_ev']}%")

    if picks:
        top = picks[0]
        print(f"\n  Top pick: {top['player_name']} {top['prop_type']} "
              f"(conf {top['confidence']}, EV +{top['ev_pct']}%)")


if __name__ == "__main__":
    build_picks()
