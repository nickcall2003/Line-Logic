"""
cfb_portal.py — FBS transfer-portal board for Line Logic, scored on PRODUCTION.

Restores thelinelogic.com/portal and ranks teams by the real value of who they
added and lost — what players actually DID, not their recruiting stars.

Data source: CollegeFootballData.com (your CFBD_API_KEY). Endpoints used:
  * /player/portal?year=Y            every portal entry (origin, destination, stars)
  * /ppa/players/season?year=Y-1..   offensive production (PPA = EPA/efficiency)
  * /stats/player/season?year=Y-1..  defensive production (tackles, TFL, sacks, INT, PD)
  * /teams/fbs?year=Y                the 136-team universe + logos/colors/conference

--------------------------------------------------------------------------------
SCORING  (per player -> "Impact" 0-100)
--------------------------------------------------------------------------------
1. Production: a player's last 1-2 seasons of production, recency-weighted, turned
   into a PERCENTILE within their position group. A workhorse RB and an efficient
   QB are compared to their own position, not to each other.
     - Offense  -> total PPA (all) from /ppa/players/season.
     - Defense  -> weighted box score (tackles, TFL, sacks, INT, PD).
   PPV = that percentile * 100.  (Arnold's poor OU/Auburn PPA -> low QB percentile.)

2. Unproven: a transfer with no prior college production (true freshman recruit,
   or anyone we can't match) is NEUTRAL -> Impact 50, the "middle line". Stars do
   NOT inflate it. A 5* rookie is a neutral add, not a haul.

3. Value over expectation (the "continuity" piece): we compare the production
   percentile to what the player's STAR rating predicted.
     VOE = PPV - star_expectation
     - produced & VOE >= +12  -> "Overachiever"   (3* who balled out)
     - produced & VOE <= -12  -> "Underachiever"  (5* who didn't, e.g. Arnold)
     - produced & in between   -> "Producer"
     - no production           -> "Unproven"

4. Where they came from: production is discounted by the origin program's level
   (SEC/Big Ten full, other P4 ~0.95, G5 ~0.85, FCS/unknown ~0.75), so a MAC star
   isn't valued like an SEC star.

TEAM NET = sum(incoming proven value) - sum(outgoing proven value), where a
player's value = max(0, Impact - 45) * origin_tier. So:
  * losing a genuine producer hurts a lot; losing an underachiever costs ~0,
  * adding a proven stud helps a lot; adding Arnold or a raw freshman adds ~0.
Teams are ranked 1..N by net.

Tune knobs: BASE, VOE_BAND, STAR_EXP, CONF_TIER, RECENCY.
"""
import datetime as dt
import json
import os
import re
import unicodedata

BASE_URL = "https://api.collegefootballdata.com"
OUT = "/data/cfb_portal.json"
_FALLBACK = "cfb_portal.json"

BASE = 45          # Impact below this contributes nothing to a team's net
VOE_BAND = 12      # how far past star expectation counts as over/under-achieving
RECENCY = {0: 1.0, 1: 0.5, 2: 0.25}   # weight by how many seasons back

# star rating -> expected production percentile (what a star SHOULD become)
STAR_EXP = {5: 90, 4: 76, 3: 52, 2: 44, 1: 40, 0: 42}

# origin program level -> value multiplier ("where they came from")
CONF_TIER = {
    "SEC": 1.00, "Big Ten": 1.00, "ACC": 0.95, "Big 12": 0.95,
    "Pac-12": 0.95, "American Athletic": 0.85, "Mountain West": 0.85,
    "Sun Belt": 0.82, "Mid-American": 0.82, "Conference USA": 0.80,
    "FBS Independents": 0.90,
}
_DEF_W = {"TOT": 0.4, "SOLO": 0.2, "TFL": 2.0, "SACKS": 3.0, "INT": 4.0,
          "PD": 1.5, "QB HUR": 1.0, "FF": 2.0}

# position -> coarse group used for percentile buckets
_POS_GROUP = {
    "QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL",
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL", "EDGE": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "ATH": "WR",
}
_OFF = {"QB", "RB", "WR", "TE", "OL"}


def _key():
    k = (os.environ.get("CFBD_API_KEY") or os.environ.get("CFB_API_KEY") or "").strip()
    if not k:
        raise SystemExit("CFBD_API_KEY not set")
    return k


def _default_year():
    # Always the current season's portal cycle ("this year"). CFBD tags the
    # Dec->summer movement that feeds the 2026 season as season=2026.
    return dt.datetime.utcnow().year


