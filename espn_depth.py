"""
espn_depth.py — season team depth from ESPN standings (works from any server;
the app already runs on ESPN). One cached standings call per league gives every
team's for/against, differential, win%/points and record. Emits the same generic
shape soccer/UFC use, so the one frontend panel renders it.
"""
from __future__ import annotations

import os
import re
import time

_TTL = int(os.environ.get("ESPN_DEPTH_TTL", "21600"))   # 6h
_NEG_TTL = 900
_cache = {}
_last = {"url": None, "status": None, "bytes": 0, "error": None}

_PATH = {
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "nfl": ("football", "nfl"),
    "nhl": ("hockey", "nhl"),
    "mlb": ("baseball", "mlb"),
    "ncaaf": ("football", "college-football"),
    "ncaab": ("basketball", "mens-college-basketball"),
    "ncaabb": ("basketball", "mens-college-basketball"),
    "wncaab": ("basketball", "womens-college-basketball"),
}

# per sport: (stats candidates, label, lower_better, mode) ; mode: pg|pct|None
_BARS = {
    "_pts": [
        (["avgPointsFor", "pointsFor"], "Points / game", False, "pg"),
        (["avgPointsAgainst", "pointsAgainst"], "Points allowed / game", True, "pg"),
        (["pointDifferential", "differential"], "Point differential", False, None),
        (["winPercent", "leagueWinPercent"], "Win %", False, "pct"),
    ],
    "_goals": [
        (["avgPointsFor", "pointsFor"], "Goals / game", False, "pg"),
        (["avgPointsAgainst", "pointsAgainst"], "Goals against / game", True, "pg"),
        (["pointDifferential", "differential"], "Goal differential", False, None),
        (["points"], "Points", False, None),
    ],
    "_runs": [
        (["avgPointsFor", "pointsFor"], "Runs / game", False, "pg"),
        (["avgPointsAgainst", "pointsAgainst"], "Runs allowed / game", True, "pg"),
        (["pointDifferential", "differential"], "Run differential", False, None),
        (["winPercent", "leagueWinPercent"], "Win %", False, "pct"),
    ],
}
_SPORT_BARS = {"nba": "_pts", "wnba": "_pts", "nfl": "_pts", "ncaaf": "_pts",
               "ncaab": "_pts", "ncaabb": "_pts", "wncaab": "_pts",
               "nhl": "_goals", "soccer": "_goals", "mlb": "_runs"}

_FILLER = {"fc", "sc", "the"}
_ALIAS = {"utd": "united"}

# Leagues whose site.api v2 standings are unreliable (college football returns
# placeholder/split entries with no per-game or win% fields). cdn.espn core
# carries clean season totals + the overall record, one entry per team.
_CORE_FEED = {"ncaaf"}


def enabled() -> bool:
    return os.environ.get("ESPN_DEPTH_ENABLED", "1") == "1"


def _get(url):
    try:
        import httpx
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        _last.update(url=url, status=r.status_code, bytes=len(r.text), error=None)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _last.update(url=url, error=str(e))
        print(f"[espn_depth] GET failed {url}: {e}")
        return None


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _tokens(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace("&", " and ").replace(".", " ").replace("-", " "))
    return {(_ALIAS.get(t, t)) for t in n.split() if t not in _FILLER and len(t) >= 2}


def _parse_record(s):
    """Pull (wins, losses, ties) from a team's record displayValues. Primary:
    the 'overall' display ('8-2'). Fallback: sum Home + Away/Road splits."""
    import re as _re
    for key in ("overall__d", "total__d"):
        m = _re.match(r"\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?", str(s.get(key) or ""))
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    w = l = t = 0
    found = False
    for key in ("Home__d", "Away__d", "Road__d"):
        m = _re.match(r"\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?", str(s.get(key) or ""))
        if m:
            found = True
            w += int(m.group(1)); l += int(m.group(2)); t += int(m.group(3) or 0)
    return (w, l, t) if found else None


def _gp_robust(stats):
    gp = _num(stats.get("gamesPlayed"))
    if gp:
        return gp
    w, l, t = _num(stats.get("wins")), _num(stats.get("losses")), _num(stats.get("ties"))
    if w is not None and l is not None:
        return w + l + (t or 0)
    rec = _parse_record(stats)
    if rec:
        g = rec[0] + rec[1] + rec[2]
        return g or None
    return None


def _enrich(stats):
    """Normalize a team's stats so the generic bar engine has per-game points,
    differential and win% regardless of which feed supplied them. Only fills
    gaps — existing per-game/win% values (NFL, NCAAB) are left untouched."""
    games = _gp_robust(stats)
    pf, pa = _num(stats.get("pointsFor")), _num(stats.get("pointsAgainst"))
    if stats.get("avgPointsFor") is None and pf is not None and games:
        stats["avgPointsFor"] = round(pf / games, 2)
    if stats.get("avgPointsAgainst") is None and pa is not None and games:
        stats["avgPointsAgainst"] = round(pa / games, 2)
    if stats.get("pointDifferential") is None and pf is not None and pa is not None:
        stats["pointDifferential"] = pf - pa
    wp = stats.get("winPercent")
    if wp in (None, 0, 0.0, "0", "0.0"):
        w = _num(stats.get("wins"))
        rec = _parse_record(stats)
        if w is None and rec:
            w = rec[0]
        if w is not None and games:
            stats["winPercent"] = round(w / games, 4)
    if games and stats.get("gamesPlayed") is None:
        stats["gamesPlayed"] = games


