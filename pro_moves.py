"""
pro_moves.py — pro roster-changes boards (NBA / MLB / NFL / NHL).

The pro analogue of the college transfer-portal boards: per team, who they
ADDED (trades in, free-agent signings, waiver claims) and who they LOST (traded
away, released, left in free agency) from the start of the most recent offseason
through today (including the trade deadline). Output matches the portal board
shape so the same frontend renderer handles it.

Data sources:
  * MLB : statsapi.mlb.com/api/v1/transactions  (clean, dated, structured)
  * NBA/NFL/NHL : ESPN core API transactions
        sports.core.api.espn.com/v2/sports/{sport}/leagues/{lg}/transactions
    (list of $refs; each transaction has date, type, team, athlete)

Heavy work (one fetch per transaction for the ESPN leagues) is cached; a settled
move never changes.
"""
from __future__ import annotations

import datetime as dt
import re
import time

_cache = {}        # sport -> (ts, data)
_TTL = 6 * 3600

# League path + offseason-start (month, day) for each sport. The window runs from
# the most recent offseason start through today, so current rosters' full churn
# shows — NBA/NHL open in July (FA), NFL in March, MLB in November.
_LEAGUE = {
    "nba": ("basketball", "nba", (7, 1)),
    "nfl": ("football", "nfl", (3, 1)),
    "nhl": ("hockey", "nhl", (7, 1)),
    "mlb": (None, "mlb", (11, 1)),
}


def _offseason_start(sport):
    mo, day = _LEAGUE[sport][2]
    today = dt.date.today()
    # the most recent occurrence of (mo, day) on or before today
    yr = today.year if (today.month, today.day) >= (mo, day) else today.year - 1
    return dt.date(yr, mo, day)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---- MLB (statsapi) ------------------------------------------------------
# Meaningful move types only (statsapi is full of minor-league optioned/recalled
# noise we don't want on a roster-change board).
_MLB_KEEP = {"TR": "Trade", "FA": "Free Agent", "SFA": "Free Agent",
             "CU": "Waiver Claim", "CL": "Waiver Claim", "REL": "Released",
             "SE": "Signed", "SC": "Signed"}


def _mlb_board():
    import httpx
    start = _offseason_start("mlb")
    end = dt.date.today()
    try:
        r = httpx.get("https://statsapi.mlb.com/api/v1/transactions",
                      params={"startDate": start.isoformat(), "endDate": end.isoformat()},
                      timeout=25)
        r.raise_for_status()
        txns = (r.json() or {}).get("transactions") or []
    except Exception as e:
        print(f"[pro_moves] mlb failed: {e}")
        return None
    teams = {}

    def slot(name):
        if not name:
            return None
        k = _norm(name)
        return teams.setdefault(k, {"team": name, "conference": "",
                                    "gained": 0, "lost": 0, "in": [], "out": []})
    kept = 0
    for t in txns:
        code = t.get("typeCode")
        label = _MLB_KEEP.get(code)
        if not label:
            continue
        player = (t.get("person") or {}).get("fullName") or ""
        if not player:
            continue
        date = (t.get("date") or t.get("effectiveDate") or "")[:10]
        to_t = (t.get("toTeam") or {}).get("name")
        from_t = (t.get("fromTeam") or {}).get("name")
        kept += 1
        # added to the toTeam
        if to_t:
            r = slot(to_t)
            r["gained"] += 1
            r["in"].append({"player": player, "pos": label, "from": from_t or "FA",
                            "value": "", "date": date})
        # lost from the fromTeam (trade/release; FA signing has no fromTeam move)
        if from_t and code in ("TR", "REL", "CU", "CL"):
            r = slot(from_t)
            r["lost"] += 1
            r["out"].append({"player": player, "pos": label, "to": to_t or "—",
                             "value": "", "date": date})
    rows = []
    for r in teams.values():
        r["net"] = r["gained"] - r["lost"]
        r["in"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["out"].sort(key=lambda x: x.get("date") or "", reverse=True)
        r["in"] = r["in"][:12]
        r["out"] = r["out"][:12]
        rows.append(r)
    rows.sort(key=lambda r: (r["gained"] + r["lost"]), reverse=True)  # most active first
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
        r = httpx.get(base, params={"limit": 25}, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
        out["list_keys"] = list(data.keys())
        out["count"] = data.get("count")
        items = data.get("items") or []
        out["item_sample"] = items[:2]
        # follow the first ref to show a transaction detail
        ref = (items[0] or {}).get("$ref") if items else None
        if ref:
            rr = httpx.get(ref, timeout=20)
            rr.raise_for_status()
            out["detail_sample"] = rr.json()
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


def board(sport):
    sport = (sport or "").lower()
    if sport not in _LEAGUE:
        return {"error": "unsupported sport", "teams": []}
    c = _cache.get(sport)
    if c and time.time() - c[0] < _TTL:
        return c[1]
    if sport == "mlb":
        data = _mlb_board() or {"sport": "mlb", "teams": [], "error": "fetch failed"}
    else:
        # NBA/NFL/NHL aggregator is added once the probe confirms the structure.
        data = {"sport": sport, "teams": [], "pending": True,
                "note": "ESPN transactions aggregator pending structure confirmation"}
    _cache[sport] = (time.time(), data)
    return data