def _get(path, key, params=None):
    import httpx
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    r = httpx.get(BASE_URL + path, params=params or {}, headers=headers, timeout=60.0)
    r.raise_for_status()
    return r.json()


def _get_safe(path, key, params=None):
    try:
        return _get(path, key, params) or []
    except Exception:
        return []


def _num(v):
    try:
        f = float(v)
        return f if f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


def _norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _group(pos):
    return _POS_GROUP.get((pos or "").upper().strip(), "OTHER")


# ----------------------------- production indexes -----------------------------

def _ppa_index(year, key):
    """normname -> {'prod':weighted_total_ppa, 'pos':.., 'seasons':n} (offense)."""
    idx = {}
    for back in (1, 2):
        season = year - back
        w = RECENCY.get(back, 0)
        rows = _get_safe("/ppa/players/season", key,
                         {"year": season, "excludeGarbageTime": "true"})
        for r in rows:
            nm = _norm(r.get("name"))
            if not nm:
                continue
            tp = r.get("totalPPA") or {}
            val = _num(tp.get("all"))
            rec = idx.setdefault(nm, {"prod": 0.0, "pos": r.get("position") or "",
                                      "seasons": 0})
            rec["prod"] += max(0.0, val) * w
            rec["seasons"] += 1
            if not rec["pos"]:
                rec["pos"] = r.get("position") or ""
    return idx


def _def_index(year, key):
    """normname -> {'prod':weighted_box_score, 'pos':.., 'seasons':n} (defense)."""
    idx = {}
    for back in (1, 2):
        season = year - back
        w = RECENCY.get(back, 0)
        rows = _get_safe("/stats/player/season", key,
                         {"year": season, "category": "defensive"})
        agg = {}
        for r in rows:
            nm = _norm(r.get("player"))
            if not nm:
                continue
            st = (r.get("statType") or "").upper()
            wt = _DEF_W.get(st)
            if wt is None:
                continue
            a = agg.setdefault(nm, {"prod": 0.0, "pos": r.get("position") or ""})
            a["prod"] += _num(r.get("stat")) * wt
            if not a["pos"]:
                a["pos"] = r.get("position") or ""
        for nm, a in agg.items():
            rec = idx.setdefault(nm, {"prod": 0.0, "pos": a["pos"], "seasons": 0})
            rec["prod"] += a["prod"] * w
            rec["seasons"] += 1
            if not rec["pos"]:
                rec["pos"] = a["pos"]
    return idx


def _percentile_funcs(prod_map):
    """Build, per position group, a sorted list of production values so we can
    rank any player as a percentile within their group."""
    import bisect
    buckets = {}
    for rec in prod_map.values():
        g = _group(rec.get("pos"))
        if rec["prod"] > 0:
            buckets.setdefault(g, []).append(rec["prod"])
    for g in buckets:
        buckets[g].sort()

    def pct(group, value):
        arr = buckets.get(group)
        if not arr or value <= 0:
            return None
        i = bisect.bisect_right(arr, value)
        return 100.0 * i / len(arr)
    return pct, {g: len(v) for g, v in buckets.items()}


# --------------------------------- universe -----------------------------------

def _fbs_universe(year, key):
    universe = {}
    for t in _get_safe("/teams/fbs", key, {"year": year}):
        name = t.get("school") or t.get("team") or ""
        if not name:
            continue
        logos = t.get("logos") or []
        universe[name] = {"logo": logos[0] if logos else "",
                          "color": t.get("color") or "",
                          "conference": t.get("conference") or ""}
    return universe


def _tier(conf):
    return CONF_TIER.get(conf or "", 0.80)


def _blank(name, meta):
    m = meta or {}
    return {"team": name, "logo": m.get("logo", ""), "color": m.get("color", ""),
            "conference": m.get("conference", ""),
            "net": 0.0, "added": 0.0, "lost": 0.0,
            "in_n": 0, "out_n": 0, "in_proven": 0, "out_proven": 0,
            "incoming": [], "outgoing": []}


# --------------------------------- the build ----------------------------------

