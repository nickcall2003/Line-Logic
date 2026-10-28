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


# ---- NBA Defense vs Position --------------------------------------------
# NBA box scores are simpler than NFL: one stat group per team listing every
# player with a full line (MIN, FG, 3PT, FT, OREB, DREB, REB, AST, STL, BLK,
# TO, PF, PTS). We attribute each player's line to the defense they faced and
# bucket by position resolved from the team roster.

_nba_dvp_cache = {"ts": 0.0, "data": None}
# ESPN's NBA roster reports the GENERIC position (G / F / C), not PG/SG/SF/PF,
# so those three are the honest granularity we can bucket by. (Mapping guards to
# SG and forwards to SF left PG/PF permanently empty — the bug reported.)
_NBA_POS_CANON = {"pg": "G", "sg": "G", "g": "G", "gf": "G", "fg": "F",
                  "sf": "F", "pf": "F", "f": "F", "fc": "F", "cf": "C", "c": "C"}


_valid_ids_cache = {}


def ncaaf_diag():
    """TEMP: where do CFB games go? Per-week raw/completed/kept, the valid-team
    set size, and sample dropped games with in_valid flags."""
    sport = "ncaaf"
    base = EP.SCOREBOARD.get(sport)
    today = dt.date.today()
    yr = today.year if today.month >= 8 else today.year - 1
    group = "80"
    valid = _valid_team_ids(sport, group)
    out = {"year": yr, "valid_n": len(valid), "weeks": [],
           "total_raw": 0, "total_completed": 0, "total_kept": 0, "dropped": []}
    for wk in range(1, 17):
        p = {"dates": str(yr), "seasontype": 2, "week": wk, "limit": "900", "groups": group}
        try:
            data = EP._get(base, p) or {}
        except Exception as e:
            out["weeks"].append({"wk": wk, "err": str(e)[:80]})
            continue
        evs = data.get("events") or []
        comp_n = kept = 0
        for ev in evs:
            comp = (ev.get("competitions") or [{}])[0]
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            comp_n += 1
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            ids = [str((c.get("team") or {}).get("id")) for c in cs]
            nms = [(c.get("team") or {}).get("displayName") for c in cs]
            if valid and (ids[0] not in valid or ids[1] not in valid):
                if len(out["dropped"]) < 8:
                    out["dropped"].append({"wk": wk, "teams": nms, "ids": ids,
                                           "in_valid": [i in valid for i in ids]})
                continue
            kept += 1
        out["weeks"].append({"wk": wk, "raw": len(evs), "done": comp_n, "kept": kept})
        out["total_raw"] += len(evs)
        out["total_completed"] += comp_n
        out["total_kept"] += kept
    # does a DATE-based query break the 25 cap? test a Saturday + a 7-day range.
    def _count(params):
        try:
            d = EP._get(base, dict(params, limit="900", groups=group)) or {}
            evs = d.get("events") or []
            done = sum(1 for e in evs if ((((e.get("competitions") or [{}])[0]
                       .get("status") or {}).get("type") or {}).get("completed")))
            return {"raw": len(evs), "done": done}
        except Exception as e:
            return {"err": str(e)[:80]}
    out["datetest"] = {
        "sat_page2": _count({"dates": "20261017", "page": "2"}),
        "sat_page1_limit50": _count({"dates": "20261017", "limit": "50"}),
    }
    # ESPN CORE API week-events — usually returns the full list, no 25 cap.
    import httpx
    core = (f"https://sports.core.api.espn.com/v2/sports/football/leagues/"
            f"college-football/seasons/{yr}/types/2/weeks/7/events")
    try:
        with httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as cx:
            r = cx.get(core, params={"limit": 1000})
            j = r.json() or {}
            out["core_week7"] = {"count": j.get("count"), "pageCount": j.get("pageCount"),
                                 "n_items": len(j.get("items") or [])}
            ref = ((j.get("items") or [{}])[0] or {}).get("$ref")
            if ref:
                ev = cx.get(ref).json() or {}
                comp = (ev.get("competitions") or [{}])[0]
                cs = comp.get("competitors") or []
                # is the score inline, or a $ref we'd have to fetch too?
                sc = (cs[0].get("score") if cs else None)
                out["core_event_sample"] = {
                    "date": ev.get("date"), "comp_keys": list(comp.keys())[:12],
                    "score_type": type(sc).__name__,
                    "score_val": (sc if isinstance(sc, (str, int, float)) else
                                  (list(sc.keys())[:6] if isinstance(sc, dict) else None))}
    except Exception as e:
        out["core_week7"] = {"err": str(e)[:120]}
    return out


