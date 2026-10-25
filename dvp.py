"""
dvp.py — Defense vs Position tables, auto-generated from ESPN box scores.

For each defense, we walk the season's completed games and attribute every
opposing player's production to that defense, bucketed by the player's position
(QB/RB/WR/TE for the NFL). The result is the classic "what this defense allows
to each position" matchup table (targets, receptions, yards, TDs, fantasy
points), as season totals and per-game averages — exactly the kind of sheet
bettors build by hand.

All data comes from ESPN's free box-score summary feed (no paid quota). Heavy
work (one summary fetch per game) is cached; a finished game never changes.

This file starts with a PROBE so we can confirm ESPN's exact column labels and
whether player position is present in the box score before building the full
aggregator against the wrong field names.
"""
from __future__ import annotations

import datetime as dt
import re

import espn_provider as EP


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _f(x):
    """Parse an ESPN stat cell to float; '--' and blanks -> 0.0."""
    try:
        s = str(x).strip().replace(",", "")
        if s in ("", "--", "-"):
            return 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _col_index(labels, *targets):
    """Index of the first label matching any target (case-insensitive)."""
    low = [str(l).strip().lower() for l in (labels or [])]
    for t in targets:
        t = t.lower()
        if t in low:
            return low.index(t)
    return None


# RB covers fullbacks/halfbacks; everything else is its own bucket.
_POS_CANON = {"rb": "RB", "fb": "RB", "hb": "RB", "wr": "WR", "te": "TE", "qb": "QB"}

# Fantasy scoring (PPR by default). Exposed so the table can match a user's
# own source if their "points" column uses different weights.
_SCORING = {
    "ppr": {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
            "rush_yd": 0.1, "rush_td": 6.0,
            "pass_yd": 0.04, "pass_td": 4.0, "int": -2.0},
    "half": {"rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
             "rush_yd": 0.1, "rush_td": 6.0,
             "pass_yd": 0.04, "pass_td": 4.0, "int": -2.0},
    "standard": {"rec": 0.0, "rec_yd": 0.1, "rec_td": 6.0,
                 "rush_yd": 0.1, "rush_td": 6.0,
                 "pass_yd": 0.04, "pass_td": 4.0, "int": -2.0},
}


def nfl_season_year():
    """NFL season year: the league labels a season by the year it STARTS, so
    Jan–Jul belongs to the previous year's season."""
    today = dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def _recent_final_nfl_game_id():
    """Grab one recently-completed NFL game id from the current week's scoreboard,
    walking back a few weeks if the current week has no finals yet."""
    base = EP.SCOREBOARD["nfl"]
    yr = nfl_season_year()
    for params in ({}, {"dates": str(yr)}):
        try:
            data = EP._get(base, params) or {}
            for ev in data.get("events") or []:
                comp = (ev.get("competitions") or [{}])[0]
                if (((comp.get("status") or {}).get("type") or {}).get("completed")):
                    return str(ev.get("id"))
        except Exception:
            pass
    return None


