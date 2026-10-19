"""
nhl_edge.py — Player Edge scoring for the NHL, from nhl_profiles.json.

Skaters: Logic Score = Baseline (shot volume, xG/60, points, Corsi, finishing talent,
TOI percentiles) blended with Opportunity (opposing defense shots/goals allowed +
opposing starter weakness). Projections: SOG, points, goals, assists.
Goalies: Baseline (save%, GAA, volume) + Opportunity (opposing offense = save volume).
Projections: saves, goals against.

Same card shape as venom.py / nfl_edge.py so the one Player Edge UI renders it.
"""
import json
import os
import math
import unicodedata
import datetime as dt

_CACHE = {"data": None, "mtime": 0.0, "idx": None, "lg": None, "dlg": None, "glg": None, "ranks": None}

# ESPN/app team abbrev -> NHL API abbrev
_TEAM = {"TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "VGK": "VGK", "VGS": "VGK",
         "WSH": "WSH", "WAS": "WSH", "MTL": "MTL", "MON": "MTL", "CBJ": "CBJ", "WPG": "WPG",
         "NAS": "NSH", "NSH": "NSH", "CLS": "CBJ", "ANA": "ANA", "ARI": "ARI", "UTA": "UTA"}


def _tnorm(t):
    t = (t or "").upper()
    return _TEAM.get(t, t)


def _mug(season, team, pid):
    """NHL headshot CDN url, keyed by season/team tricode/playerId.
    Falls back to initials client-side if it 404s (onerror)."""
    if not (season and team and pid):
        return None
    return f"https://assets.nhle.com/mugs/nhl/{season}/{str(team).upper()}/{pid}.png"


def _norm(s):
    if not s:
        return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s))
                if not unicodedata.combining(c)).lower()
    return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip()


def _path():
    for p in ("/data/nhl_profiles.json", "nhl_profiles.json"):
        if os.path.exists(p):
            return p
    return None


_SK_BASE = [("shots_pg", 0.22), ("xg_per60", 0.18), ("points_pg", 0.20),
            ("corsi_pct", 0.12), ("icf_per60", 0.12), ("shot_talent", 0.08), ("toi_pg", 0.08)]
_SK_RADAR = {"Shots": "shots_pg", "xG/60": "xg_per60", "Points": "points_pg",
             "Corsi": "corsi_pct", "iCF/60": "icf_per60", "Finish": "shot_talent"}
_SK_GRID = [("xg_per60", "xG/60", "num2"), ("shots_pg", "Shots/g", "num2"),
            ("shooting_pct", "Shooting %", "num1"), ("corsi_pct", "Corsi %", "num1"),
            ("points_pg", "Points/g", "num2"), ("toi_pg", "TOI (min)", "num1"),
            ("pp_points_pg", "PP Pts/g", "num2"), ("shot_talent", "Finishing", "num2")]
_GO_GRID = [("save_pct", "Save %", "num2"), ("gaa", "GAA", "num2"),
            ("saves_pg", "Saves/g", "num1"), ("sa_pg", "Shots Ag/g", "num1")]


def load():
    p = _path()
    if not p:
        return None
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    if _CACHE["data"] is not None and _CACHE["mtime"] == mt:
        return _CACHE["data"]
    with open(p) as f:
        data = json.load(f)
    idx = {}
    for pid, pl in data.get("players", {}).items():
        pl["id"] = pid
        idx.setdefault(_norm(pl.get("name")), pl)
    _CACHE.update(data=data, mtime=mt, idx=idx,
                  lg=_league(data.get("players", {})),
                  dlg=_def_league(data.get("defense", {})),
                  glg=_goalie_league(data.get("goalies", {})),
                  ranks=_nhl_ranks(data.get("defense", {}), data.get("goalies", {})))
    return data


