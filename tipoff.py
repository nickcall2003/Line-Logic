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
    """Most-recent completed game ids for a team, newest first."""
    import espn_provider as EP
    if sport not in EP.SCOREBOARD or not team_id:
        return []
    base = EP.SCOREBOARD[sport].rsplit("/scoreboard", 1)[0]
    ids = []
    try:
        data = EP._get(f"{base}/teams/{team_id}/schedule", {}) or {}
        for ev in reversed(data.get("events") or []):
            comp = (ev.get("competitions") or [{}])[0]
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            gid = ev.get("id") or comp.get("id")
            if gid:
                ids.append(str(gid))
            if len(ids) >= limit:
                break
    except Exception:
        pass
    return ids


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


def _opening_tip_winner(sport, game_id):
    """The player who won the opening tip of one game, or None."""
    import espn_provider as EP
    key = (sport, str(game_id))
    c = _tip_cache.get(key)
    if c and time.time() - c[0] < _TIP_TTL:
        return c[1]
    winner = None
    try:
        data = EP._get(EP.SUMMARY[sport], {"event": str(game_id)}) or {}
        plays = data.get("plays") or (data.get("gamepackageJSON") or {}).get("plays") or []
        for pl in plays:
            typ = ((pl.get("type") or {}).get("text") or "")
            txt = pl.get("text") or ""
            if "jump ball" in (typ + " " + txt).lower():
                winner = _winner_from_text(txt)
                break
    except Exception:
        pass
    _tip_cache[key] = (time.time(), winner)
    return winner


def winrate(sport, team_id, player_name, limit=15):
    """{wins, total, rate} — real opening-tip win rate for a player over their
    team's recent games. rate is None when no tips were logged."""
    key = (sport, str(team_id), _norm(player_name))
    c = _rate_cache.get(key)
    if c and time.time() - c[0] < _RATE_TTL:
        return c[1]
    pn = _norm(player_name)
    wins = total = 0
    for gid in _recent_game_ids(sport, team_id, limit):
        w = _opening_tip_winner(sport, gid)
        if not w:
            continue
        total += 1
        wn = _norm(w)
        if wn == pn or (pn and (pn in wn or wn in pn)):
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
