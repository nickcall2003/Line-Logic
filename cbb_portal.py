"""
cbb_portal.py — NCAAB transfer-portal board, scored on PRODUCTION.

Mirror of cfb_portal.py for college basketball, using the CollegeBasketballData
API (CBBD_API_KEY). Ranks teams by the real value of who they gained and lost in
the portal, so a team that landed a 15-win-share wing grades out ahead of one
that added three end-of-bench bodies.

Endpoints (CBBD, https://api.collegebasketballdata.com, Bearer auth):
  * /recruiting/portal?year=Y     every portal entry (origin, destination, stars, rating)
  * /stats/player/season?season=S each player's production last season (win shares,
                                  porpag, usage, minutes, points…)
  * /teams?season=S               the team universe + logos/colors/conference

A transfer's value = MAX(proven production, recruiting talent). Proven
production comes from last season's win_shares (a clean "wins contributed"
value); unproven incomings (freshmen/JUCO with no D1 line) fall back to a
star-based talent estimate. Per team: value gained (incoming) minus value lost
(outgoing) = net portal haul, and teams are ranked by it.

The CBBD "year" convention for the portal is resolved at build time by trying a
few candidate years and keeping the one that returns entries, so this works
regardless of whether the API tags the spring-2026 cycle as 2026 or 2027.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

BASE = "https://api.collegebasketballdata.com"
OUT = "/data/cbb_portal.json"
_FALLBACK = "cbb_portal.json"
_cache = {"ts": 0.0, "data": None}
_TTL = 12 * 3600

# star -> win-share-equivalent talent value for unproven incomings
_STAR_VALUE = {5: 6.0, 4: 3.5, 3: 1.5, 2: 0.8, 1: 0.5, 0: 0.5}


def _key():
    return (os.environ.get("CBBD_API_KEY") or os.environ.get("CBB_API_KEY") or "").strip()


def _get(path, params=None):
    import httpx
    k = _key()
    if not k:
        raise RuntimeError("CBBD_API_KEY not set")
    r = httpx.get(BASE + path, params=params or {},
                  headers={"Authorization": "Bearer " + k, "Accept": "application/json"},
                  timeout=25)
    r.raise_for_status()
    return r.json() or []


def _get_safe(path, params=None):
    try:
        return _get(path, params) or []
    except Exception as e:
        print(f"[cbb_portal] {path} {params}: {e}")
        return []


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _g(d, *keys):
    """First present key — the raw CBBD JSON is camelCase (firstName, winShares,
    PORPAG, displayName, primaryColor); we accept snake_case too for safety."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return None


def _default_years():
    """Candidate portal years, most likely first. The 2026 spring cycle feeds the
    2026-27 season; CBBD may tag it 2026 (calendar) or 2027 (season-ending)."""
    today = dt.date.today()
    end_year = today.year + 1 if today.month >= 7 else today.year
    out = []
    for y in (end_year, today.year, end_year - 1, today.year - 1):
        if y not in out:
            out.append(y)
    return out


def _team_name(o):
    """origin/destination come back as {name, conference, id} objects (or null)."""
    if isinstance(o, dict):
        return o.get("name")
    return o if isinstance(o, str) else None


def _team_conf(o):
    return o.get("conference") if isinstance(o, dict) else None


def _player_value_index(seasons):
    """norm(name) -> best production value (win shares) across the given seasons,
    plus position/team context. Most recent non-null wins."""
    idx = {}
    for season in seasons:
        rows = _get_safe("/stats/player/season", {"season": season})
        for p in rows:
            nm = _norm(_g(p, "name"))
            if not nm:
                continue
            # winShares is an object {offensive, defensive, total, totalPer40}
            ws_raw = _g(p, "winShares", "win_shares")
            ws = _num(ws_raw.get("total")) if isinstance(ws_raw, dict) else _num(ws_raw)
            if ws is None:
                por = _num(_g(p, "PORPAG", "porpag"))
                g = _num(_g(p, "games")) or 25.0
                ws = (por * g / 40.0) if por is not None else None
            if ws is None:
                continue
            cur = idx.get(nm)
            # prefer the later season's line (seasons passed newest-first keeps first)
            if cur is None:
                idx[nm] = {"ws": ws, "pts": _num(_g(p, "points")),
                           "min": _num(_g(p, "minutes")), "usage": _num(_g(p, "usage")),
                           "pos": _g(p, "position") or "", "team": _g(p, "team") or ""}
    return idx


def _transfer_value(t, pidx):
    """MAX(proven production, talent). Returns (value, basis)."""
    nm = _norm((_g(t, "firstName", "first_name") or "") + (_g(t, "lastName", "last_name") or ""))
    if not nm:
        nm = _norm(_g(t, "name", "fullName", "displayName", "player"))
    prod = None
    st = pidx.get(nm)
    if st and st.get("ws") is not None:
        prod = round(st["ws"], 2)
    stars = int(_g(t, "stars") or 0)
    talent = _STAR_VALUE.get(stars, 0.5)
    rating = _num(_g(t, "rating"))
    if rating and rating > 0:
        # CBBD rating ~0.80-1.00 composite for blue-chips; scale into WS-equiv
        talent = max(talent, round((rating - 0.75) * 24, 2)) if rating > 0.75 else talent
    if prod is not None and prod >= talent:
        return prod, "production"
    return talent, ("talent" if prod is None else "talent>production")