def _valid_team_ids(sport, group=None):
    """Set of real team ids for a league (e.g. the 30 NBA franchises, or FBS-only
    for CFB) from the teams endpoint, so exhibitions vs international clubs and
    FCS opponents can be filtered out. Cached."""
    key = (sport, group)
    if key in _valid_ids_cache:
        return _valid_ids_cache[key]
    ids = set()
    base = EP.SCOREBOARD.get(sport)
    if base:
        url = base.rsplit("/scoreboard", 1)[0] + "/teams"
        params = {"limit": "900"}
        if group:
            params["groups"] = group
        try:
            data = EP._get(url, params) or {}
            for lg in ((data.get("sports") or [{}])[0].get("leagues") or []):
                for t in (lg.get("teams") or []):
                    tid = ((t.get("team") or {}) or {}).get("id")
                    if tid:
                        ids.add(str(tid))
        except Exception:
            pass
    _valid_ids_cache[key] = ids
    return ids


def _pos_generic(sport, team_id, name, canon):
    key = (sport, team_id)
    rm = _rosterpos_cache.get(key)
    if rm is None:
        try:
            meta = EP.team_roster_meta(sport, team_id) or {}
        except Exception:
            meta = {}
        rm = {nm: (v.get("pos") or "").lower() for nm, v in meta.items()}
        _rosterpos_cache[key] = rm
    return canon.get(rm.get(_norm(name), ""))


def _nba_season_game_ids(lookback_days=80):
    """Completed NBA game ids over the recent window (season-to-date, capped).
    Early season that's every game; deep in the season it's a rolling window,
    which is what a DvP table actually wants."""
    base = EP.SCOREBOARD["nba"]
    today = dt.date.today()
    # NBA seasons open late Oct; don't scan before the opener.
    season_open = dt.date(today.year if today.month >= 10 else today.year - 1, 10, 1)
    start = max(season_open, today - dt.timedelta(days=lookback_days))
    ids = []
    seen = set()
    d = today
    while d >= start:
        try:
            data = EP._get(base, {"dates": d.strftime("%Y%m%d")}) or {}
            for ev in data.get("events") or []:
                comp = (ev.get("competitions") or [{}])[0]
                if (((comp.get("status") or {}).get("type") or {}).get("completed")):
                    gid = str(ev.get("id") or "")
                    if gid and gid not in seen:
                        seen.add(gid)
                        ids.append(gid)
        except Exception:
            pass
        d -= dt.timedelta(days=1)
    return ids


def _nba_blank():
    return {"gms": set(), "pts": 0.0, "reb": 0.0, "ast": 0.0, "fg3": 0.0,
            "stl": 0.0, "blk": 0.0, "to": 0.0}


def _nba_accumulate(gid, acc, team_names, valid=None):
    try:
        data = EP._get(EP.SUMMARY["nba"], {"event": str(gid)}) or {}
    except Exception:
        return
    teams = (data.get("boxscore") or {}).get("players") or []
    if len(teams) != 2:
        return
    tids = [str(((t.get("team") or {}).get("id")) or "") for t in teams]
    # skip exhibitions vs non-NBA clubs (e.g. preseason vs international teams)
    if valid and (tids[0] not in valid or tids[1] not in valid):
        return
    tnm = [((t.get("team") or {}).get("displayName")
            or (t.get("team") or {}).get("abbreviation") or "") for t in teams]
    for i, t in enumerate(teams):
        off_id, def_id = tids[i], tids[1 - i]
        if not def_id:
            continue
        team_names[def_id] = tnm[1 - i]
        dbuckets = acc.setdefault(def_id, {p: _nba_blank() for p in _NBA_POSITIONS})
        for p in dbuckets.values():
            p["gms"].add(gid)
        for grp in (t.get("statistics") or []):
            labels = grp.get("labels") or []
            ci = {"pts": _col_index(labels, "pts"), "reb": _col_index(labels, "reb"),
                  "ast": _col_index(labels, "ast"), "fg3": _col_index(labels, "3pt"),
                  "stl": _col_index(labels, "stl"), "blk": _col_index(labels, "blk"),
                  "to": _col_index(labels, "to", "tov")}
            for ath in (grp.get("athletes") or []):
                nm = ((ath.get("athlete") or {}).get("displayName") or "")
                pos = _pos_generic("nba", off_id, nm, _NBA_POS_CANON)
                if pos not in dbuckets:
                    continue
                s = ath.get("stats") or []
                if not s:
                    continue
                b = dbuckets[pos]
                for key, idx in ci.items():
                    if idx is None or idx >= len(s):
                        continue
                    raw = str(s[idx])
                    # "3PT" is made-attempted (e.g. "4-9") -> take made
                    if key == "fg3" and "-" in raw:
                        raw = raw.split("-")[0]
                    b[key] += _f(raw)