def nfl_probe(game_id=None):
    """Dump one NFL game's box-score player groups so we can see the real labels
    (TGTS/REC/YDS/TD...), the group names (passing/rushing/receiving), and
    whether each athlete carries a position. Read-only, free feed."""
    gid = game_id or _recent_final_nfl_game_id()
    if not gid:
        return {"error": "no final NFL game id found to probe"}
    try:
        data = EP._get(EP.SUMMARY["nfl"], {"event": str(gid)}) or {}
    except Exception as e:
        return {"error": f"summary fetch failed: {str(e)[:150]}", "game_id": gid}
    out = {"game_id": gid, "teams": []}
    for team in ((data.get("boxscore") or {}).get("players") or []):
        tinfo = (team.get("team") or {})
        tentry = {"team": tinfo.get("displayName") or tinfo.get("abbreviation"),
                  "team_id": tinfo.get("id"), "groups": []}
        for grp in (team.get("statistics") or []):
            athletes = grp.get("athletes") or []
            sample = []
            for ath in athletes[:3]:
                a = ath.get("athlete") or {}
                sample.append({
                    "name": a.get("displayName"),
                    # is position present on the box-score athlete?
                    "position": ((a.get("position") or {}) or {}).get("abbreviation")
                    or ((a.get("position") or {}) or {}).get("name"),
                    "stats": ath.get("stats"),
                })
            tentry["groups"].append({
                "name": grp.get("name") or grp.get("type"),
                "labels": grp.get("labels"),
                "descriptions": grp.get("descriptions"),
                "keys": grp.get("keys"),
                "totals": grp.get("totals"),
                "sample_athletes": sample,
            })
        out["teams"].append(tentry)
    # also probe a roster so we know the position-map fallback works
    try:
        tid = (((data.get("boxscore") or {}).get("players") or [{}])[0]
               .get("team") or {}).get("id")
        rm = EP.team_roster_meta("nfl", tid) if tid else {}
        out["roster_sample"] = dict(list(rm.items())[:5])
        out["roster_count"] = len(rm)
    except Exception as e:
        out["roster_sample"] = {"error": str(e)[:120]}
    return out


# ---- NFL Defense vs Position --------------------------------------------

_rosterpos_cache = {}      # team_id -> {norm_name: pos}
_nfl_dvp_cache = {"ts": 0.0, "data": None}
_DVP_TTL = 6 * 3600


def _nfl_pos(team_id, name):
    """Position (RB/WR/TE/QB) for a player on a team, from the team roster.
    Box scores don't carry position, so the roster is the source of truth."""
    rm = _rosterpos_cache.get(team_id)
    if rm is None:
        try:
            meta = EP.team_roster_meta("nfl", team_id) or {}
        except Exception:
            meta = {}
        rm = {nm: (v.get("pos") or "").lower() for nm, v in meta.items()}
        _rosterpos_cache[team_id] = rm
    raw = rm.get(_norm(name), "")
    return _POS_CANON.get(raw)


def _nfl_season_game_ids():
    """Every completed NFL game id this season, via the weekly scoreboards
    (free feed). Deduped; walks weeks 1-18 of the regular season."""
    base = EP.SCOREBOARD["nfl"]
    yr = nfl_season_year()
    ids = []
    seen = set()
    for wk in range(1, 19):
        try:
            data = EP._get(base, {"dates": str(yr), "seasontype": 2, "week": wk}) or {}
        except Exception:
            continue
        evs = data.get("events") or []
        if not evs:
            continue
        for ev in evs:
            comp = (ev.get("competitions") or [{}])[0]
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            gid = str(ev.get("id") or "")
            if gid and gid not in seen:
                seen.add(gid)
                ids.append(gid)
    return ids


def _blank_pos():
    return {"gms": set(), "tar": 0.0, "rec": 0.0, "rec_yds": 0.0, "rec_td": 0.0,
            "rush_att": 0.0, "rush_yds": 0.0, "rush_td": 0.0,
            "cmp": 0.0, "att": 0.0, "pass_yds": 0.0, "pass_td": 0.0, "intc": 0.0}