def build(year=None):
    """Fetch the portal + prior production, value every move, aggregate by team,
    rank by net value. Persists the result. Returns the board dict."""
    years = [int(year)] if year else _default_years()
    portal, used_year = [], None
    for y in years:
        rows = _get_safe("/recruiting/portal", {"year": y})
        if rows:
            portal, used_year = rows, y
            break
    if used_year is None:
        used_year = years[0]
    # value players by last season's and the season-of production (robust to convention)
    pidx = _player_value_index([used_year, used_year - 1])
    # team universe for names/logos/colors
    teams = {}
    for t in _get_safe("/teams", {"season": used_year}):
        rec = {
            "team": _g(t, "school", "displayName", "display_name") or "",
            "abbr": _g(t, "abbreviation") or "",
            "conference": _g(t, "conference") or "",
            "logo": (_g(t, "logos") or [None])[0],
            "color": _g(t, "primaryColor", "primary_color")}
        # index by several name forms so the portal's team name matches
        for nm in (_g(t, "school"), _g(t, "displayName", "display_name"),
                   _g(t, "shortDisplayName", "short_display_name")):
            if nm:
                teams.setdefault(_norm(nm), rec)

    def _team_rec(name, conf=None):
        key = _norm(name)
        base = teams.get(key)
        if base is None:
            base = {"team": name or "Unknown", "abbr": "", "conference": conf or "",
                    "logo": None, "color": None}
        return key, base

    agg = {}
    n_valued = 0
    for t in portal:
        player = ((_g(t, "firstName", "first_name") or "") + " "
                  + (_g(t, "lastName", "last_name") or "")).strip()
        if not player:
            player = (_g(t, "name", "fullName", "displayName", "player") or "").strip()
        pos = _g(t, "position") or ""
        stars = int(_g(t, "stars") or 0)
        val, basis = _transfer_value(t, pidx)
        n_valued += 1
        dest = _team_name(_g(t, "destination"))
        orig = _team_name(_g(t, "origin"))
        # withdrew / returned to the same school -> net-zero noise, skip it
        if dest and orig and _norm(dest) == _norm(orig):
            continue
        # incoming to destination
        if dest:
            k, base = _team_rec(dest, _team_conf(_g(t, "destination")))
            r = agg.setdefault(k, {**base, "gained": 0.0, "lost": 0.0, "in": [], "out": []})
            r["gained"] += val
            r["in"].append({"player": player, "pos": pos, "from": orig or "—",
                            "value": round(val, 2), "stars": stars, "basis": basis})
        # outgoing from origin
        if orig:
            k, base = _team_rec(orig, _team_conf(_g(t, "origin")))
            r = agg.setdefault(k, {**base, "gained": 0.0, "lost": 0.0, "in": [], "out": []})
            r["lost"] += val
            r["out"].append({"player": player, "pos": pos, "to": dest or "Portal",
                             "value": round(val, 2), "stars": stars})
    rows = []
    for r in agg.values():
        r["in"].sort(key=lambda x: -x["value"])
        r["out"].sort(key=lambda x: -x["value"])
        r["net"] = round(r["gained"] - r["lost"], 2)
        r["gained"] = round(r["gained"], 2)
        r["lost"] = round(r["lost"], 2)
        r["in"] = r["in"][:8]
        r["out"] = r["out"][:8]
        rows.append(r)
    rows.sort(key=lambda x: -x["net"])
    data = {"sport": "ncaab", "source": "CollegeBasketballData",
            "season": used_year, "built_at": dt.datetime.utcnow().isoformat() + "Z",
            "count_entries": len(portal), "count_valued": n_valued,
            "teams": rows}
    _cache["data"] = data
    _cache["ts"] = time.time()
    try:
        path = OUT if os.path.isdir(os.path.dirname(OUT)) else _FALLBACK
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[cbb_portal] save failed: {e}")
    return data


def _load_persisted():
    for path in (OUT, _FALLBACK):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


_building = {"on": False}


def _bg_build():
    try:
        build()
    except Exception as e:
        print(f"[cbb_portal] bg build failed: {e}")
    finally:
        _building["on"] = False


def board(force=False):
    """Cached board. The build walks a full season of player stats, so it runs in
    the BACKGROUND — a cold request returns {building:true} instead of hanging,
    and fills in once the build finishes. Returns needs_key if no API key."""
    if not _key():
        return {"error": "CBBD_API_KEY not set", "needs_key": True, "teams": []}
    c = _cache
    if not force and c["data"] and time.time() - c["ts"] < _TTL:
        return c["data"]
    disk = _load_persisted()
    if not force and disk:
        c["data"] = disk
        c["ts"] = time.time()
        return disk
    if force:
        return build()
    # need to build and nothing cached — kick it off in the background
    if not _building["on"]:
        _building["on"] = True
        import threading
        threading.Thread(target=_bg_build, daemon=True).start()
    return {"building": True, "teams": [], "season": _default_years()[0]}


def status():
    return {"has_key": bool(_key()),
            "cached": bool(_cache["data"]),
            "persisted": bool(_load_persisted()),
            "candidate_years": _default_years()}
