"""
Wikipedia / Wikimedia REST — a genuinely free (no key) source for a real player
photo. Returns the page's lead image (usually a freely-licensed photo for a
living person). Rectangular, not a transparent cutout, so it's best used as a
full-bleed hero background. Best-effort + cached.

Wikimedia requires a descriptive User-Agent or it returns 403.
"""
import time

_cache = {}
_TTL = 7 * 24 * 3600
_UA = "LineLogic/1.0 (https://thelinelogic.com; contact@thelinelogic.com)"


def photo(name, sport=""):
    """Lead-image URL for a player's Wikipedia page, or None. Cached."""
    if not name:
        return None
    ck = str(name).lower()
    hit = _cache.get(ck)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    url = None
    try:
        import httpx
        title = str(name).strip().replace(" ", "_")
        r = httpx.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + title,
                      timeout=8, follow_redirects=True, headers={"User-Agent": _UA})
        if r.status_code == 200:
            j = r.json() or {}
            oi = j.get("originalimage") or {}
            th = j.get("thumbnail") or {}
            url = oi.get("source") or th.get("source")
    except Exception:
        url = None
    _cache[ck] = (time.time(), url)
    return url