_NBA_COLS = [("pts", "Pts"), ("reb", "Reb"), ("ast", "Ast"), ("fg3", "3PM"),
             ("stl", "Stl"), ("blk", "Blk"), ("to", "TO")]
_NBA_POSITIONS = ["G", "F", "C"]


def nba_dvp(scoring=None):
    """NBA Defense vs Position: per team, what each position (G/F/C) produces
    against them — points, rebounds, assists, 3s, steals, blocks, turnovers —
    season totals + per-game. Filtered to the 30 NBA franchises so preseason
    exhibitions vs international clubs don't leak in. Cached."""
    import time as _t
    c = _nba_dvp_cache
    if c["data"] and _t.time() - c["ts"] < _DVP_TTL:
        return _recolor_generic(c["data"])
    acc, team_names = {}, {}
    valid = _valid_team_ids("nba")
    gids = _nba_season_game_ids()
    for gid in gids:
        _nba_accumulate(gid, acc, team_names, valid=valid)
    tables = {}
    for pos in _NBA_POSITIONS:
        rows = []
        for def_id, buckets in acc.items():
            b = buckets[pos]
            g = len(b["gms"]) or 1
            tot = {k: round(v, 1) for k, v in b.items() if k != "gms"}
            avg = {k: round(v / g, 1) for k, v in tot.items()}
            rows.append({"team": team_names.get(def_id, def_id), "team_id": def_id,
                         "gms": len(b["gms"]), "totals": tot, "avgs": avg})
        rows.sort(key=lambda r: r["avgs"].get("pts", 0))
        tables[pos] = rows
    data = {"sport": "nba",
            "updated": dt.datetime.utcnow().isoformat() + "Z",
            "games_counted": len(gids),
            "positions": _NBA_POSITIONS,
            "columns": {p: _NBA_COLS for p in _NBA_POSITIONS},
            "tables": tables}
    _nba_dvp_cache["data"] = data
    _nba_dvp_cache["ts"] = _t.time()
    return _recolor_generic(data)


def _recolor_generic(data):
    """Attach a 0-1 heatmap rank per cell (higher value -> greener), for sheets
    where every stat is 'more allowed = softer matchup'. Nothing inverted."""
    out = dict(data)
    tables = {}
    for pos, rows in data["tables"].items():
        cols = [k for k, _ in data["columns"][pos]]
        rt = {k: _rank_map([r["totals"].get(k, 0) for r in rows]) for k in cols}
        ra = {k: _rank_map([r["avgs"].get(k, 0) for r in rows]) for k in cols}
        nr = []
        for i, r in enumerate(rows):
            rr = dict(r)
            rr["heat_tot"] = {k: rt[k][i] for k in cols}
            rr["heat_avg"] = {k: ra[k][i] for k in cols}
            nr.append(rr)
        tables[pos] = nr
    out["tables"] = tables
    return out


