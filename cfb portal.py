"""
cfb_portal.py — FBS transfer-portal board for Line Logic.

Rebuilds the /portal feature: every FBS team, who left and who arrived via the
transfer portal, and a 1-N ranking of each team's portal haul.

Data source: CollegeFootballData.com (the same CFBD_API_KEY you already use for
NCAAF SP+ ratings). Two endpoints:
  * /player/portal?year=YYYY  -> every portal entry (origin, destination, rating)
  * /teams/fbs?year=YYYY      -> the full FBS universe (so teams with no moves
                                 still appear, plus logos & colors)

Ranking metric ("Portal Net"): sum of INCOMING player composite ratings minus
sum of OUTGOING player composite ratings. This rewards adding talent and
penalizes losing it — the net view of a team's offseason. The board also carries
an "Added" score (incoming only) and blue-chip (4-5*) counts so you can sort by
whichever lens you want on the page.

CFBD composite ratings are ~0.75-1.00 on the 247 scale; players with no rating
(walk-ons, low-rated) still count toward the in/out COUNTS but contribute 0 to
the rating totals, so counts and rating-net tell slightly different stories by
design.

Output JSON shape (saved to /data/cfb_portal.json, falls back to ./):
{
  "season": 2026,
  "built_at": "2026-10-16T16:20:00Z",
  "count_entries": 3412,
  "teams": [
     { "team": "...", "logo": "...", "color": "#...", "conference": "...",
       "rank": 1, "net": 12.34, "added": 18.01, "lost": 5.67,
       "in_n": 24, "out_n": 11, "in_bluechip": 6, "out_bluechip": 1,
       "incoming": [ {name,pos,stars,rating,origin,date}, ... ],
       "outgoing": [ {name,pos,stars,rating,destination,date}, ... ] },
     ...
  ]
}
"""
import datetime as dt
import json
import os

BASE = "https://api.collegefootballdata.com"
OUT = "/data/cfb_portal.json"       # overridden by the route; volume-backed
_FALLBACK = "cfb_portal.json"       # local, if /data isn't writable


def _key():
    k = os.environ.get("CFBD_API_KEY") or os.environ.get("CFB_API_KEY") or ""
    k = k.strip()
    if not k:
        raise SystemExit("CFBD_API_KEY not set")
    return k


def _default_year():
    # Always the current season's portal cycle ("this year"). CFBD tags the
    # Dec->summer movement that feeds the 2026 season as season=2026, so the
    # current calendar year is exactly the cycle people mean by "this year".
    return dt.datetime.utcnow().year


def _get(path, key, params=None):
    import httpx
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    r = httpx.get(BASE + path, params=params or {}, headers=headers, timeout=45.0)
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        f = float(v)
        return f if f == f else 0.0   # drop NaN
    except (TypeError, ValueError):
        return 0.0


def _fbs_universe(year, key):
    """team name -> {logo, color, conference}. Tolerant of CFBD field drift."""
    universe = {}
    try:
        rows = _get("/teams/fbs", key, {"year": year})
    except Exception:
        rows = []
    for t in rows or []:
        name = t.get("school") or t.get("team") or ""
        if not name:
            continue
        logos = t.get("logos") or []
        logo = logos[0] if logos else ""
        universe[name] = {
            "logo": logo,
            "color": t.get("color") or "",
            "conference": t.get("conference") or "",
        }
    return universe


def _blank(name, meta):
    m = meta or {}
    return {
        "team": name, "logo": m.get("logo", ""), "color": m.get("color", ""),
        "conference": m.get("conference", ""),
        "net": 0.0, "added": 0.0, "lost": 0.0,
        "in_n": 0, "out_n": 0, "in_bluechip": 0, "out_bluechip": 0,
        "incoming": [], "outgoing": [],
    }


def build(year=None):
    key = _key()
    year = int(year) if year else _default_year()

    universe = _fbs_universe(year, key)
    entries = _get("/player/portal", key, {"year": year}) or []

    teams = {name: _blank(name, meta) for name, meta in universe.items()}

    def slot(name):
        if name not in teams:
            teams[name] = _blank(name, universe.get(name))
        return teams[name]

    for e in entries:
        first = (e.get("firstName") or "").strip()
        last = (e.get("lastName") or "").strip()
        full = (first + " " + last).strip() or "Unknown"
        pos = e.get("position") or ""
        stars = int(e.get("stars") or 0)
        rating = _num(e.get("rating"))
        origin = (e.get("origin") or "").strip()
        dest = (e.get("destination") or "").strip()
        date = (e.get("transferDate") or "")[:10]
        blue = stars >= 4

        # Outgoing: counted against the origin team (FBS only — ignore moves
        # from/to non-FBS schools that aren't in the universe when we can).
        if origin:
            o = slot(origin)
            o["outgoing"].append({"name": full, "pos": pos, "stars": stars,
                                  "rating": rating,
                                  "destination": dest or "Undecided", "date": date})
            o["out_n"] += 1
            o["lost"] += rating
            if blue:
                o["out_bluechip"] += 1

        # Incoming: credited to the destination (skip uncommitted — no dest yet).
        if dest:
            d = slot(dest)
            d["incoming"].append({"name": full, "pos": pos, "stars": stars,
                                  "rating": rating,
                                  "origin": origin or "Unknown", "date": date})
            d["in_n"] += 1
            d["added"] += rating
            if blue:
                d["in_bluechip"] += 1

    rows = list(teams.values())
    for t in rows:
        t["net"] = round(t["added"] - t["lost"], 3)
        t["added"] = round(t["added"], 3)
        t["lost"] = round(t["lost"], 3)
        # sort each team's lists by rating desc so the best moves show first
        t["incoming"].sort(key=lambda p: (p["stars"], p["rating"]), reverse=True)
        t["outgoing"].sort(key=lambda p: (p["stars"], p["rating"]), reverse=True)

    # Rank 1..N by net haul (ties broken by added, then fewer losses).
    rows.sort(key=lambda t: (t["net"], t["added"], -t["out_n"]), reverse=True)
    for i, t in enumerate(rows, 1):
        t["rank"] = i

    out = {
        "season": year,
        "built_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count_entries": len(entries),
        "ranked": len(rows),
        "metric": "Portal Net = sum(incoming composite rating) - sum(outgoing)",
        "teams": rows,
    }
    _save(out)
    return out


def _save(out):
    path = OUT
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(out, f)
        return path
    except Exception:
        try:
            with open(_FALLBACK, "w") as f:
                json.dump(out, f)
            return _FALLBACK
        except Exception:
            return None


def load():
    """Return the cached board, or None if nothing built yet."""
    for p in (OUT, _FALLBACK):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            continue
    return None


if __name__ == "__main__":
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else None
    data = build(yr)
    print(f"built {data['ranked']} teams from {data['count_entries']} entries "
          f"(season {data['season']})")
    top = data["teams"][:5]
    for t in top:
        print(f"  #{t['rank']:>3} {t['team']:<24} net {t['net']:+.2f}  "
              f"in {t['in_n']} / out {t['out_n']}")
