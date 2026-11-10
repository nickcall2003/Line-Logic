"""
TheSportsDB (free API) — player cutouts + team venue/fanart for the share cards.

- player cutout (strCutout / strRender): transparent player image for the hero.
- team fanart / stadium thumb: a real venue/action backdrop for the hero.

Best-effort and cached for a week; callers fall back to the ESPN headshot and
the drawn spotlight when these aren't available. Free tier uses key "3"; set
SPORTSDB_KEY (a Patreon key) to raise limits and coverage.
"""
import os
import time

_KEY = os.environ.get("SPORTSDB_KEY", "3")
_TTL = 7 * 24 * 3600
_cache = {}          # "name|sport" -> (ts, assets_dict)
_team_cache = {}     # idTeam -> (ts, {fanart, stadium})

_SPORT_NAME = {
    "nfl": "American Football", "ncaaf": "American Football",
    "nba": "Basketball", "wnba": "Basketball",
    "ncaab": "Basketball", "ncaabb": "Basketball", "wncaab": "Basketball",
    "nhl": "Ice Hockey", "mlb": "Baseball",
}


def _get(url, params):
    import httpx
    return httpx.get(url, params=params, timeout=8, headers={"User-Agent": "Mozilla/5.0"})


def _team_art(id_team):
    if not id_team:
        return {}
    hit = _team_cache.get(id_team)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    art = {}
    try:
        tr = _get("https://www.thesportsdb.com/api/v1/json/%s/lookupteam.php" % _KEY,
                  {"id": id_team})
        if tr.status_code == 200:
            t = ((tr.json() or {}).get("teams") or [{}])[0] or {}
            art = {
                "fanart": t.get("strFanart1") or t.get("strFanart2") or t.get("strFanart3"),
                "stadium": t.get("strStadiumThumb"),
            }
    except Exception:
        art = {}
    _team_cache[id_team] = (time.time(), art)
    return art


def player_assets(name, sport=""):
    """{'cutout','render','team','fanart','stadium'} — best-effort, cached."""
    if not name:
        return {}
    ck = str(name).lower() + "|" + str(sport or "").lower()
    hit = _cache.get(ck)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    out = {}
    try:
        r = _get("https://www.thesportsdb.com/api/v1/json/%s/searchplayers.php" % _KEY,
                 {"p": name})
        if r.status_code == 200:
            players = (r.json() or {}).get("player") or []
            want = _SPORT_NAME.get(str(sport or "").lower())
            cands = [p for p in players if p.get("strCutout") or p.get("strRender")]
            if want:
                same = [p for p in cands if p.get("strSport") == want]
                if same:
                    cands = same
            if not cands and players:          # team art even if no cutout
                cands = [p for p in players if (not want or p.get("strSport") == want)] or players
            if cands:
                cands.sort(key=lambda p: (1 if p.get("strCutout") else 0), reverse=True)
                best = cands[0]
                out = {"cutout": best.get("strCutout"), "render": best.get("strRender"),
                       "team": best.get("strTeam")}
                out.update(_team_art(best.get("idTeam")))
    except Exception:
        out = {}
    _cache[ck] = (time.time(), out)
    return out


def player_cutout(name, sport=""):
    a = player_assets(name, sport) or {}
    return a.get("cutout") or a.get("render")