def nba_probe(game_id=None):
    """Confirm NBA box-score labels + roster positions before trusting the grid."""
    gid = game_id
    if not gid:
        base = EP.SCOREBOARD["nba"]
        d = dt.date.today()
        for _ in range(10):
            try:
                data = EP._get(base, {"dates": d.strftime("%Y%m%d")}) or {}
                for ev in data.get("events") or []:
                    comp = (ev.get("competitions") or [{}])[0]
                    if (((comp.get("status") or {}).get("type") or {}).get("completed")):
                        gid = str(ev.get("id"))
                        break
            except Exception:
                pass
            if gid:
                break
            d -= dt.timedelta(days=1)
    if not gid:
        return {"error": "no completed NBA game found to probe"}
    try:
        data = EP._get(EP.SUMMARY["nba"], {"event": str(gid)}) or {}
    except Exception as e:
        return {"error": str(e)[:150], "game_id": gid}
    out = {"game_id": gid, "teams": []}
    for t in ((data.get("boxscore") or {}).get("players") or []):
        tinfo = t.get("team") or {}
        tid = tinfo.get("id")
        grps = []
        for grp in (t.get("statistics") or []):
            aths = grp.get("athletes") or []
            sample = []
            for ath in aths[:4]:
                a = ath.get("athlete") or {}
                nm = a.get("displayName")
                sample.append({"name": nm,
                               "roster_pos": _pos_generic("nba", str(tid), nm, _NBA_POS_CANON),
                               "stats": ath.get("stats")})
            grps.append({"name": grp.get("name"), "labels": grp.get("labels"),
                         "sample": sample})
        out["teams"].append({"team": tinfo.get("displayName"), "team_id": tid, "groups": grps})
    return out


# ---- College team ratings (SRS: opponent-adjusted margin) ----------------
# A full KenPom clone needs possession-level box scores for all of D1 — too
# heavy, and CBB barely opens before November. Instead we compute the Simple
# Rating System from final scores: each team's rating = its average scoring
# margin plus the average rating of its opponents (solved iteratively). It's a
# real, respected, opponent-adjusted power rating (Sports-Reference uses it),
# computable from scores alone, so it auto-updates with zero box-score cost.

_ratings_cache = {}        # sport -> (ts, data)
_RATINGS_TTL = 6 * 3600
_MARGIN_CAP = 24           # cap blowouts so one 50-pt win can't distort a rating


def _college_games(sport):
    """[(home_id,home_name,home_pts,away_id,away_name,away_pts)] for completed
    games this season, from the weekly/daily scoreboards (free, no box scores)."""
    base = EP.SCOREBOARD.get(sport)
    if not base:
        return []
    today = dt.date.today()
    yr = today.year if today.month >= 8 else today.year - 1
    # FBS football / D1 basketball groups so we sweep the full slate, not top-25.
    group = {"ncaaf": "80", "ncaab": "50", "wncaab": "50"}.get(sport)
    # Only rate games between members of this set — FBS-only for CFB (FCS
    # opponents excluded), D1-only for CBB. A game vs a non-member is dropped,
    # which also keeps records clean (SRS counts member-vs-member only).
    valid = _valid_team_ids(sport, group)
    out = []
    seen = set()

    def _pull(params):
        p = dict(params)
        p["limit"] = "900"
        if group:
            p["groups"] = group
        try:
            data = EP._get(base, p) or {}
        except Exception:
            return
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            gid = str(ev.get("id") or "")
            if not gid or gid in seen:
                continue
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            try:
                h = next(c for c in cs if c.get("homeAway") == "home")
                a = next(c for c in cs if c.get("homeAway") == "away")
            except StopIteration:
                continue
            try:
                hp, ap = int(h.get("score")), int(a.get("score"))
            except (TypeError, ValueError):
                continue
            ht, at = h.get("team") or {}, a.get("team") or {}
            hid, aid = str(ht.get("id")), str(at.get("id"))
            # drop FCS/non-D1 opponents entirely
            if valid and (hid not in valid or aid not in valid):
                continue
            seen.add(gid)
            out.append((hid, ht.get("displayName") or ht.get("shortDisplayName"),
                        hp, aid, at.get("displayName") or at.get("shortDisplayName"), ap))

    if sport == "ncaaf":
        for wk in range(1, 17):
            _pull({"dates": str(yr), "seasontype": 2, "week": wk})
    else:
        # basketball: scan the season to date (opens early Nov); the member-set
        # filter removes any exhibition vs a non-D1 school.
        season_open = dt.date(yr, 10, 15)
        start = max(season_open, today - dt.timedelta(days=120))
        d = today
        while d >= start:
            _pull({"dates": d.strftime("%Y%m%d")})
            d -= dt.timedelta(days=1)
    return out