# ---- matchup leaks: rank teams/goalies league-wide on what they give up ----
def _nhl_ranks(defense, goalies):
    def rank_desc(d, key):
        pairs = [(t, v.get(key)) for t, v in d.items() if v.get(key) is not None]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return {t: i + 1 for i, (t, _v) in enumerate(pairs)}
    gs = [(gid, g.get("save_pct")) for gid, g in goalies.items()
          if (g.get("gp") or 0) >= 5 and g.get("save_pct") is not None]
    gs.sort(key=lambda x: x[1])   # ascending: lowest (worst) save% ranks 1
    return {"shots_against": rank_desc(defense, "shots_against_pg"),
            "goals_against": rank_desc(defense, "goals_against_pg"),
            "shots_for": rank_desc(defense, "shots_for_pg"),
            "goalie_save": {gid: i + 1 for i, (gid, _v) in enumerate(gs)}}


def _ord_most(r):
    return "most" if r == 1 else ("2nd-most" if r == 2 else
           ("3rd-most" if r == 3 else str(r) + "th-most"))


def _ord_worst(r):
    return "worst" if r == 1 else ("2nd-worst" if r == 2 else
           ("3rd-worst" if r == 3 else str(r) + "th-worst"))


def _skater_leaks(opp, defense, goalies):
    R = _CACHE.get("ranks") or {}
    dd = defense.get(opp) or {}
    out = []
    r = R.get("shots_against", {}).get(opp); v = dd.get("shots_against_pg")
    if r and v is not None and r <= 10:
        out.append({"text": "%s allows the %s shots on goal per game (%s)." % (opp, _ord_most(r), round(v, 1)),
                    "rank": r, "strong": r <= 5})
    r = R.get("goals_against", {}).get(opp); v = dd.get("goals_against_pg")
    if r and v is not None and r <= 10:
        out.append({"text": "%s allows the %s goals per game (%s)." % (opp, _ord_most(r), round(v, 2)),
                    "rank": r, "strong": r <= 5})
    gid = dd.get("starter_id")
    if gid is not None:
        g = goalies.get(str(gid)) or {}
        gr = R.get("goalie_save", {}).get(str(gid)); sv = g.get("save_pct")
        if gr and sv is not None and gr <= 10:
            out.append({"text": "%s starter %s ranks %s in save%% (%s)." % (
                        opp, g.get("name") or "the starter", _ord_worst(gr), ("%.3f" % (sv / 100.0))),
                        "rank": gr, "strong": gr <= 5})
    out.sort(key=lambda x: x["rank"])
    return out[:3]


def _goalie_leaks(opp, defense):
    R = _CACHE.get("ranks") or {}
    dd = defense.get(opp) or {}
    out = []
    r = R.get("shots_for", {}).get(opp); v = dd.get("shots_for_pg")
    if r and v is not None and r <= 12:
        out.append({"text": "%s generates the %s shots per game (%s) — save volume on offer." % (
                    opp, _ord_most(r), round(v, 1)), "rank": r, "strong": r <= 6})
    return out[:2]


