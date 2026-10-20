"""
tipoff.py — REAL opening-tip win rates from ESPN play-by-play.

stats.nba.com has jump-ball stats but blocks datacenter IPs. ESPN's game
summary feed (reachable from the server) records every game's opening jump ball
and who gained possession, so we derive each center's actual tip win rate from
their team's recent games. Heavy work (one summary fetch per game) is cached:
a settled game's tip never changes, and a player's rate is cached for a day.

Used to refine the First Basket model: real P(win tip) via a log5 combination
of the two centers' rates, replacing the height estimate when the sample is big
enough. Falls back to height when a center has too few logged tips (early
season, or an unparsed feed).
"""
from __future__ import annotations

import re
import time

_rate_cache = {}          # (sport, team_id, norm_player) -> (ts, dict)
_RATE_TTL = 24 * 3600
_tip_cache = {}           # (sport, game_id) -> (ts, winner_text_or_None)
_TIP_TTL = 14 * 24 * 3600  # a finished game's opening tip is immutable


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _recent_game_ids(sport, team_id, limit=15):
    """Most-recent completed game ids for a team, newest first. Walks back from
    the current season to last season so early-season (few games played yet)
    still has real tips to count."""
    import espn_provider as EP
    import datetime as _dt
    if sport not in EP.SCOREBOARD or not team_id:
        return []
    base = EP.SCOREBOARD[sport].rsplit("/scoreboard", 1)[0]
    yr = _dt.date.today().year
    ids = []
    for params in ({}, {"season": yr}, {"season": yr - 1}):
        try:
            data = EP._get(f"{base}/teams/{team_id}/schedule", params) or {}
            for ev in reversed(data.get("events") or []):
                comp = (ev.get("competitions") or [{}])[0]
                if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                    continue
                gid = ev.get("id") or comp.get("id")
                if gid and str(gid) not in ids:
                    ids.append(str(gid))
        except Exception:
            pass
        if len(ids) >= max(6, limit // 2):
            break
    return ids[:limit]


def _winner_from_text(text):
    """Pull the possession-gaining player from an ESPN jump-ball play text, e.g.
    'Victor Wembanyama vs. Chet Holmgren (Victor Wembanyama gains possession)'."""
    t = text or ""
    m = re.search(r"\(([^)]+?)\s+gains possession", t, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"won by\s+([A-Za-z.\-' ]+)", t, re.I)
    if m:
        return m.group(1).strip()
    return None


def _opening_tip(sport, game_id):
    """(winning_team_id, possession_player) for the opening tip, or (None, None).
    ESPN's jump-ball play is attributed to the team that GAINED possession, so
    the play's team is the tip winner (the possession player is often a teammate
    the ball was tipped to, not the jumper)."""
    import espn_provider as EP
    key = (sport, str(game_id))
    c = _tip_cache.get(key)
    if c and time.time() - c[0] < _TIP_TTL:
        return c[1]
    res = (None, None)
    try:
        data = EP._get(EP.SUMMARY[sport], {"event": str(game_id)}) or {}
        plays = data.get("plays") or (data.get("gamepackageJSON") or {}).get("plays") or []
        for pl in plays:
            blob = (((pl.get("type") or {}).get("text") or "") + " "
                    + (pl.get("text") or "")).lower()
            if "jump ball" in blob or "jumpball" in blob:
                tid = ((pl.get("team") or {}) or {}).get("id")
                res = (str(tid) if tid is not None else None,
                       _winner_from_text(pl.get("text") or ""))
                break
    except Exception:
        pass
    _tip_cache[key] = (time.time(), res)
    return res


def winrate(sport, team_id, player_name=None, limit=15):
    """{wins, total, rate} — real opening-tip win rate for a TEAM over its recent
    games (the same starting center jumps, so team tip-win ≈ that center's).
    rate is None when no tips were logged."""
    key = (sport, str(team_id))
    c = _rate_cache.get(key)
    if c and time.time() - c[0] < _RATE_TTL:
        return c[1]
    wins = total = 0
    for gid in _recent_game_ids(sport, team_id, limit):
        wtid, _name = _opening_tip(sport, gid)
        if not wtid:
            continue
        total += 1
        if str(wtid) == str(team_id):
            wins += 1
    res = {"wins": wins, "total": total,
           "rate": (round(wins / total, 3) if total else None)}
    _rate_cache[key] = (time.time(), res)
    return res


def log5(x, y):
    """P(A beats B) from each one's independent win rate (Bill James log5)."""
    if x is None or y is None:
        return None
    d = x + y - 2 * x * y
    return round((x - x * y) / d, 4) if d else 0.5