def _srs(games):
    """Solve the Simple Rating System. Returns per-team dict with record, points
    for/against per game, margin, SRS (opponent-adjusted) and SOS."""
    rec = {}   # id -> {name, g, pf, pa, mov_sum, opps:[ids]}
    for hid, hnm, hp, aid, anm, ap in games:
        for tid, nm, pf, pa, opp in ((hid, hnm, hp, ap, aid), (aid, anm, ap, hp, hid)):
            r = rec.setdefault(tid, {"name": nm, "g": 0, "pf": 0, "pa": 0,
                                     "mov": 0.0, "opps": []})
            r["name"] = nm or r["name"]
            r["g"] += 1
            r["pf"] += pf
            r["pa"] += pa
            m = pf - pa
            m = max(-_MARGIN_CAP, min(_MARGIN_CAP, m))
            r["mov"] += m
            r["opps"].append(opp)
    if not rec:
        return {}
    # iterate: rating = avg margin + avg opponent rating
    rating = {t: (r["mov"] / r["g"]) for t, r in rec.items()}
    for _ in range(30):
        new = {}
        for t, r in rec.items():
            opp_sum = sum(rating.get(o, 0.0) for o in r["opps"])
            new[t] = (r["mov"] / r["g"]) + (opp_sum / r["g"])
        mean = sum(new.values()) / len(new)
        rating = {t: v - mean for t, v in new.items()}   # re-center each pass
    out = {}
    for t, r in rec.items():
        g = r["g"] or 1
        avg_margin = r["mov"] / g
        out[t] = {"name": r["name"], "g": r["g"],
                  "w": sum(1 for o_i, o in enumerate(r["opps"]) if False),  # filled below
                  "pf": round(r["pf"] / g, 1), "pa": round(r["pa"] / g, 1),
                  "marg": round((r["pf"] - r["pa"]) / g, 1),
                  "srs": round(rating[t], 2),
                  "sos": round(rating[t] - avg_margin, 2)}
    # wins/losses
    wl = {}
    for hid, hnm, hp, aid, anm, ap in games:
        wl.setdefault(hid, [0, 0]); wl.setdefault(aid, [0, 0])
        if hp > ap:
            wl[hid][0] += 1; wl[aid][1] += 1
        elif ap > hp:
            wl[aid][0] += 1; wl[hid][1] += 1
    for t in out:
        out[t]["w"], out[t]["l"] = wl.get(t, [0, 0])
    return out


_RATINGS_COLS = [("w", "W"), ("l", "L"), ("pf", "PF/g"), ("pa", "PA/g"),
                 ("marg", "Marg"), ("srs", "SRS"), ("sos", "SOS")]
_RATINGS_INVERT = {"pa"}   # fewer points allowed is better (green)


def college_ratings(sport):
    """Opponent-adjusted team ratings (SRS) for a college sport. One ranked
    table, sorted by SRS. Cached."""
    import time as _t
    c = _ratings_cache.get(sport)
    if c and _t.time() - c[0] < _RATINGS_TTL:
        return _color_ratings(c[1])
    games = _college_games(sport)
    srs = _srs(games)
    rows = []
    for tid, r in srs.items():
        rows.append({"team": r["name"] or tid, "team_id": tid, "gms": r["g"],
                     "vals": {"w": r["w"], "l": r["l"], "pf": r["pf"], "pa": r["pa"],
                              "marg": r["marg"], "srs": r["srs"], "sos": r["sos"]}})
    rows.sort(key=lambda x: -x["vals"]["srs"])
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    data = {"sport": sport, "kind": "ratings",
            "updated": dt.datetime.utcnow().isoformat() + "Z",
            "games_counted": len(games), "teams_counted": len(rows),
            "columns": _RATINGS_COLS, "rows": rows}
    _ratings_cache[sport] = (_t.time(), data)
    return _color_ratings(data)


def _color_ratings(data):
    out = dict(data)
    rows = data["rows"]
    cols = [k for k, _ in _RATINGS_COLS]
    ranks = {k: _rank_map([r["vals"].get(k, 0) for r in rows]) for k in cols}
    nr = []
    for i, r in enumerate(rows):
        rr = dict(r)
        rr["heat"] = {}
        for k in cols:
            if k in ("w", "l"):
                continue
            h = ranks[k][i]
            rr["heat"][k] = (1 - h) if k in _RATINGS_INVERT else h
        nr.append(rr)
    out["rows"] = nr
    return out