def _stats(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if len(vals) < 5:
        return (0.0, 1.0)
    m = sum(vals) / len(vals)
    return (m, math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0)


def _league(players):
    ps = [p for p in players.values() if (p.get("gp") or 0) >= 10]
    keys = set(k for k, _ in _SK_BASE) | set(_SK_RADAR.values()) | set(k for k, _, _ in _SK_GRID)
    return {k: _stats([p.get(k) for p in ps]) for k in keys}


def _def_league(defense):
    return {k: _stats([d.get(k) for d in defense.values()])
            for k in ("shots_against_pg", "goals_against_pg", "shots_for_pg", "goals_for_pg", "team_save_pct")}


def _goalie_league(goalies):
    gs = [g for g in goalies.values() if (g.get("gp") or 0) >= 5]
    return {k: _stats([g.get(k) for g in gs]) for k in ("save_pct", "gaa", "saves_pg", "sa_pg")}


def _z(val, ms):
    if val is None:
        return 0.0
    m, s = ms
    return (val - m) / (s or 1.0)


def _pctile(z):
    return round(100 * 0.5 * (1 + math.erf(z / math.sqrt(2))), 1)


def _grade(z):
    return "batter" if z >= 0.45 else ("pitcher" if z <= -0.45 else "neutral")


def find(name):
    load()
    return (_CACHE["idx"] or {}).get(_norm(name))


# ---- injury backup boost: a ruled-out skater's volume flows to a linemate ----
_NHL_INHERIT_F = {"shots_pg": 0.30, "points_pg": 0.30, "goals_pg": 0.30,
                  "assists_pg": 0.30, "toi_pg": 0.15, "pp_points_pg": 0.35}
_NHL_INHERIT_D = {"shots_pg": 0.25, "points_pg": 0.25, "toi_pg": 0.20,
                  "pp_points_pg": 0.30}
_NHL_FWD = {"C", "LW", "RW", "F", "W", "LEFT WING", "RIGHT WING", "CENTER"}


def _nhl_is_out(status):
    s = (status or "").lower()
    return any(k in s for k in ("out", "injured reserve", "season", " ir", "ltir"))


def apply_injuries(roster, team):
    """Drop OUT skaters from a team's prop list and boost the top remaining
    skater (same side: forward/defense) by the vacated volume. Tagged boosted_by.
    No-op when the injury feed is unavailable."""
    try:
        import injuries
        if not injuries.enabled("nhl"):
            return roster
    except Exception:
        return roster
    info = injuries.for_team("nhl", abbr=str(team).upper(), name=team)
    outs = [o for o in info.get("players", []) if _nhl_is_out(o.get("status"))]
    if not outs:
        return roster
    out_norm = {_norm(o.get("name")) for o in outs}
    pool = [p for p in roster if _norm(p.get("name")) not in out_norm]

    def _is_fwd(p):
        return (p.get("pos") or "").upper() in _NHL_FWD
    for o in outs:
        fwd = (o.get("pos") or "").upper() in _NHL_FWD
        inh = _NHL_INHERIT_F if fwd else _NHL_INHERIT_D
        same = [p for p in pool if _is_fwd(p) == fwd]
        if not same:
            continue
        starter = next((p for p in roster
                        if _norm(p.get("name")) == _norm(o.get("name"))), None)
        backup = max(same, key=lambda p: (p.get("toi_pg") or p.get("shots_pg") or 0))
        bk = dict(backup)
        for st, frac in inh.items():
            vac = (starter.get(st) if starter else 0) or 0
            if vac:
                bk[st] = round((bk.get(st) or 0) + vac * frac, 3)
        bk["boosted_by"] = o.get("name")
        pool = [bk if p is backup else p for p in pool]
    return pool


def score_skater(p, opp_def, opp_goalie):
    load()
    lg = _CACHE["lg"]
    comps, acc, w = {}, 0.0, 0.0
    for k, wt in _SK_BASE:
        pc = _pctile(_z(p.get(k), lg.get(k, (0, 1))))
        comps[k] = pc
        acc += wt * pc
        w += wt
    baseline = round(acc / (w or 1), 1)
    opp, edge = 50.0, 0.0
    if opp_def:
        dl = _CACHE["dlg"]
        z = (0.5 * _z(opp_def.get("shots_against_pg"), dl["shots_against_pg"])
             + 0.5 * _z(opp_def.get("goals_against_pg"), dl["goals_against_pg"]))
        if opp_goalie and opp_goalie.get("save_pct") is not None:
            z += -0.4 * _z(opp_goalie.get("save_pct"), _CACHE["glg"]["save_pct"])  # weak goalie -> more
        opp = _pctile(z)
        edge = round((opp - 50) / 5.0, 1)
    logic = round(0.62 * baseline + 0.38 * opp, 1)
    mf = 1.0 + max(-0.18, min(0.18, edge / 100.0))
    proj = {}
    if p.get("shots_pg") is not None:
        proj["shots_on_goal"] = round(p["shots_pg"] * mf, 2)
    if p.get("points_pg") is not None:
        proj["points"] = round(p["points_pg"] * mf, 2)
    if p.get("goals_pg") is not None:
        proj["goals"] = round(p["goals_pg"] * mf, 2)
    if p.get("assists_pg") is not None:
        proj["assists"] = round(p["assists_pg"] * mf, 2)
    radar = {}
    for lab, src in _SK_RADAR.items():
        radar[lab] = _pctile(_z(p.get(src), lg.get(src, (0, 1))))
    radar["Matchup"] = round(opp, 1)
    radar["Logic"] = logic
    grid = []
    for key, label, kind in _SK_GRID:
        v = p.get(key)
        if v is None:
            continue
        grid.append({"label": label, "value": v, "kind": kind, "grade": _grade(_z(v, lg.get(key, (0, 1))))})
    sig, desc = _nhl_blueprint(p, opp_def, opp_goalie)
    card = {"player": p.get("name"), "id": p.get("id"), "type": "nhl", "pos": p.get("pos"),
            "team": p.get("team"), "logic_score": logic, "baseline": baseline,
            "opportunity": round(opp, 1), "edge_pct": edge, "games": p.get("gp"),
            "grid": grid, "radar": radar, "projections": proj,
            "signals": sig, "descriptor": desc}
    bp = _nhl_goal_blueprint(p)
    if bp:
        card["blueprint"] = bp
    return card


def _nhl_goal_blueprint(p):
    """Goal Debt = expected goals (from xG) vs actual, the NHL regression read —
    the same shape as NFL's Scoring Debt so the one Blueprint section renders it.
    xG here is shot-quality expected goals; it won't perfectly include every PP
    look, so it's a directional 'luck' read, not a projection."""
    xg60, toi, gpg, gp = p.get("xg_per60"), p.get("toi_pg"), p.get("goals_pg"), p.get("gp")
    if xg60 is None or toi is None or gpg is None or not gp:
        return None
    xg_pg = xg60 * (float(toi) / 60.0)
    xg_tot = xg_pg * gp
    scored = int(round(gpg * gp))
    debt = round(xg_tot - scored, 1)
    if debt >= 0.8:
        reg = {"dir": "up", "label": "Due to score",
               "note": "Generating more xG than he's finished — positive regression."}
    elif debt <= -0.8:
        reg = {"dir": "down", "label": "Regression risk",
               "note": "Finishing above his shot quality — likely to cool off."}
    else:
        reg = {"dir": "flat", "label": "On model", "note": "Goals match his shot quality."}
    return {"scoring": {"xtd": round(xg_tot, 1), "scored": scored, "debt": debt,
                        "regress": reg, "zones": [],
                        "labels": {"xtd": "Expected Goals", "scored": "Scored",
                                   "debt": "Goal Debt",
                                   "sub": "Goals priced by shot quality (xG)"}},
            "box": None, "signals": []}


# ---- NHL Blueprint: the "cheat code" signal chips + a two-phrase descriptor ----
# Thresholds are the Venom-style tells, tuned to league-ish NHL values.
_NHL_SHOT_VOL = 3.0        # shots/game -> Volume Shooter
_NHL_SOFT_SAVE = 89.0      # opp goalie save% at/under -> Soft Net
_NHL_LEAKY_SHOTS = 31.0    # opp shots-against/game at/over -> Leaky Defense


def _nhl_blueprint(p, opp_def, opp_goalie):
    """Returns (signals, descriptor). signals = [{label,on,note}]; the chips fire
    when a threshold is crossed, same idea as the Blueprint signals on NFL."""
    sig = []
    shots = p.get("shots_pg")
    if shots is not None and shots >= _NHL_SHOT_VOL:
        sig.append({"key": "vol", "label": "Volume Shooter", "on": True,
                    "note": "%.1f shots/g" % shots})
    sv = (opp_goalie or {}).get("save_pct")
    if sv is None and opp_def:
        sv = opp_def.get("team_save_pct")
    if sv is not None and sv <= _NHL_SOFT_SAVE:
        sig.append({"key": "net", "label": "Soft Net", "on": True,
                    "note": "opp stops %.1f%%" % sv})
    sa = (opp_def or {}).get("shots_against_pg")
    if sa is not None and sa >= _NHL_LEAKY_SHOTS:
        sig.append({"key": "leak", "label": "Leaky Defense", "on": True,
                    "note": "opp allows %.1f shots/g" % sa})
    if (p.get("pp_toi_pg") or p.get("pp_points_pg") or 0):
        if (p.get("pp_points_pg") or 0) >= 0.3:
            sig.append({"key": "pp", "label": "Power-Play Role", "on": True})
    # descriptor: shooter trait + matchup trait
    trait = "Volume shooter" if (shots and shots >= _NHL_SHOT_VOL) else (
        "Sniper" if (p.get("shooting_pct") or 0) >= 14 else "Playmaker"
        if (p.get("assists_pg") or 0) >= (p.get("goals_pg") or 0) else "Shooter")
    mtrait = None
    if sv is not None and sv <= _NHL_SOFT_SAVE:
        mtrait = "Soft net"
    elif sa is not None and sa >= _NHL_LEAKY_SHOTS:
        mtrait = "Leaky defense"
    desc = " + ".join([x for x in (trait, mtrait) if x]) or None
    return sig, desc


def score_goalie(g, opp_off):
    load()
    gl = _CACHE["glg"]
    base = round(0.4 * _pctile(_z(g.get("save_pct"), gl["save_pct"]))
                 + 0.3 * _pctile(-_z(g.get("gaa"), gl["gaa"]))
                 + 0.3 * _pctile(_z(g.get("saves_pg"), gl["saves_pg"])), 1)
    opp, edge = 50.0, 0.0
    if opp_off:
        dl = _CACHE["dlg"]
        z = 0.6 * _z(opp_off.get("shots_for_pg"), dl["shots_for_pg"]) + 0.4 * _z(opp_off.get("goals_for_pg"), dl["goals_for_pg"])
        opp = _pctile(z)          # more shots faced -> more saves on offer
        edge = round((opp - 50) / 5.0, 1)
    logic = round(0.62 * base + 0.38 * opp, 1)
    mf = 1.0 + max(-0.2, min(0.2, edge / 100.0))
    proj = {}
    if g.get("saves_pg") is not None:
        proj["saves"] = round(g["saves_pg"] * mf, 2)
    if g.get("sa_pg") is not None and g.get("save_pct") is not None:
        proj["goals_against"] = round(g["sa_pg"] * mf * (1 - g["save_pct"] / 100.0), 2)
    radar = {"Save %": _pctile(_z(g.get("save_pct"), gl["save_pct"])),
             "GAA": _pctile(-_z(g.get("gaa"), gl["gaa"])),
             "Saves/g": _pctile(_z(g.get("saves_pg"), gl["saves_pg"])),
             "Volume": _pctile(_z(g.get("sa_pg"), gl["sa_pg"])),
             "Matchup": round(opp, 1), "Logic": logic}
    grid = []
    for key, label, kind in _GO_GRID:
        v = g.get(key)
        if v is None:
            continue
        inv = key == "gaa"
        grid.append({"label": label, "value": v, "kind": kind,
                     "grade": _grade((-1 if inv else 1) * _z(v, gl.get(key, (0, 1))))})
    return {"player": g.get("name"), "id": g.get("id"), "type": "nhl", "pos": "G",
            "team": g.get("team"), "logic_score": logic, "baseline": base,
            "opportunity": round(opp, 1), "edge_pct": edge, "games": g.get("gp"),
            "grid": grid, "radar": radar, "projections": proj}


def _ord_most(r):
    return "most" if r == 1 else ("2nd-most" if r == 2 else
           ("3rd-most" if r == 3 else str(r) + "th-most"))


def _ord_worst(r):
    return "the worst" if r == 1 else ("2nd-worst" if r == 2 else
           ("3rd-worst" if r == 3 else str(r) + "th-worst"))


def _nhl_ranks(defense, goalies):
    """League-wide ranks: 1 = allows/generates the MOST (leaky)."""
    R = {}
    for key in ("shots_against_pg", "goals_against_pg", "shots_for_pg", "goals_for_pg"):
        pairs = [(t, d.get(key)) for t, d in defense.items() if d.get(key) is not None]
        pairs.sort(key=lambda x: x[1], reverse=True)
        R[key] = {t: i + 1 for i, (t, _v) in enumerate(pairs)}
    gp = [(gid, g.get("save_pct")) for gid, g in goalies.items()
          if (g.get("gp") or 0) >= 5 and g.get("save_pct") is not None]
    gp.sort(key=lambda x: x[1])   # ascending: weakest save% first
    R["_goalie_weak"] = {gid: i + 1 for i, (gid, _v) in enumerate(gp)}
    return R


def _nhl_tax_grade(r):
    if r is None:
        return "neutral"
    if r <= 10:
        return "batter"     # soft / favorable
    if r >= 23:
        return "pitcher"    # tough
    return "neutral"


def _nhl_tax(p, opp, defense, goalies):
    """HIM vs what the opponent gives up, with league rank — the NHL Matchup Tax.
    Shots & goals are team-level (opponent defense); save% is the projected starter."""
    R = _CACHE.get("ranks") or {}
    dd = defense.get(opp) or {}
    rows = []
    # shots on goal
    v = dd.get("shots_against_pg")
    if v is not None:
        r = R.get("shots_against", {}).get(opp)
        rows.append({"label": "Shots/g", "him": _r(p.get("shots_pg"), 1),
                     "allows": round(v, 1), "rank": r, "grade": _nhl_tax_grade(r)})
    # goals
    v = dd.get("goals_against_pg")
    if v is not None:
        r = R.get("goals_against", {}).get(opp)
        rows.append({"label": "Goals/g", "him": _r(p.get("goals_pg"), 2),
                     "allows": round(v, 2), "rank": r, "grade": _nhl_tax_grade(r)})
    # opposing starter save %
    gid = dd.get("starter_id")
    g = goalies.get(str(gid)) if gid is not None else None
    if g and g.get("save_pct") is not None:
        r = R.get("goalie_save", {}).get(str(gid))
        rows.append({"label": "Opp SV%", "him": _r(p.get("shooting_pct"), 1),
                     "allows": round(g["save_pct"], 1), "rank": r, "grade": _nhl_tax_grade(r)})
    if not rows:
        return None
    soft = [x["label"] for x in rows if x["rank"] and x["rank"] <= 8]
    headline = ("%s is top-8 soft in %s." % (opp, " & ".join(soft[:2]))) if soft else \
        ("%s is a middle-of-the-pack matchup." % opp)
    return {"opp": opp, "headline": headline, "rows": rows}


def _r(v, nd):
    return round(v, nd) if isinstance(v, (int, float)) else None


def _skater_leaks(opp, defense, goalies, ranks):
    dd = defense.get(opp) or {}
    out = []
    r = ranks.get("shots_against_pg", {}).get(opp); v = dd.get("shots_against_pg")
    if r and v is not None and r <= 10:
        out.append({"text": "%s allows the %s shots on goal per game this season (%s/g)."
                    % (opp, _ord_most(r), round(v, 1)), "rank": r, "strong": r <= 5})
    r = ranks.get("goals_against_pg", {}).get(opp); v = dd.get("goals_against_pg")
    if r and v is not None and r <= 10:
        out.append({"text": "%s allows the %s goals per game (%s/g)."
                    % (opp, _ord_most(r), round(v, 2)), "rank": r, "strong": r <= 5})
    gid = str(dd.get("starter_id") or "")
    gw = ranks.get("_goalie_weak", {}).get(gid); g = goalies.get(gid)
    if gw and g and gw <= 10 and g.get("save_pct") is not None:
        out.append({"text": "%s's starter ranks %s in save %% (%.1f%%)."
                    % (opp, _ord_worst(gw), g.get("save_pct")), "rank": gw, "strong": gw <= 5})
    out.sort(key=lambda x: x["rank"])
    return out[:3]


def _goalie_leaks(opp, defense, ranks):
    dd = defense.get(opp) or {}
    out = []
    r = ranks.get("shots_for_pg", {}).get(opp); v = dd.get("shots_for_pg")
    if r and v is not None and r <= 12:
        out.append({"text": "%s fires the %s shots per game (%s/g) — save volume on offer."
                    % (opp, _ord_most(r), round(v, 1)), "rank": r, "strong": r <= 6})
    return out[:2]


def register(app):
    from fastapi.responses import JSONResponse

    @app.get("/api/nhl/venom/{game_id}")
    def nhl_venom(game_id: str, date: str | None = None,
                  home: str | None = None, away: str | None = None):
        data = load()
        if data is None:
            return JSONResponse({"error": "nhl profiles not built yet",
                                 "build": "/api/nhl/profiles/build?confirm=yes"})
        if not (home and away):
            try:
                import espn_provider as ep
                d = dt.date.fromisoformat(date) if date else None
                g = ep.get_game("nhl", game_id, d) if hasattr(ep, "get_game") else None
                if g:
                    home = home or (g.get("home") or {}).get("abbr")
                    away = away or (g.get("away") or {}).get("abbr")
            except Exception:
                pass
        if not home or not away:
            return JSONResponse({"error": "pass ?home=ABBR&away=ABBR"})
        H, A = _tnorm(home), _tnorm(away)
        season = data.get("season")
        players, goalies, defense = data.get("players", {}), data.get("goalies", {}), data.get("defense", {})
        ranks = _nhl_ranks(defense, goalies)
        # trade remap prep: resolve each game team's id + a name index of all
        # profiled skaters, so a player now on a team's CURRENT roster but tagged
        # elsewhere in the profile (a trade) shows under his new team. Best-effort.
        _tid_by, _idx = {}, {}
        try:
            import espn_provider as _ep
            _d = dt.date.fromisoformat(date) if date else None
            _g = _ep.get_game("nhl", _d, game_id) if _d else None
            if _g:
                for _s, _tn in (("home", H), ("away", A)):
                    _ti = (_g.get(_s) or {}).get("team_id")
                    if _ti:
                        _tid_by[_tn] = _ti
                _idx = {_norm(pl.get("name")): pl for pl in players.values()}
        except Exception:
            pass
        cards = []
        for team, opp in ((H, A), (A, H)):
            opp_def = defense.get(opp) or {}
            opp_goalie = goalies.get(str(opp_def.get("starter_id"))) if opp_def.get("starter_id") else None
            roster = [pl for pl in players.values()
                      if _tnorm(pl.get("team")) == team
                      and (pl.get("gp") or 0) >= 3 and (pl.get("shots_pg") or 0) >= 1.0]
            try:
                _tid = _tid_by.get(team)
                if _tid and _idx:
                    import espn_provider as _ep
                    _have = {_norm(p.get("name")) for p in roster}
                    for _nm in _ep._team_roster_ids("nhl", _tid):
                        _k = _norm(_nm)
                        _pl = _idx.get(_k)
                        if _pl and _k not in _have and _tnorm(_pl.get("team")) != team:
                            _mv = dict(_pl)
                            _mv["team"] = home if team == H else away
                            _mv["traded_from"] = _pl.get("team")
                            roster.append(_mv)
                            _have.add(_k)
            except Exception:
                pass
            try:
                roster = apply_injuries(roster, team)   # drop OUT, boost the linemate
            except Exception:
                pass
            for pl in roster:
                c = score_skater(pl, opp_def, opp_goalie)
                c["opp"] = opp
                c["headshot"] = _mug(season, pl.get("team"), pl.get("id"))
                c["leaks"] = _skater_leaks(opp, defense, goalies, ranks)
                c["tax"] = _nhl_tax(pl, opp, defense, goalies)
                if pl.get("boosted_by"):
                    c["boosted_by"] = pl["boosted_by"]
                if pl.get("traded_from"):
                    c["traded_from"] = pl["traded_from"]
                cards.append(c)
            # team's starting goalie vs opponent offense
            my_def = defense.get(team) or {}
            gid2 = my_def.get("starter_id")
            if gid2 and str(gid2) in goalies:
                g = goalies[str(gid2)]
                gc = score_goalie(g, defense.get(opp) or {})
                gc["opp"] = opp
                gc["id"] = str(gid2)
                gc["headshot"] = _mug(season, g.get("team"), gid2)
                gc["leaks"] = _goalie_leaks(opp, defense, ranks)
                cards.append(gc)
        cards.sort(key=lambda c: c.get("logic_score") or 0, reverse=True)
        for i, c in enumerate(cards, 1):
            c["rank"] = i
        return {"game_id": game_id, "home_team": home, "away_team": away,
                "count": len(cards), "cards": cards}

    print("[nhl] edge endpoint registered")
