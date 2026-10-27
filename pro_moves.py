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


def _match_braces(txt, start):
    """Return the balanced {...} substring beginning at index `start`, skipping
    braces that appear inside JSON strings."""
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
    """Find and parse the window['__espnfitt__'] = {...} state. The name appears
    several times in the page (references, not just the assignment), so try each
    occurrence and keep the biggest object — the real state has `page`/`app`."""
    import json
    best = None
    pos = 0
    while True:
        idx = txt.find("__espnfitt__", pos)
        if idx < 0:
            break
        pos = idx + 12
        eq = txt.find("=", idx + 12, idx + 60)   # '=' comes after the 12-char name
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


def espn_scrape_probe(sport):
    """Pull the ESPN transactions page, parse its embedded state, and locate the
    transactions list + a couple sample entries so we can see the exact shape."""
    import httpx
    group, league, _ = _LEAGUE[sport]
    url = f"https://www.espn.com/{league}/transactions"
    out = {"url": url}
    try:
        with httpx.Client(timeout=25, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as cx:
            r = cx.get(url)
            out["status"] = r.status_code
            txt = r.text or ""
        import json as _j
        data = _espnfitt_blob(txt)
        if data is None:
            out["error"] = "no __espnfitt__ blob parsed"
            return out
        out["top_keys"] = list(data.keys())[:25]
        pc = data.get("page", {})
        pc = pc.get("content", {}) if isinstance(pc, dict) else {}
        if isinstance(pc, dict):
            cmap = {}
            for k, v in pc.items():
                if isinstance(v, list):
                    cmap[k] = f"list[{len(v)}]" + ((" e0=" + _j.dumps(v[0])[:220]) if v else "")
                elif isinstance(v, dict):
                    cmap[k] = "dict{" + ",".join(list(v.keys())[:12]) + "}"
                else:
                    cmap[k] = f"{type(v).__name__}:{str(v)[:30]}"
            out["content_map"] = cmap
        # heuristic: any list whose elements carry a description/text + a date
        cands = []

        def txnish(d):
            if not isinstance(d, dict):
                return False
            ks = {k.lower() for k in d.keys()}
            return bool(ks & {"description", "text", "desc", "note", "headline"})

        def walk(o, path, depth=0):
            if len(cands) >= 5 or depth > 14:
                return
            if isinstance(o, list):
                if o and txnish(o[0]):
                    cands.append((path, len(o), o[0]))
                for j, v in enumerate(o[:40]):
                    walk(v, f"{path}[{j}]", depth + 1)
            elif isinstance(o, dict):
                for k, v in o.items():
                    walk(v, path + "." + k, depth + 1)
        walk(data, "")
        out["candidates"] = [{"path": p, "n": n, "sample": s} for p, n, s in cands[:3]]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


_PST_PATH = {"nba": "basketball", "nfl": "football", "nhl": "hockey"}


def pst_probe(sport):
    """Probe prosportstransactions.com — a structured transactions DB with clean
    HTML tables (Date | Team | Acquired | Relinquished | Notes). Report the table
    shape + sample rows so we can build the real NBA/NFL/NHL parser from it."""
    import httpx
    import re
    path = _PST_PATH.get(sport)
    if not path:
        return {"error": "nba/nfl/nhl only"}
    start = _offseason_start(sport).isoformat()
    end = dt.date.today().isoformat()
    url = f"https://www.prosportstransactions.com/{path}/Search/SearchResults.php"
    params = {"Player": "", "Team": "", "BeginDate": start, "EndDate": end,
              "ILChkBx": "yes", "InjuriesChkBx": "yes", "PersonalChkBx": "yes",
              "Submit": "Search", "start": 0}
    out = {"url": url, "begin": start, "end": end}
    try:
        with httpx.Client(timeout=25, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as cx:
            r = cx.get(url, params=params)
            out["status"] = r.status_code
            html = r.text or ""
            out["len"] = len(html)
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)

        def cells(tr):
            return [re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").strip()
                    for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        allrows = [cells(t) for t in trs]
        datarows = [c for c in allrows if c and re.match(r"\d{4}-\d{2}-\d{2}", c[0] or "")]
        # header row = the first all-<td>/<th> row before data; grab any <th> row
        ths = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
        header = None
        for t in ths:
            hc = [re.sub(r"<[^>]+>", " ", c).strip()
                  for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", t, re.S)]
            if hc and any("Date" in x for x in hc):
                header = hc
                break
        out["header"] = header
        out["n_datarows"] = len(datarows)
        out["sample_rows"] = datarows[:6]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


_WIKI_TITLES = {
    "nba": ["2026–27 NBA season transactions",
            "List of 2026–27 NBA season transactions", "2026–27 NBA season"],
    "nhl": ["2026–27 NHL season transactions",
            "List of 2026–27 NHL transactions", "2026–27 NHL season"],
    "nfl": ["2026 NFL season transactions",
            "List of 2026 NFL transactions", "2026 NFL season"],
}


def wiki_probe(sport):
    """Probe Wikipedia's season-transaction article for a league: which candidate
    title resolves, how many wikitables it has, and sample rows — so we can see
    whether the transactions sit in a clean table we can parse."""
    import httpx
    import re
    titles = _WIKI_TITLES.get(sport, [])
    out = {"sport": sport, "titles_tried": titles}
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "LineLogic/1.0 (analytics)"}) as cx:
        for t in titles:
            try:
                r = cx.get("https://en.wikipedia.org/w/api.php",
                           params={"action": "parse", "page": t, "prop": "text",
                                   "format": "json", "redirects": 1})
                j = r.json()
                if "error" in j:
                    out["miss_" + t] = j["error"].get("code")
                    continue
                html = j.get("parse", {}).get("text", {}).get("*", "")
                tables = re.findall(r'<table[^>]*wikitable[^>]*>(.*?)</table>', html, re.S)
                out["resolved_title"] = j["parse"].get("title")
                out["n_wikitables"] = len(tables)

                def cells(tr):
                    return [re.sub(r"<[^>]+>", " ", c).replace("&#160;", " ").strip()[:70]
                            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
                if tables:
                    big = max(tables, key=len)
                    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", big, re.S)
                    rows = [c for c in (cells(x) for x in trs) if c]
                    out["sample_rows"] = rows[:7]
                break
            except Exception as e:
                out["err_" + t] = str(e)[:120]
                break
    return out


def src_probe(sport):
    """Decisive source probe: can we pull current + historical rosters from ESPN
    (which already works from this server)? If so, roster-diff gives the offseason
    add/drop list with no text parsing. Report team count, current roster sample,
    and which season-scoped roster years actually carry data."""
    import httpx
    group, league, _ = _LEAGUE[sport]
    out = {"sport": sport}
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        tid = None
        try:
            tr = cx.get(f"https://site.api.espn.com/apis/site/v2/sports/{group}/{league}/teams")
            tj = tr.json()
            teams = tj["sports"][0]["leagues"][0]["teams"]
            tid = teams[0]["team"]["id"]
            out["teams"] = {"n": len(teams), "sample_id": tid,
                            "sample_name": teams[0]["team"]["displayName"]}
        except Exception as e:
            out["teams_err"] = str(e)[:150]
        if tid:
            try:
                rr = cx.get(f"https://site.api.espn.com/apis/site/v2/sports/{group}/{league}/teams/{tid}/roster")
                rj = rr.json()
                names = []

                def collect(o):
                    if isinstance(o, dict):
                        if o.get("fullName"):
                            names.append(o["fullName"])
                        for v in o.values():
                            collect(v)
                    elif isinstance(o, list):
                        for v in o:
                            collect(v)
                collect(rj.get("athletes"))
                out["roster_current"] = {"status": rr.status_code, "n": len(names),
                                         "sample": names[:4]}
            except Exception as e:
                out["roster_err"] = str(e)[:150]
            for yr in (2024, 2025, 2026, 2027):
                try:
                    cr = cx.get(f"https://sports.core.api.espn.com/v2/sports/{group}/leagues/{league}/seasons/{yr}/teams/{tid}/athletes",
                                params={"limit": 80})
                    cj = cr.json()
                    out[f"core_{yr}"] = {"count": cj.get("count"),
                                         "n_items": len(cj.get("items") or [])}
                except Exception as e:
                    out[f"core_{yr}_err"] = str(e)[:100]
    return out


def _espn_txns_from_html(txt):
    data = _espnfitt_blob(txt)
    if not data:
        return []
    pc = (data.get("page") or {}).get("content") or {}
    return pc.get("transactions") or []


def espn_txn_probe(sport):
    """Confirm ESPN date-anchored transaction pagination so we can walk the whole
    offseason. Fetch the base page + a couple /_/date/YYYYMMDD pages and report
    how many transactions each returns and their date range."""
    import httpx
    path = {"nba": "nba", "nfl": "nfl", "nhl": "nhl"}[sport]
    out = {"sport": sport, "pages": []}

    def summarize(label, url, txns):
        dates = sorted(t.get("date", "")[:10] for t in txns if t.get("date"))
        out["pages"].append({"label": label, "url": url, "n": len(txns),
                             "date_min": dates[0] if dates else None,
                             "date_max": dates[-1] if dates else None,
                             "samples": [t.get("description") for t in txns[:3]]})
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        for label, url in [
            ("base", f"https://www.espn.com/{path}/transactions"),
            ("date-0801", f"https://www.espn.com/{path}/transactions/_/date/20260801"),
            ("date-0701", f"https://www.espn.com/{path}/transactions/_/date/20260701"),
        ]:
            try:
                r = cx.get(url)
                summarize(label, url, _espn_txns_from_html(r.text or ""))
            except Exception as e:
                out["pages"].append({"label": label, "error": str(e)[:120]})
    return out


def espn_api_probe(sport):
    """Dump the transactions page's requestParams (what its data call uses) and
    try the candidate transaction APIs with pagination/date params to find one
    that returns the full offseason, not just the latest 25."""
    import httpx
    group, league, _ = _LEAGUE[sport]
    out = {"sport": sport}
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        try:
            r = cx.get(f"https://www.espn.com/{league}/transactions")
            data = _espnfitt_blob(r.text or "") or {}
            pc = (data.get("page") or {}).get("content") or {}
            out["requestParams"] = pc.get("requestParams")
            out["currentSeason"] = pc.get("currentSeason")
        except Exception as e:
            out["page_err"] = str(e)[:120]
        cands = [
            f"https://site.web.api.espn.com/apis/fitt/v3/sports/{group}/{league}/transactions?region=us&lang=en&limit=100",
            f"https://site.web.api.espn.com/apis/fitt/v3/sports/{group}/{league}/transactions?region=us&lang=en&dates=20260701-20261027",
            f"https://site.api.espn.com/apis/common/v3/sports/{group}/{league}/transactions?limit=100",
            f"https://site.web.api.espn.com/apis/v2/sports/{group}/{league}/transactions?limit=100",
        ]
        res = []
        for u in cands:
            row = {"url": u}
            try:
                r = cx.get(u)
                row["status"] = r.status_code
                try:
                    j = r.json()
                    if isinstance(j, dict):
                        items = j.get("transactions") or j.get("items") or []
                        row["keys"] = list(j.keys())[:8]
                        row["n"] = len(items) if isinstance(items, list) else None
                        if isinstance(items, list) and items:
                            s = items[0]
                            row["sample"] = ({k: s[k] for k in list(s)[:4]}
                                             if isinstance(s, dict) else str(s)[:100])
                except Exception:
                    row["body_head"] = (r.text or "")[:100]
            except Exception as e:
                row["error"] = str(e)[:120]
            res.append(row)
        out["candidates"] = res
    return out


def espn_team_txn_probe(sport):
    """Check whether ESPN's per-team transactions page embeds that team's full
    season history (which would let us build complete per-team boards by fetching
    each team), vs just the league-wide latest 25."""
    import httpx
    group, league, _ = _LEAGUE[sport]
    out = {"sport": sport}
    with httpx.Client(timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as cx:
        try:
            tr = cx.get(f"https://site.api.espn.com/apis/site/v2/sports/{group}/{league}/teams")
            t = tr.json()["sports"][0]["leagues"][0]["teams"][0]["team"]
            abbr = (t.get("abbreviation") or "").lower()
            out["team"] = t.get("displayName")
        except Exception as e:
            out["teams_err"] = str(e)[:120]
            return out
        import json as _j
        url = f"https://www.espn.com/{league}/team/transactions/_/name/{abbr}"
        out["url"] = url
        try:
            r = cx.get(url)
            out["status"] = r.status_code
            data = _espnfitt_blob(r.text or "") or {}
            pc = (data.get("page") or {}).get("content") or {}
            out["content_keys"] = list(pc.keys())[:40]
            txns = pc.get("transactions")
            out["txn_type"] = type(txns).__name__
            if isinstance(txns, list):
                out["txn_n"] = len(txns)
                out["txn_head"] = txns[:3]
            elif isinstance(txns, dict):
                out["txn_keys"] = list(txns.keys())[:20]
                out["txn_peek"] = _j.dumps(txns)[:1800]
        except Exception as e:
            out["error"] = str(e)[:150]
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
