"""
pro_moves.py — pro roster-changes boards (NBA / MLB / NFL / NHL).

The pro analogue of the college transfer-portal boards: per team, who they ADDED
(signings, trades in, waiver claims, draft picks) and who they LOST (waived,
released, traded away) from the start of the most recent offseason through today.
Output matches the portal board shape so the same frontend renderer handles it,
with a 1-100 activity score (busiest offseason = 100).

Data sources:
  * MLB : statsapi.mlb.com/api/v1/transactions  (clean, dated, structured)
  * NBA/NFL/NHL : ESPN per-team transactions pages. ESPN's transaction APIs are
    empty and the league page only embeds the latest 25, but each TEAM page
    (espn.com/{league}/team/transactions/_/name/{abbr}) embeds that team's full
    current-season history in a __espnfitt__ state. We fetch all teams, flatten
    the month-keyed lists, and parse the free-text descriptions into add/drop.

Builds run in a BACKGROUND THREAD and are cached — a request never blocks.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

_cache = {}        # sport -> board dict
_built_at = {}     # sport -> epoch seconds
_building = set()  # sports with a build in flight
_lock = threading.Lock()
_TTL = 6 * 3600

# League path + offseason-start (month, day). NBA/NHL open in July (FA), NFL in
# March, MLB in November.
_LEAGUE = {
    "nba": ("basketball", "nba", (7, 1)),
    "nfl": ("football", "nfl", (3, 1)),
    "nhl": ("hockey", "nhl", (7, 1)),
    "mlb": (None, "mlb", (11, 1)),
}


def _offseason_start(sport):
    mo, day = _LEAGUE[sport][2]
    today = dt.date.today()
    yr = today.year if (today.month, today.day) >= (mo, day) else today.year - 1
    return dt.date(yr, mo, day)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _month_spans(start, end):
    cur = start
    while cur <= end:
        first_next = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        chunk_end = min(end, first_next - dt.timedelta(days=1))
        yield cur, chunk_end
        cur = chunk_end + dt.timedelta(days=1)


# ======================================================================
# MLB — statsapi (clean structured source)
# ======================================================================
_MLB_KEEP = {"TR": "Trade", "FA": "Free Agent", "SFA": "Free Agent",
             "CU": "Waiver Claim", "CL": "Waiver Claim", "REL": "Released",
             "SE": "Signed", "SC": "Signed"}


def _mlb_team_ids(cx):
    try:
        r = cx.get("https://statsapi.mlb.com/api/v1/teams", params={"sportId": 1})
        r.raise_for_status()
        return {t["id"] for t in (r.json() or {}).get("teams", []) if t.get("id")}
    except Exception as e:
        print(f"[pro_moves] mlb team-id fetch failed: {e}")
        return set()


def _mlb_board():
    import httpx
    start = _offseason_start("mlb")
    end = dt.date.today()
    txns = []
    with httpx.Client(timeout=30) as cx:
        mlb_ids = _mlb_team_ids(cx)
        for a, b in _month_spans(start, end):
            try:
                r = cx.get("https://statsapi.mlb.com/api/v1/transactions",
                           params={"startDate": a.isoformat(), "endDate": b.isoformat()})
                r.raise_for_status()
                txns += (r.json() or {}).get("transactions") or []
            except Exception as e:
                print(f"[pro_moves] mlb chunk {a}..{b} failed: {e}")
    if not txns:
        return None
    teams = {}

    def slot(name):
        return teams.setdefault(_norm(name), {"team": name, "conference": "",
                                              "gained": 0, "lost": 0, "in": [], "out": []})

    def touches_minors(*objs):
        if not mlb_ids:
            return False
        return any(o.get("name") and o.get("id") not in mlb_ids for o in objs)

    kept = 0
    for t in txns:
        label = _MLB_KEEP.get(t.get("typeCode"))
        if not label:
            continue
        if "minor league" in (t.get("description") or "").lower():
            continue
        to_obj = t.get("toTeam") or {}
        from_obj = t.get("fromTeam") or {}
        if touches_minors(to_obj, from_obj):
            continue
        player = (t.get("person") or {}).get("fullName") or ""
        if not player:
            continue
        date = (t.get("date") or t.get("effectiveDate") or "")[:10]
        to_name, from_name = to_obj.get("name"), from_obj.get("name")
        if not (to_name or from_name):
            continue
        counted = False
        if to_name:
            r = slot(to_name)
            r["gained"] += 1
            r["in"].append({"player": player, "pos": label, "from": from_name or "FA",
                            "value": "", "date": date})
            counted = True
        if from_name and t.get("typeCode") in ("TR", "REL", "CU", "CL"):
            r = slot(from_name)
            r["lost"] += 1
            r["out"].append({"player": player, "pos": label, "to": to_name or "—",
                             "value": "", "date": date})
            counted = True
        if counted:
            kept += 1
    return _finalize("mlb", "MLB Stats API", start.isoformat(), teams, kept)


# ======================================================================
# NBA / NFL / NHL — ESPN per-team transaction pages
# ======================================================================
def _match_braces(txt, start):
    depth = 0
    i = start
    instr = False
    esc = False
    while i < len(txt):
        c = txt[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return txt[start:i + 1]
        i += 1
    return None


def _espnfitt_blob(txt):
    import json
    best = None
    pos = 0
    while True:
        idx = txt.find("__espnfitt__", pos)
        if idx < 0:
            break
        pos = idx + 12
        eq = txt.find("=", idx + 12, idx + 60)
        if eq < 0:
            continue
        start = txt.find("{", eq, eq + 12)
        if start < 0:
            continue
        obj = _match_braces(txt, start)
        if not obj:
            continue
        try:
            d = json.loads(obj)
        except Exception:
            continue
        if isinstance(d, dict):
            if "page" in d or "app" in d:
                return d
            if best is None or len(d) > len(best):
                best = d
    return best


# ---- free-text description parser -------------------------------------
_POS = ("QB", "RB", "FB", "WR", "TE", "OT", "OG", "OL", "DE", "DT", "DL", "NT",
        "ILB", "OLB", "LB", "EDGE", "CB", "FS", "SS", "DB", "LS", "PG", "SG",
        "SF", "PF", "LW", "RW", "LD", "RD", "C", "G", "F", "D", "T", "S", "K", "P")
_POS_RE = "|".join(sorted(_POS, key=len, reverse=True))
_NAME = r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){0,2}(?:\s+(?:Jr\.?|Sr\.?|II|III|IV|V))?"
_UNIT = re.compile(r"\b(" + _POS_RE + r")s?\.?\s+(" + _NAME + r"(?:\s+and\s+" + _NAME + r")?)")
_ADD_VERB = re.compile(r"^(Signed|Acquired|Claimed|Selected)", re.I)
_LOSS_VERB = re.compile(r"^(Waived|Released|Traded|Terminated)", re.I)


def _names(chunk):
    out = []
    for m in _UNIT.finditer(chunk):
        pos, who = m.group(1), m.group(2)
        for nm in re.split(r"\s+and\s+", who):
            nm = nm.strip(" .")
            if nm:
                out.append((pos, nm))
    return out


def _parse_desc(desc):
    """(adds, losses) as lists of (pos, player). Skips extensions, re-signings
    and IL/status moves — the board tracks real roster changes only."""
    adds, losses = [], []
    d = (desc or "").strip()
    low = d.lower()
    if "extension" in low or low.startswith("re-signed"):
        return adds, losses
    sent = re.search(r"\bsent\s+(.*?)(?:\.|$)", d)
    if sent and ("Acquired" in d or "Traded" in d):
        head = d[:sent.start()]
        if re.search(r"Acquired", head):
            adds += _names(head)
        losses += _names(sent.group(1))
        if head.strip().startswith("Traded"):
            losses += _names(head)
        return adds, losses
    if _ADD_VERB.match(d):
        adds += _names(d.split(" from ")[0].split(" to ")[0])
    elif _LOSS_VERB.match(d):
        losses += _names(d.split(" to ")[0])
    return adds, losses


def _src_ctx(d):
    m = re.search(r"\bfrom (?:the )?([A-Z][A-Za-z.&'’ ]+?)(?:\s+in\b|\s+for\b|\s+as\b|[.,]|$)", d)
    if m:
        return m.group(1).strip()
    if d.startswith("Claimed"):
        return "waivers"
    if d.startswith("Selected"):
        return "draft"
    return "FA"


def _dst_ctx(d):
    m = re.search(r"\bto (?:the )?([A-Z][A-Za-z.&'’ ]+?)(?:[.,]|$)", d)
    if m:
        return m.group(1).strip()
    return "waivers"


def _espn_teams(cx, group, league):
    r = cx.get(f"https://site.api.espn.com/apis/site/v2/sports/{group}/{league}/teams")
    r.raise_for_status()
    rows = r.json()["sports"][0]["leagues"][0]["teams"]
    out = []
    for t in rows:
        tm = t["team"]
        out.append({"abbr": (tm.get("abbreviation") or "").lower(),
                    "name": tm.get("displayName") or tm.get("name")})
    return out


def _espn_team_txns(cx, league, abbr):
    url = f"https://www.espn.com/{league}/team/transactions/_/name/{abbr}"
    r = cx.get(url)
    data = _espnfitt_blob(r.text or "") or {}
    cont = ((data.get("page") or {}).get("content") or {}).get("transactions") or {}
    months = cont.get("transactions")
    flat = []
    if isinstance(months, dict):
        for lst in months.values():
            if isinstance(lst, list):
                flat += [x for x in lst if isinstance(x, dict)]
    elif isinstance(months, list):
        flat = [x for x in months if isinstance(x, dict)]
    return flat


def _espn_board(sport):
    import httpx
    group, league, _ = _LEAGUE[sport]
    teams = {}
    all_dates = []
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        try:
            roster = _espn_teams(cx, group, league)
        except Exception as e:
            print(f"[pro_moves] {sport} team list failed: {e}")
            return None
        for tm in roster:
            if not tm["abbr"]:
                continue
            try:
                txns = _espn_team_txns(cx, league, tm["abbr"])
            except Exception as e:
                print(f"[pro_moves] {sport} {tm['abbr']} failed: {e}")
                continue
            rec = {"team": tm["name"], "conference": "", "gained": 0, "lost": 0,
                   "in": [], "out": []}
            for tx in txns:
                desc = tx.get("description") or ""
                date = (tx.get("date") or "")[:10]
                if date:
                    all_dates.append(date)
                adds, losses = _parse_desc(desc)
                for pos, who in adds:
                    rec["gained"] += 1
                    rec["in"].append({"player": who, "pos": pos, "from": _src_ctx(desc),
                                      "value": "", "date": date})
                for pos, who in losses:
                    rec["lost"] += 1
                    rec["out"].append({"player": who, "pos": pos, "to": _dst_ctx(desc),
                                       "value": "", "date": date})
            if rec["gained"] or rec["lost"]:
                teams[_norm(tm["name"])] = rec
    if not teams:
        return None
    start = min(all_dates) if all_dates else _offseason_start(sport).isoformat()
    total = sum(r["gained"] + r["lost"] for r in teams.values())
    return _finalize(sport, "ESPN", start, teams, total)


# ======================================================================
# shared finalize + build orchestration
# ======================================================================
def _finalize(sport, source, window_start, teams, count_entries):
    rows = []
    for r in teams.values():
        r["net"] = r["gained"] - r["lost"]
        r["in"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["out"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["in"] = r["in"][:16]
        r["out"] = r["out"][:16]
        rows.append(r)
    rows.sort(key=lambda r: (r["gained"] + r["lost"]), reverse=True)
    max_act = max((r["gained"] + r["lost"] for r in rows), default=0) or 1
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["score"] = max(1, round(100 * (r["gained"] + r["lost"]) / max_act))
    return {"sport": sport, "source": source, "kind": "trades",
            "window_start": window_start,
            "built_at": dt.datetime.utcnow().isoformat() + "Z",
            "count_entries": count_entries, "teams": rows}


def _build(sport):
    try:
        data = _mlb_board() if sport == "mlb" else _espn_board(sport)
        if data:
            with _lock:
                _cache[sport] = data
                _built_at[sport] = time.time()
    except Exception as e:
        print(f"[pro_moves] build {sport} failed: {e}")
    finally:
        with _lock:
            _building.discard(sport)


def board(sport):
    sport = (sport or "").lower()
    if sport not in _LEAGUE:
        return {"error": "unsupported sport", "teams": []}
    with _lock:
        data = _cache.get(sport)
        fresh = data and (time.time() - _built_at.get(sport, 0) < _TTL)
        if fresh:
            return data
        if sport not in _building:
            _building.add(sport)
            threading.Thread(target=_build, args=(sport,), daemon=True).start()
    if data:
        return data
    return {"sport": sport, "teams": [], "building": True,
            "window_start": _offseason_start(sport).isoformat()}


# ---- college baseball transfer board (ESPN rankings article, option B) ----
_CBASE_URL = ("https://www.espn.com/college-baseball/story/_/id/49168943/"
              "2026-college-baseball-transfer-portal-rankings-signed")
_CB_RE = re.compile(
    r"(\d{1,2})\.\s+"
    r"([A-Z][A-Za-z.'\u2019\- ]+?),\s*"
    r"([A-Za-z0-9/]{1,6}),\s*"
    r".*?Transferring from:\s*([^|\n]+?)\s*[|\n]\s*"
    r"Transferring to:\s*([A-Za-z&.'\u2019\- ]+?)(?=\s{2,}|\n|$)",
    re.S)
_cbase = {"data": None, "ts": 0}
_CBASE_TTL = 12 * 3600


def _cbase_build():
    import httpx
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        r = cx.get(_CBASE_URL)
        html = r.text or ""
    txt = re.sub(r"<script[\s\S]*?</script>", " ", html)
    txt = re.sub(r"<[^>]+>", " ", txt)
    entries = []
    seen = set()
    for m in _CB_RE.finditer(txt):
        rank = int(m.group(1))
        if rank in seen:
            continue
        seen.add(rank)
        entries.append((rank, m.group(2).strip(), m.group(3).strip(),
                        m.group(4).strip(), m.group(5).strip()))
    if not entries:
        return None
    nmax = max(e[0] for e in entries)
    schools = {}

    def slot(name):
        return schools.setdefault(_norm(name), {"team": name, "conference": "",
                                                "gained": 0, "lost": 0, "in": [], "out": [],
                                                "_tw": 0})
    _PLACE = {"tbd", "undecided", "uncommitted", "unknown", "portal", ""}
    for rank, player, pos, frm, to in entries:
        w = nmax - rank + 1
        to_place = _norm(to) in _PLACE
        # only count a GAINED entry for a real destination (uncommitted players
        # haven't landed anywhere yet — no fake "TBD" team)
        if not to_place:
            s = slot(to)
            s["gained"] += 1
            s["_tw"] += w
            s["in"].append({"player": player, "pos": pos, "from": frm,
                            "value": "#%d" % rank, "_r": rank})
        # the old school still loses them (to the portal, destination pending)
        if _norm(frm) not in _PLACE:
            s = slot(frm)
            s["lost"] += 1
            s["out"].append({"player": player, "pos": pos,
                             "to": (to if not to_place else "Portal (TBD)"),
                             "value": "#%d" % rank, "_r": rank})
    rows = []
    for s in schools.values():
        s["net"] = s["gained"] - s["lost"]
        s["in"].sort(key=lambda x: x["_r"])
        s["out"].sort(key=lambda x: x["_r"])
        for p in s["in"] + s["out"]:
            p.pop("_r", None)
        rows.append(s)
    rows.sort(key=lambda s: (s["net"], s.pop("_tw")), reverse=True)
    for i, s in enumerate(rows):
        s["rank"] = i + 1
    return {"sport": "cbase", "source": "ESPN top transfers", "season": "2026",
            "count_entries": len(entries), "teams": rows}


def cbase_board():
    """College baseball transfer board (per school, from ESPN's ranked top-60
    transfers). Cached; refreshes every 12h."""
    now = time.time()
    if _cbase["data"] and now - _cbase["ts"] < _CBASE_TTL:
        return _cbase["data"]
    try:
        d = _cbase_build()
        if d:
            _cbase["data"] = d
            _cbase["ts"] = now
    except Exception as e:
        print(f"[pro_moves] cbase build failed: {e}")
    return _cbase["data"] or {"sport": "cbase", "teams": [],
                              "building": True, "season": "2026"}


def status():
    with _lock:
        return {s: {"cached": s in _cache, "building": s in _building,
                    "teams": len((_cache.get(s) or {}).get("teams") or []),
                    "built_at": _built_at.get(s)}
                for s in _LEAGUE}