def _collect(node, acc):
    if isinstance(node, dict):
        if isinstance(node.get("entries"), list):
            for e in node["entries"]:
                if isinstance(e, dict) and e.get("team"):
                    acc.append(e)
        for v in node.values():
            _collect(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect(v, acc)


def _standings(sport, league=None):
    ck = f"{sport}|{league}"
    c = _cache.get(ck)
    if c:
        age = time.time() - c[0]
        if c[1] and age < _TTL:
            return c[1]
        if not c[1] and age < _NEG_TTL:
            return {}
    if sport == "soccer":
        if not league:
            return {}
        url = f"https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings"
    elif sport in _CORE_FEED:
        sp = _PATH.get(sport)
        if not sp:
            return {}
        url = f"https://cdn.espn.com/core/{sp[1]}/standings?xhr=1"
    else:
        sp = _PATH.get(sport)
        if not sp:
            return {}
        url = f"https://site.api.espn.com/apis/v2/sports/{sp[0]}/{sp[1]}/standings"
    data = _get(url)
    table = {}
    if data:
        acc = []
        _collect(data, acc)
        for e in acc:
            tm = e["team"]
            name = tm.get("displayName") or tm.get("name") or tm.get("shortDisplayName")
            if not name:
                continue
            stats = {}
            for s in e.get("stats", []):
                if s.get("name") is not None:
                    stats[s["name"]] = s.get("value")
                    stats[s["name"] + "__d"] = s.get("displayValue")
            _enrich(stats)
            table[name] = {"id": str(tm.get("id") or ""), "name": name, "stats": stats}
    _cache[ck] = (time.time(), table)
    return table


def _match(name, table):
    target = _tokens(name)
    if not target:
        return None
    best, score = None, 0
    for nm, rec in table.items():
        s = sum(len(t) for t in (target & _tokens(nm)))
        if s > score:
            score, best = s, rec
    return best if score >= 4 else None


def _gp(stats):
    return _gp_robust(stats)


def _val(stats, cand, mode):
    name = raw = None
    for k in cand:
        if stats.get(k) is not None:
            name, raw = k, _num(stats[k])
            break
    if raw is None:
        return None
    if mode == "pct":
        return round(raw * 100, 1)
    if mode == "pg":
        if name and name.lower().startswith("avg"):
            return round(raw, 2)          # ESPN already gives per-game
        gp = _gp(stats)
        return round(raw / gp, 2) if gp else round(raw, 2)
    return raw


def _bio(rec):
    s = rec["stats"]
    record = s.get("overall__d") or ""
    if not record:
        w, l, t = s.get("wins"), s.get("losses"), s.get("ties")
        if w is not None and l is not None:
            record = f"{int(_num(w))}-{int(_num(l))}"
            if t not in (None, "", 0) and _num(t):
                record += f"-{int(_num(t))}"
    out = []
    if record:
        out.append(["Record", record])
    l10 = s.get("Last Ten Games__d")
    if l10:
        out.append(["Last 10", l10])
    streak = s.get("streak__d")
    if streak:
        out.append(["Streak", streak])
    return out


def match_depth(sport, home_name, away_name, league=None):
    if not enabled():
        return None
    table = _standings(sport, league)
    if not table:
        return None
    h = _match(home_name, table)
    a = _match(away_name, table)
    if not h and not a:
        return None
    barcfg = _BARS.get(_SPORT_BARS.get(sport, "_pts"))
    bars = []
    for cand, label, lb, mode in barcfg:
        av = _val(a["stats"], cand, mode) if a else None
        hv = _val(h["stats"], cand, mode) if h else None
        if av is None and hv is None:
            continue
        bars.append({"label": label, "away": av, "home": hv, "lower_better": lb})
    if not bars:
        return None
    out = {
        "source": "ESPN",
        "away": {"name": (a["name"] if a else away_name), "bio": (_bio(a) if a else [])},
        "home": {"name": (h["name"] if h else home_name), "bio": (_bio(h) if h else [])},
        "bars": bars,
    }
    return out


def diag(sport, league=None):
    table = _standings(sport, league)
    first = list(table.values())[0] if table else None
    # Full name->value map for the sample team so we can see exactly what
    # names ESPN uses (college feeds differ from pro) and whether they're 0.
    sample_vals = {}
    if first:
        for k, v in first["stats"].items():
            if k.endswith("__d"):
                continue
            sample_vals[k] = v
    return {"enabled": enabled(), "sport": sport, "league": league,
            "teams": len(table),
            "sample_team": first["name"] if first else None,
            "sample_record": (_bio(first) if first else []),
            "sample_stats": sample_vals,
            "all_team_names": sorted(list(table.keys()))[:40],
            "fetch": _last}
