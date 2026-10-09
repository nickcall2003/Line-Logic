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

_CACHE = {"data": None, "mtime": 0.0, "idx": None, "lg": None, "dlg": None, "glg": None}

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
                  glg=_goalie_league(data.get("goalies", {})))
    return data


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
    return {"player": p.get("name"), "id": p.get("id"), "type": "nhl", "pos": p.get("pos"),
            "team": p.get("team"), "logic_score": logic, "baseline": baseline,
            "opportunity": round(opp, 1), "edge_pct": edge, "games": p.get("gp"),
            "grid": grid, "radar": radar, "projections": proj}


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
        cards = []
        for team, opp in ((H, A), (A, H)):
            opp_def = defense.get(opp) or {}
            opp_goalie = goalies.get(str(opp_def.get("starter_id"))) if opp_def.get("starter_id") else None
            for pl in players.values():
                if _tnorm(pl.get("team")) != team:
                    continue
                if (pl.get("gp") or 0) >= 3 and (pl.get("shots_pg") or 0) >= 1.0:
                    c = score_skater(pl, opp_def, opp_goalie)
                    c["opp"] = opp
                    c["headshot"] = _mug(season, pl.get("team"), pl.get("id"))
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
                cards.append(gc)
        cards.sort(key=lambda c: c.get("logic_score") or 0, reverse=True)
        for i, c in enumerate(cards, 1):
            c["rank"] = i
        return {"game_id": game_id, "home_team": home, "away_team": away,
                "count": len(cards), "cards": cards}

    print("[nhl] edge endpoint registered")