def _accumulate_game(gid, acc, team_names):
    """Add one game's opposing production to each defense's per-position totals."""
    try:
        data = EP._get(EP.SUMMARY["nfl"], {"event": str(gid)}) or {}
    except Exception:
        return
    teams = (data.get("boxscore") or {}).get("players") or []
    if len(teams) != 2:
        return
    tids = [str(((t.get("team") or {}).get("id")) or "") for t in teams]
    tnm = [((t.get("team") or {}).get("displayName")
            or (t.get("team") or {}).get("abbreviation") or "") for t in teams]
    for i, t in enumerate(teams):
        off_id = tids[i]                       # offense producing the stats
        def_id = tids[1 - i]                   # defense that allowed them
        if not def_id:
            continue
        team_names[def_id] = tnm[1 - i]
        dbuckets = acc.setdefault(def_id, {p: _blank_pos() for p in ("QB", "RB", "WR", "TE")})
        for p in dbuckets.values():
            p["gms"].add(gid)
        for grp in (t.get("statistics") or []):
            gname = (grp.get("name") or "").lower()
            labels = grp.get("labels") or []
            aths = grp.get("athletes") or []
            if gname == "receiving":
                ci_rec = _col_index(labels, "rec")
                ci_yds = _col_index(labels, "yds")
                ci_td = _col_index(labels, "td")
                ci_tgt = _col_index(labels, "tgts", "tgt", "tar")
                for ath in aths:
                    nm = ((ath.get("athlete") or {}).get("displayName") or "")
                    pos = _nfl_pos(off_id, nm)
                    if pos not in ("RB", "WR", "TE"):
                        continue
                    s = ath.get("stats") or []
                    b = dbuckets[pos]
                    if ci_tgt is not None and ci_tgt < len(s):
                        b["tar"] += _f(s[ci_tgt])
                    if ci_rec is not None and ci_rec < len(s):
                        b["rec"] += _f(s[ci_rec])
                    if ci_yds is not None and ci_yds < len(s):
                        b["rec_yds"] += _f(s[ci_yds])
                    if ci_td is not None and ci_td < len(s):
                        b["rec_td"] += _f(s[ci_td])
            elif gname == "rushing":
                ci_car = _col_index(labels, "car")
                ci_yds = _col_index(labels, "yds")
                ci_td = _col_index(labels, "td")
                for ath in aths:
                    nm = ((ath.get("athlete") or {}).get("displayName") or "")
                    pos = _nfl_pos(off_id, nm)
                    if pos != "RB":
                        continue
                    s = ath.get("stats") or []
                    b = dbuckets["RB"]
                    if ci_car is not None and ci_car < len(s):
                        b["rush_att"] += _f(s[ci_car])
                    if ci_yds is not None and ci_yds < len(s):
                        b["rush_yds"] += _f(s[ci_yds])
                    if ci_td is not None and ci_td < len(s):
                        b["rush_td"] += _f(s[ci_td])
            elif gname == "passing":
                ci_cat = _col_index(labels, "c/att")
                ci_yds = _col_index(labels, "yds")
                ci_td = _col_index(labels, "td")
                ci_int = _col_index(labels, "int")
                for ath in aths:
                    nm = ((ath.get("athlete") or {}).get("displayName") or "")
                    pos = _nfl_pos(off_id, nm)
                    if pos != "QB":
                        continue
                    s = ath.get("stats") or []
                    b = dbuckets["QB"]
                    if ci_cat is not None and ci_cat < len(s) and "/" in str(s[ci_cat]):
                        cc, aa = str(s[ci_cat]).split("/", 1)
                        b["cmp"] += _f(cc)
                        b["att"] += _f(aa)
                    if ci_yds is not None and ci_yds < len(s):
                        b["pass_yds"] += _f(s[ci_yds])
                    if ci_td is not None and ci_td < len(s):
                        b["pass_td"] += _f(s[ci_td])
                    if ci_int is not None and ci_int < len(s):
                        b["intc"] += _f(s[ci_int])


def _pts_for(pos, b, sc):
    if pos == "QB":
        return (b["pass_yds"] * sc["pass_yd"] + b["pass_td"] * sc["pass_td"]
                + b["intc"] * sc["int"])
    pts = b["rec"] * sc["rec"] + b["rec_yds"] * sc["rec_yd"] + b["rec_td"] * sc["rec_td"]
    if pos == "RB":
        pts += b["rush_yds"] * sc["rush_yd"] + b["rush_td"] * sc["rush_td"]
    return pts


