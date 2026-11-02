"""
win_total_lines.py — season win-total MARKET lines (the book over/under per
team), scraped to sit next to our model projections.

The Odds API only carries game markets and championship futures, not season
win totals, so these come from a public consensus table (sportsbettingdime,
which renders a clean HTML table of Team | Win Total | Over | Under per sport).

Reality check on coverage: books post season win totals for every pro team
(NBA/NFL/NHL/MLB, 30-32 each) and for FBS college football, but NOT for all
~360 D-I college basketball teams — that market barely exists, so CBB stays
projection-only. NHL is posted as a POINTS total, not wins, so it isn't
comparable to our win projection and is left off.

Everything is cached (12h) and fails soft: no lines just means the UI shows
the pure projection, exactly as before.
"""
import re
import time

# Candidate source URLs per sport. The fetcher tries each until one returns a
# usable table — so a slug change on their side degrades gracefully instead of
# breaking. Confirmed working: NBA. Others are the obvious same-pattern slugs,
# verified live via the startup diag.
_SOURCES = {
    "nba": [
        "https://www.sportsbettingdime.com/nba/win-totals-best-odds",
    ],
    "nfl": [
        "https://www.sportsbettingdime.com/nfl/win-totals-best-odds",
    ],
    "mlb": [
        "https://www.sportsbettingdime.com/mlb/win-totals-best-odds",
    ],
    "ncaaf": [
        "https://www.sportsbettingdime.com/college-football/win-totals-best-odds",
        "https://www.sportsbettingdime.com/ncaaf/win-totals-best-odds",
    ],
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TTL = 12 * 3600
_cache: dict = {}   # sport -> (ts, {norm_name: {...}})


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#x27;", "'")
    s = s.replace("&#39;", "'").replace("&rsquo;", "'")
    return re.sub(r"\s+", " ", s).strip()


_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_ODDS = re.compile(r"^[+-]\d{2,4}$")          # -105, +120, -110
_DEC = re.compile(r"^\d{1,3}\.\d+$")          # 60.5, 35.5, 103.5  -> the line
_INT = re.compile(r"^\d{1,3}$")               # 41, 9  (whole-number line fallback)


def _fetch(url):
    import httpx
    r = httpx.get(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }, timeout=25, follow_redirects=True)
    r.raise_for_status()
    return r.text or ""


def _parse_table(html):
    """Pull {norm_team: {team, line, over, under}} out of a win-totals table.
    Column positions aren't trusted. Per row: the team is the first text cell
    with letters; the line is the first number carrying a decimal (win totals
    are halves like 60.5 — odds are whole signed numbers like -105), falling
    back to a bare small integer; signed numbers after it are the over/under
    odds."""
    out = {}
    for row in _TR.findall(html):
        cells = [_strip_tags(c) for c in _CELL.findall(row)]
        cells = [c for c in cells if c != ""]
        if len(cells) < 2:
            continue
        team = None
        line = None
        int_line = None
        odds = []
        for c in cells:
            if team is None and re.search(r"[A-Za-z]{3,}", c) and not _ODDS.match(c):
                team = c
                continue
            if line is None and _DEC.match(c):
                v = float(c)
                if 0.5 <= v <= 130:
                    line = v
                    continue
            if _ODDS.match(c):
                odds.append(c)
                continue
            if int_line is None and _INT.match(c):
                v = int(c)
                if 1 <= v <= 120:
                    int_line = v
        if team is None:
            continue
        use = line if line is not None else int_line
        if use is None:
            continue
        o = odds[0] if len(odds) >= 1 else None
        u = odds[1] if len(odds) >= 2 else None
        out[_norm(team)] = {"team": team, "line": float(use), "over": o, "under": u}
    return out


def _build(sport):
    urls = _SOURCES.get(sport) or []
    for u in urls:
        try:
            html = _fetch(u)
            tbl = _parse_table(html)
            if len(tbl) >= 8:          # a real league table, not a stray 1-row
                return tbl, u, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    return {}, (urls[0] if urls else None), locals().get("last")


def lines(sport):
    """{norm_team_name: {team, line, over, under}} for a sport, cached 12h.
    Empty dict when unavailable (UI then shows projection only)."""
    sport = (sport or "").lower()
    if sport not in _SOURCES:
        return {}
    now = time.time()
    hit = _cache.get(sport)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    tbl, _url, _err = _build(sport)
    if tbl:
        _cache[sport] = (now, tbl)
        return tbl
    # keep stale over nothing
    if hit:
        return hit[1]
    return {}


def line_for(sport, team_name, abbr=None):
    """Best-match win-total line for one team, or None."""
    tbl = lines(sport)
    if not tbl:
        return None
    n = _norm(team_name)
    if n in tbl:
        return tbl[n]
    # loose contains match (e.g. "Oklahoma City Thunder" vs "Thunder")
    for k, v in tbl.items():
        if n and (n in k or k in n):
            return v
    # last token (nickname) match
    toks = (team_name or "").split()
    if toks:
        last = _norm(toks[-1])
        for k, v in tbl.items():
            if last and last in k:
                return v
    return None


def diag():
    """Report per-sport reachability + parse result (printed once at startup)."""
    out = {}
    for sp in _SOURCES:
        rec = {}
        tbl, url, err = _build(sp)
        rec["url"] = url
        rec["n"] = len(tbl)
        if err:
            rec["err"] = err
        samp = list(tbl.values())[:3]
        rec["sample"] = [{"t": s["team"], "line": s["line"]} for s in samp]
        out[sp] = rec
    return out
