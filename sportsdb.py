"""
TheSportsDB (free API) — player cutout images for the share cards.

Returns a transparent player cutout URL (strCutout) when available, which
composites far better on the hero than a square headshot. Best-effort and
cached for a week; callers fall back to the ESPN headshot, then to initials.

Free tier uses key "3". Set SPORTSDB_KEY (e.g. a Patreon key) to raise limits
and coverage.
"""
import os
import time

_KEY = os.environ.get("SPORTSDB_KEY", "3")
_TTL = 7 * 24 * 3600
_cache = {}          # "name|sport" -> (ts, url_or_None)

# our sport code -> TheSportsDB strSport label, to disambiguate common names
_SPORT_NAME = {
    "nfl": "American Football", "ncaaf": "American Football",
    "nba": "Basketball", "wnba": "Basketball",
    "ncaab": "Basketball", "ncaabb": "Basketball", "wncaab": "Basketball",
    "nhl": "Ice Hockey", "mlb": "Baseball",
}


def player_cutout(name, sport=""):
    """Best cutout/render URL for a player, or None. Cached per (name, sport)."""
    if not name:
        return None
    ck = (str(name).lower() + "|" + str(sport or "").lower())
    hit = _cache.get(ck)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    url = None
    try:
        import httpx
        r = httpx.get(
            "https://www.thesportsdb.com/api/v1/json/%s/searchplayers.php" % _KEY,
            params={"p": name}, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            players = (r.json() or {}).get("player") or []
            want = _SPORT_NAME.get(str(sport or "").lower())
            cands = [p for p in players if p.get("strCutout") or p.get("strRender")]
            if want:
                same = [p for p in cands if p.get("strSport") == want]
                if same:
                    cands = same
            if cands:
                # prefer a true transparent cutout over a render
                cands.sort(key=lambda p: (1 if p.get("strCutout") else 0), reverse=True)
                best = cands[0]
                url = best.get("strCutout") or best.get("strRender")
    except Exception:
        url = None
    _cache[ck] = (time.time(), url)
    return url
