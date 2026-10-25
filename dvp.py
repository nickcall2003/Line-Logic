"""
dvp.py — Defense vs Position tables, auto-generated from ESPN box scores.

For each defense, we walk the season's completed games and attribute every
opposing player's production to that defense, bucketed by the player's position
(QB/RB/WR/TE for the NFL). The result is the classic "what this defense allows
to each position" matchup table (targets, receptions, yards, TDs, fantasy
points), as season totals and per-game averages — exactly the kind of sheet
bettors build by hand.

All data comes from ESPN's free box-score summary feed (no paid quota). Heavy
work (one summary fetch per game) is cached; a finished game never changes.

This file starts with a PROBE so we can confirm ESPN's exact column labels and
whether player position is present in the box score before building the full
aggregator against the wrong field names.
"""
from __future__ import annotations

import datetime as dt
import re

import espn_provider as EP


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def nfl_season_year():
    """NFL season year: the league labels a season by the year it STARTS, so
    Jan–Jul belongs to the previous year's season."""
    today = dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def _recent_final_nfl_game_id():
    """Grab one recently-completed NFL game id from the current week's scoreboard,
    walking back a few weeks if the current week has no finals yet."""
    base = EP.SCOREBOARD["nfl"]
    yr = nfl_season_year()
    for params in ({}, {"dates": str(yr)}):
        try:
            data = EP._get(base, params) or {}
            for ev in data.get("events") or []:
                comp = (ev.get("competitions") or [{}])[0]
                if (((comp.get("status") or {}).get("type") or {}).get("completed")):
                    return str(ev.get("id"))
        except Exception:
            pass
    return None


def nfl_probe(game_id=None):
    """Dump one NFL game's box-score player groups so we can see the real labels
    (TGTS/REC/YDS/TD...), the group names (passing/rushing/receiving), and
    whether each athlete carries a position. Read-only, free feed."""
    gid = game_id or _recent_final_nfl_game_id()
    if not gid:
        return {"error": "no final NFL game id found to probe"}
    try:
        data = EP._get(EP.SUMMARY["nfl"], {"event": str(gid)}) or {}
    except Exception as e:
        return {"error": f"summary fetch failed: {str(e)[:150]}", "game_id": gid}
    out = {"game_id": gid, "teams": []}
    for team in ((data.get("boxscore") or {}).get("players") or []):
        tinfo = (team.get("team") or {})
        tentry = {"team": tinfo.get("displayName") or tinfo.get("abbreviation"),
                  "team_id": tinfo.get("id"), "groups": []}
        for grp in (team.get("statistics") or []):
            athletes = grp.get("athletes") or []
            sample = []
            for ath in athletes[:3]:
                a = ath.get("athlete") or {}
                sample.append({
                    "name": a.get("displayName"),
                    # is position present on the box-score athlete?
                    "position": ((a.get("position") or {}) or {}).get("abbreviation")
                    or ((a.get("position") or {}) or {}).get("name"),
                    "stats": ath.get("stats"),
                })
            tentry["groups"].append({
                "name": grp.get("name") or grp.get("type"),
                "labels": grp.get("labels"),
                "descriptions": grp.get("descriptions"),
                "keys": grp.get("keys"),
                "totals": grp.get("totals"),
                "sample_athletes": sample,
            })
        out["teams"].append(tentry)
    # also probe a roster so we know the position-map fallback works
    try:
        tid = (((data.get("boxscore") or {}).get("players") or [{}])[0]
               .get("team") or {}).get("id")
        rm = EP.team_roster_meta("nfl", tid) if tid else {}
        out["roster_sample"] = dict(list(rm.items())[:5])
        out["roster_count"] = len(rm)
    except Exception as e:
        out["roster_sample"] = {"error": str(e)[:120]}
    return out
