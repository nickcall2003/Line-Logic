"""
Wikipedia / Wikimedia REST — a genuinely free (no key) source for a real player
photo. Returns the page's lead image (usually a freely-licensed photo for a
living person). Rectangular, not a transparent cutout, so it's best used as a
full-bleed hero background. Best-effort + cached.

Wikimedia requires a descriptive User-Agent or it returns 403.
"""
import re
import time

_cache = {}
_TTL = 7 * 24 * 3600
_UA = "LineLogic/1.0 (https://thelinelogic.com; contact@thelinelogic.com)"

# Reject lead images that aren't a clean shot of the player: political/group/
# off-brand photos (the "Judge posing with Trump" problem) and non-photo assets.
_BLOCK = {
    "trump", "president", "obama", "biden", "harris", "clinton", "bush",
    "politician", "congress", "senate", "governor", "inauguration", "rally",
    "campaign", "white_house", "protest", "funeral", "memorial", "statue",
    "mural", "logo", "signature", "wordmark", "jersey", "helmet", "trophy_only",
    "wedding", "family", "funeral",
}
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _acceptable(url, name):
    """True only if the image filename looks like a solo photo OF this player."""
    if not url:
        return False
    fn = url.rsplit("/", 1)[-1].split("?")[0].lower()
    if not fn.endswith(_IMG_EXT):
        return False
    for b in _BLOCK:
        if b in fn:
            return False
    toks = [t for t in re.split(r"[^a-z]+", str(name).lower()) if len(t) >= 3]
    if not toks:
        return False
    # require the player's surname (or a distinctive 4+ char name token) present
    if toks[-1] in fn:
        return True
    return any(t in fn for t in toks if len(t) >= 4)


def photo(name, sport=""):
    """Safety-filtered lead-image URL for a player's Wikipedia page, or None."""
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
            cand = (j.get("originalimage") or {}).get("source") \
                or (j.get("thumbnail") or {}).get("source")
            if _acceptable(cand, name):
                url = cand
    except Exception:
        url = None
    _cache[ck] = (time.time(), url)
    return url