# Column schema per position: (key, header). Totals and per-game share the keys.
_NFL_COLS = {
    "TE": [("tar", "Tar"), ("rec", "Rec"), ("rec_yds", "Yds"), ("rec_td", "TD"), ("pts", "Pts")],
    "WR": [("tar", "Tar"), ("rec", "Rec"), ("rec_yds", "Yds"), ("rec_td", "TD"), ("pts", "Pts")],
    "RB": [("rush_att", "Car"), ("rush_yds", "RuYd"), ("rush_td", "RuTD"),
           ("rec", "Rec"), ("rec_yds", "ReYd"), ("rec_td", "ReTD"), ("pts", "Pts")],
    "QB": [("cmp", "Cmp"), ("att", "Att"), ("pass_yds", "Yds"),
           ("pass_td", "TD"), ("intc", "INT"), ("pts", "Pts")],
}
# Which stats are GOOD for the offense when high (green) — for QB, INT is flipped.
_INVERT = {("QB", "intc")}


def nfl_dvp(scoring="ppr"):
    """Defense vs Position for the NFL: per team, what each position does against
    them. Returns {positions:[...], scoring, season, updated, tables:{POS:[rows]}}.
    Cached; a finished game never changes, so this is cheap after the first build."""
    import time as _t
    c = _nfl_dvp_cache
    if c["data"] and _t.time() - c["ts"] < _DVP_TTL:
        return _recolor(c["data"], scoring)
    acc = {}
    team_names = {}
    gids = _nfl_season_game_ids()
    for gid in gids:
        _accumulate_game(gid, acc, team_names)
    sc = _SCORING.get(scoring, _SCORING["ppr"])
    tables = {}
    for pos in ("QB", "RB", "WR", "TE"):
        rows = []
        for def_id, buckets in acc.items():
            b = buckets[pos]
            g = len(b["gms"]) or 1
            tot = {k: round(v, 1) for k, v in b.items() if k != "gms"}
            tot["pts"] = round(_pts_for(pos, b, sc), 1)
            avg = {k: round(v / g, 1) for k, v in tot.items()}
            rows.append({"team": team_names.get(def_id, def_id), "team_id": def_id,
                         "gms": len(b["gms"]), "totals": tot, "avgs": avg})
        # sort by fantasy points allowed per game (worst defense first, like the image)
        rows.sort(key=lambda r: r["avgs"].get("pts", 0))
        tables[pos] = rows
    data = {"sport": "nfl", "season": nfl_season_year(),
            "updated": dt.datetime.utcnow().isoformat() + "Z",
            "games_counted": len(gids),
            "positions": ["QB", "RB", "WR", "TE"],
            "columns": _NFL_COLS, "tables": tables}
    _nfl_dvp_cache["data"] = data
    _nfl_dvp_cache["ts"] = _t.time()
    return _recolor(data, scoring)


def _recolor(data, scoring):
    """Attach a 0-1 rank per cell (for the heatmap) without refetching. Rank is
    within the column across all teams; 1 = allows the most (green for offense)."""
    out = dict(data)
    out["scoring"] = scoring
    tables = {}
    for pos, rows in data["tables"].items():
        cols = [k for k, _ in _NFL_COLS[pos]]
        ranks_tot = {k: _rank_map([r["totals"].get(k, 0) for r in rows]) for k in cols}
        ranks_avg = {k: _rank_map([r["avgs"].get(k, 0) for r in rows]) for k in cols}
        newrows = []
        for i, r in enumerate(rows):
            rr = dict(r)
            rr["heat_tot"] = {}
            rr["heat_avg"] = {}
            for k in cols:
                inv = (pos, k) in _INVERT
                ht = ranks_tot[k][i]
                ha = ranks_avg[k][i]
                rr["heat_tot"][k] = (1 - ht) if inv else ht
                rr["heat_avg"][k] = (1 - ha) if inv else ha
            newrows.append(rr)
        tables[pos] = newrows
    out["tables"] = tables
    return out


def _rank_map(values):
    """Map each value to its 0-1 percentile within the list (ties share rank)."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.5] * n
    for rank, i in enumerate(order):
        out[i] = rank / (n - 1)
    return out
