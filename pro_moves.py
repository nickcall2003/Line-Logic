"""
pro_moves.py — pro roster-changes boards (NBA / MLB / NFL / NHL).

The pro analogue of the college transfer-portal boards: per team, who they
ADDED (trades in, free-agent signings, waiver claims) and who they LOST (traded
away, released, left in free agency) from the start of the most recent offseason
through today (including the trade deadline). Output matches the portal board
shape so the same frontend renderer handles it.

Data sources:
  * MLB : statsapi.mlb.com/api/v1/transactions  (clean, dated, structured)
  * NBA/NFL/NHL : ESPN core API transactions (pending a structure probe)

Builds run in a BACKGROUND THREAD and the result is cached — a request never
blocks on the fetch. A full offseason of MLB transactions is a large payload
(every affiliate's minor-league shuffle included), so it is pulled in monthly
chunks and filtered down to real roster moves.
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

# League path + offseason-start (month, day). The window runs from the most
# recent offseason start through today — NBA/NHL open in July (FA), NFL in
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
    """Yield (chunk_start, chunk_end) covering [start, end] one calendar month at
    a time, so each API call is small enough to never time out."""
    cur = start
    while cur <= end:
        first_next = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        chunk_end = min(end, first_next - dt.timedelta(days=1))
        yield cur, chunk_end
        cur = chunk_end + dt.timedelta(days=1)


# ---- MLB (statsapi) ------------------------------------------------------
# Meaningful move types only (statsapi is full of minor-league optioned/recalled
# noise we don't want on a roster-change board).
_MLB_KEEP = {"TR": "Trade", "FA": "Free Agent", "SFA": "Free Agent",
             "CU": "Waiver Claim", "CL": "Waiver Claim", "REL": "Released",
             "SE": "Signed", "SC": "Signed"}


def _mlb_team_ids(cx):
    """The 30 MLB clubs' team IDs (sportId=1). Used to drop every minor-league
    affiliate so only big-league teams appear on the board."""
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
        mlb_ids = _mlb_team_ids(cx)        # the 30 big-league clubs
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
        k = _norm(name)
        return teams.setdefault(k, {"team": name, "conference": "",
                                    "gained": 0, "lost": 0, "in": [], "out": []})

    def touches_minors(*objs):
        # True if any named side is a non-MLB club (an affiliate) — i.e. the move
        # is a minor-league transaction / internal assignment, not a big-league one.
        if not mlb_ids:
            return False
        return any(o.get("name") and o.get("id") not in mlb_ids for o in objs)

    kept = 0
    for t in txns:
        code = t.get("typeCode")
        label = _MLB_KEEP.get(code)
        if not label:
            continue
        # drop minor-league contracts and any move touching a minor-league club
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
        if from_name and code in ("TR", "REL", "CU", "CL"):
            r = slot(from_name)
            r["lost"] += 1
            r["out"].append({"player": player, "pos": label, "to": to_name or "—",
                             "value": "", "date": date})
            counted = True
        if counted:
            kept += 1
    rows = []
    for r in teams.values():
        r["net"] = r["gained"] - r["lost"]
        r["in"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["out"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["in"] = r["in"][:12]
        r["out"] = r["out"][:12]
        rows.append(r)
    rows.sort(key=lambda r: (r["gained"] + r["lost"]), reverse=True)
    # 1-100 activity score: busiest offseason = 100, scaled down from there.
    max_act = max((r["gained"] + r["lost"] for r in rows), default=0) or 1
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["score"] = max(1, round(100 * (r["gained"] + r["lost"]) / max_act))
    return {"sport": "mlb", "source": "MLB Stats API", "kind": "trades",
            "window_start": start.isoformat(),
            "built_at": dt.datetime.utcnow().isoformat() + "Z",
            "count_entries": kept, "teams": rows}


# ---- ESPN leagues probe (structure confirmation) -------------------------

def espn_probe(sport):
    """Fetch the ESPN core transactions list + follow the first couple of refs so
    we can see the exact transaction structure before building the aggregator."""
    import httpx
    if sport not in _LEAGUE or sport == "mlb":
        return {"error": "espn probe is for nba/nfl/nhl"}
    group, lg, _ = _LEAGUE[sport]
    base = f"https://sports.core.api.espn.com/v2/sports/{group}/leagues/{lg}/transactions"
    out = {"sport": sport, "list_url": base}
    try:
        with httpx.Client(timeout=20) as cx:
            r = cx.get(base, params={"limit": 25})
            r.raise_for_status()
            data = r.json() or {}
            out["list_keys"] = list(data.keys())
            out["count"] = data.get("count")
            items = data.get("items") or []
            out["item_sample"] = items[:2]
            ref = (items[0] or {}).get("$ref") if items else None
            if ref:
                rr = cx.get(ref)
                rr.raise_for_status()
                out["detail_sample"] = rr.json()
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


# ---- build orchestration (never blocks the request) ----------------------

def _build(sport):
    try:
        data = _mlb_board() if sport == "mlb" else None
        if data:
            with _lock:
                _cache[sport] = data
                _built_at[sport] = time.time()
    except Exception as e:
        print(f"[pro_moves] build {sport} failed: {e}")
    finally:
        with _lock:
            _building.discard(sport)


def espn_probe_sources(sport):
    """Try the candidate ESPN transaction sources and report which actually
    carries data, so we can pick the real feed for the NBA/NFL/NHL boards. The
    core API came back empty; these are the site APIs + the HTML page ESPN's own
    transactions tab is built from (it embeds a __espnfitt__ JSON state)."""
    import httpx
    if sport not in _LEAGUE or sport == "mlb":
        return {"error": "nba/nfl/nhl only"}
    group, league, _ = _LEAGUE[sport]
    yr = dt.date.today().year
    cands = [
        f"https://site.web.api.espn.com/apis/fitt/v3/sports/{group}/{league}/transactions?limit=50",
        f"https://site.web.api.espn.com/apis/common/v3/sports/{group}/{league}/transactions?limit=50",
        f"https://sports.core.api.espn.com/v2/sports/{group}/leagues/{league}/seasons/{yr}/transactions?limit=50",
    ]
    out = []
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        for u in cands:
            row = {"url": u}
            try:
                r = cx.get(u)
                row["status"] = r.status_code
                try:
                    j = r.json()
                    if isinstance(j, dict):
                        row["keys"] = list(j.keys())[:12]
                        row["count"] = j.get("count")
                        items = j.get("items") or j.get("transactions") or []
                        row["n_items"] = len(items) if isinstance(items, list) else None
                        row["sample"] = (items[0] if items else None)
                except Exception:
                    row["body_head"] = (r.text or "")[:160]
            except Exception as e:
                row["error"] = str(e)[:160]
            out.append(row)
        try:
            h = cx.get(f"https://www.espn.com/{league}/transactions")
            txt = h.text or ""
            out.append({"url": f"www.espn.com/{league}/transactions",
                        "status": h.status_code, "len": len(txt),
                        "has_fitt": "__espnfitt__" in txt,
                        "transaction_word_hits": txt.lower().count("transaction")})
        except Exception as e:
            out.append({"url": "html-page", "error": str(e)[:160]})
    return out


def board(sport):
    sport = (sport or "").lower()
    if sport not in _LEAGUE:
        return {"error": "unsupported sport", "teams": []}
    # ESPN leagues: aggregator pending the structure probe — answer immediately.
    if sport != "mlb":
        return {"sport": sport, "teams": [], "pending": True,
                "note": "ESPN transactions aggregator pending structure confirmation"}
    with _lock:
        data = _cache.get(sport)
        fresh = data and (time.time() - _built_at.get(sport, 0) < _TTL)
        if fresh:
            return data
        if sport not in _building:
            _building.add(sport)
            threading.Thread(target=_build, args=(sport,), daemon=True).start()
    if data:                      # stale but usable while a refresh runs
        return data
    return {"sport": sport, "teams": [], "building": True,
            "window_start": _offseason_start(sport).isoformat()}


def status():
    with _lock:
        return {s: {"cached": s in _cache, "building": s in _building,
                    "teams": len((_cache.get(s) or {}).get("teams") or []),
                    "built_at": _built_at.get(s)}
                for s in _LEAGUE}