def _score_player(e, prod_map, pct, team_conf):
    """Return a scored player dict + its team value (>=0)."""
    first = (e.get("firstName") or "").strip()
    last = (e.get("lastName") or "").strip()
    full = (first + " " + last).strip() or "Unknown"
    pos = e.get("position") or ""
    stars = int(e.get("stars") or 0)
    origin = (e.get("origin") or "").strip()

    rec = prod_map.get(_norm(full))
    ppv = None
    if rec and rec["prod"] > 0:
        g = _group(rec.get("pos") or pos)
        ppv = pct(g, rec["prod"])
    proven = ppv is not None
    impact = round(ppv, 1) if proven else 50.0   # neutral middle for unproven

    star_exp = STAR_EXP.get(stars, 42)
    voe = impact - star_exp
    if not proven:
        tag = "Unproven"
    elif voe >= VOE_BAND:
        tag = "Overachiever"
    elif voe <= -VOE_BAND:
        tag = "Underachiever"
    else:
        tag = "Producer"

    tier = _tier(team_conf)
    value = max(0.0, impact - BASE) * tier

    return ({"name": full, "pos": pos, "stars": stars,
             "impact": impact, "tag": tag, "proven": proven,
             "origin": origin, "destination": (e.get("destination") or "").strip(),
             "date": (e.get("transferDate") or "")[:10]},
            value, proven)


def build(year=None):
    key = _key()
    year = int(year) if year else _default_year()

    universe = _fbs_universe(year, key)
    conf_of = {name: m["conference"] for name, m in universe.items()}

    prod_map = _ppa_index(year, key)
    for nm, rec in _def_index(year, key).items():
        if nm in prod_map:
            # keep the stronger signal (same player could appear both; rare)
            if rec["prod"] > prod_map[nm]["prod"]:
                prod_map[nm] = rec
        else:
            prod_map[nm] = rec
    pct, bucket_sizes = _percentile_funcs(prod_map)

    entries = _get_safe("/player/portal", key, {"year": year})
    teams = {name: _blank(name, meta) for name, meta in universe.items()}

    def slot(name):
        if name not in teams:
            teams[name] = _blank(name, universe.get(name))
        return teams[name]

    matched = 0
    for e in entries:
        origin = (e.get("origin") or "").strip()
        dest = (e.get("destination") or "").strip()

        if origin:                      # value is judged on the origin's level
            p, val, proven = _score_player(e, prod_map, pct, conf_of.get(origin))
            matched += 1 if proven else 0
            o = slot(origin)
            o["outgoing"].append(p)
            o["out_n"] += 1
            o["lost"] += val
            o["out_proven"] += 1 if proven else 0
        if dest:
            p, val, proven = _score_player(e, prod_map, pct, conf_of.get(origin))
            d = slot(dest)
            d["incoming"].append(p)
            d["in_n"] += 1
            d["added"] += val
            d["in_proven"] += 1 if proven else 0

    rows = list(teams.values())
    for t in rows:
        t["net"] = round((t["added"] - t["lost"]) / 10.0, 2)
        t["added"] = round(t["added"] / 10.0, 2)
        t["lost"] = round(t["lost"] / 10.0, 2)
        t["incoming"].sort(key=lambda p: p["impact"], reverse=True)
        t["outgoing"].sort(key=lambda p: p["impact"], reverse=True)

    rows.sort(key=lambda t: (t["net"], t["added"], -t["out_n"]), reverse=True)
    for i, t in enumerate(rows, 1):
        t["rank"] = i

    total_players = sum(1 for e in entries if e.get("origin"))
    out = {
        "season": year,
        "built_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count_entries": len(entries),
        "ranked": len(rows),
        "metric": "Impact = prior-production percentile within position; "
                  "team net = proven value added minus lost.",
        "diag": {"production_players_indexed": len(prod_map),
                 "bucket_sizes": bucket_sizes,
                 "portal_with_origin": total_players,
                 "matched_to_production": matched,
                 "match_rate": round(matched / total_players, 3) if total_players else 0},
        "teams": rows,
    }
    _save(out)
    return out


def _save(out):
    for path in (OUT, _FALLBACK):
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f)
            return path
        except Exception:
            continue
    return None


def load():
    for p in (OUT, _FALLBACK):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            continue
    return None


if __name__ == "__main__":
    import sys
    data = build(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    d = data["diag"]
    print(f"season {data['season']} | {data['ranked']} teams | "
          f"{data['count_entries']} entries | matched {d['matched_to_production']}"
          f"/{d['portal_with_origin']} ({d['match_rate']:.0%})")
    for t in data["teams"][:5]:
        print(f"  #{t['rank']:>3} {t['team']:<22} net {t['net']:+.2f}  "
              f"in {t['in_n']}/out {t['out_n']}")
