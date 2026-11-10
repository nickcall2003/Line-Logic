"""
Wikipedia / Wikimedia REST — a genuinely free (no key) source for a real player
photo. Returns the page's lead image (usually a freely-licensed photo for a
living person). Rectangular, not a transparent cutout, so it's best used as a
full-bleed hero background. Best-effort + cached.

Wikimedia requires a descriptive User-Agent or it returns 403.

Common athlete names ("Josh Allen", "Josh Hart") collide on Wikipedia and the
bare title is a *disambiguation* page with no lead image, which used to silently
drop the card to the ESPN headshot. So we resolve the right page: try the bare
title, then the MediaWiki search API biased with the player's sport, and take
the first candidate that yields a clean photo OF this player.
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
    "wedding", "family",
}
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")

# Sport -> words that disambiguate the athlete's Wikipedia page from namesakes.
_SPORT_HINT = {
    "nfl": "American football",
    "ncaaf": "American football",
    "nba": "basketball",
    "wnba": "basketball",
    "ncaab": "basketball",
    "ncaabb": "basketball",
    "wncaab": "basketball",
    "mlb": "baseball",
    "nhl": "ice hockey",
    "epl": "footballer",
    "soccer": "footballer",
    "tennis": "tennis",
}


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


def _summary(title):
    """Fetch a page summary dict (or None). title may contain spaces/underscores."""
    import httpx
    t = str(title).strip().replace(" ", "_")
    try:
        r = httpx.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + t,
                      timeout=8, follow_redirects=True, headers={"User-Agent": _UA})
        if r.status_code == 200:
            return r.json() or {}
    except Exception:
        return None
    return None


def _img_of(summary):
    """Extract the lead image URL from a summary, skipping disambiguation pages."""
    if not summary:
        return None
    if (summary.get("type") or "") == "disambiguation":
        return None
    return (summary.get("originalimage") or {}).get("source") \
        or (summary.get("thumbnail") or {}).get("source")


def _search_titles(name, sport):
    """Candidate page titles for this athlete, best first, via the search API."""
    hint = _SPORT_HINT.get((sport or "").lower(), "")
    q = (str(name) + " " + hint).strip()
    out = []
    try:
        import httpx
        r = httpx.get("https://en.wikipedia.org/w/rest.php/v1/search/page",
                      params={"q": q, "limit": 5}, timeout=8,
                      follow_redirects=True, headers={"User-Agent": _UA})
        if r.status_code == 200:
            surname = [t for t in re.split(r"[^a-z]+", str(name).lower())
                       if len(t) >= 3]
            surname = surname[-1] if surname else ""
            for p in (r.json() or {}).get("pages", []):
                key = p.get("key") or p.get("title") or ""
                # keep only pages that plausibly belong to this person
                if surname and surname in key.lower().replace("_", " "):
                    out.append(key)
    except Exception:
        pass
    return out


def photo(name, sport=""):
    """Safety-filtered lead-image URL for a player's Wikipedia page, or None."""
    if not name:
        return None
    ck = (str(name).lower(), str(sport).lower())
    hit = _cache.get(ck)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]

    url = None
    # 1) Bare name first (correct for unambiguous players; cheapest).
    cand = _img_of(_summary(name))
    if _acceptable(cand, name):
        url = cand
    # 2) Disambiguation / namesake / no image -> search, biased by sport.
    if url is None:
        for title in _search_titles(name, sport):
            cand = _img_of(_summary(title))
            if _acceptable(cand, name):
                url = cand
                break

    _cache[ck] = (time.time(), url)
    return url
