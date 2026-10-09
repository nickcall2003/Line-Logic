"""
nfl_edge.py — Player Edge scoring for the NFL, from nfl_profiles.json.

Per player vs the opposing defense:
  - a Logic Score (0-100) = Baseline (player quality, position-percentile of usage +
    efficiency) blended with Opportunity (how much the opposing defense gives up to
    that position),
  - a radar of position-appropriate drivers,
  - a graded advanced-metric grid,
  - per-stat projections (rec yds / receptions / rush yds / pass yds / TDs …).

Mirrors venom.py's shape so the same Player Edge UI renders both sports.
"""
import json
import os
import math
import unicodedata
import datetime as dt

_CACHE = {"data": None, "mtime": 0.0, "idx": None, "lg": None, "dlg": None, "dranks": None}

# team abbreviation aliases (ESPN/app <-> nflverse)
_TEAM = {"LAR": "LA", "STL": "LA", "SD": "LAC", "OAK": "LV", "JAC": "JAX",
         "WSH": "WAS", "ARZ": "ARI", "CLV": "CLE", "HST": "HOU", "SL": "LA",
         "LA": "LA", "LAC": "LAC", "JAX": "JAX", "WAS": "WAS"}


def _tnorm(t):
    t = (t or "").upper()
    return _TEAM.get(t, t)


def _norm(s):
    if not s:
        return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s))
                if not unicodedata.combining(c)).lower()
    return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip()


def _path():
    for p in ("/data/nfl_profiles.json", "nfl_profiles.json"):
        if os.path.exists(p):
            return p
    return None


# ---- position configs: baseline metrics, radar, grid, projections ----
# metric: (key, label, kind)  kind in avg/pct/num/int for display
_POS = {
    "WR": {
        "base": [("target_share", 0.22), ("air_yards_share", 0.14), ("wopr", 0.22),
                 ("rec_yards_pg", 0.22), ("ypt", 0.12), ("rec_epa_pg", 0.08)],
        "radar": ["Tgt Share", "Air Yds", "WOPR", "Rec Yds", "aDOT", "EPA"],
        "radar_src": {"Tgt Share": "target_share", "Air Yds": "air_yards_share", "WOPR": "wopr",
                      "Rec Yds": "rec_yards_pg", "aDOT": "adot", "EPA": "rec_epa_pg"},
        "grid": [("target_share", "Tgt Share", "pctp"), ("air_yards_share", "Air Yd Sh", "pctp"),
                 ("wopr", "WOPR", "num3"), ("adot", "aDOT", "num1"),
                 ("catch_rate", "Catch %", "pctp"), ("ypt", "Yds/Tgt", "num1"),
                 ("yac_pg", "YAC/g", "num1"), ("rec_epa_pg", "Rec EPA/g", "num2")],
        "def": "WR", "def_stat": "rec_yards_pg",
        "proj": {"rec_yards": "rec_yards_pg", "receptions": "rec_pg", "rec_tds": "rec_tds_pg"},
    },
    "TE": {
        "base": [("target_share", 0.24), ("wopr", 0.22), ("rec_yards_pg", 0.24),
                 ("ypt", 0.14), ("rec_epa_pg", 0.08), ("catch_rate", 0.08)],
        "radar": ["Tgt Share", "WOPR", "Rec Yds", "aDOT", "Catch %", "EPA"],
        "radar_src": {"Tgt Share": "target_share", "WOPR": "wopr", "Rec Yds": "rec_yards_pg",
                      "aDOT": "adot", "Catch %": "catch_rate", "EPA": "rec_epa_pg"},
        "grid": [("target_share", "Tgt Share", "pctp"), ("wopr", "WOPR", "num3"),
                 ("adot", "aDOT", "num1"), ("catch_rate", "Catch %", "pctp"),
                 ("ypt", "Yds/Tgt", "num1"), ("yac_pg", "YAC/g", "num1"),
                 ("rec_epa_pg", "Rec EPA/g", "num2")],
        "def": "TE", "def_stat": "rec_yards_pg",
        "proj": {"rec_yards": "rec_yards_pg", "receptions": "rec_pg", "rec_tds": "rec_tds_pg"},
    },
    "RB": {
        "base": [("rush_yards_pg", 0.26), ("carries_pg", 0.20), ("ypc", 0.14),
                 ("rush_epa_pg", 0.10), ("targets_pg", 0.16), ("ppr_pg", 0.14)],
        "radar": ["Carries", "Rush Yds", "YPC", "Rush EPA", "Targets", "PPR"],
        "radar_src": {"Carries": "carries_pg", "Rush Yds": "rush_yards_pg", "YPC": "ypc",
                      "Rush EPA": "rush_epa_pg", "Targets": "targets_pg", "PPR": "ppr_pg"},
        "grid": [("carries_pg", "Carries/g", "num1"), ("rush_yards_pg", "Rush Yds/g", "num1"),
                 ("ypc", "Yds/Carry", "num2"), ("rush_epa_pg", "Rush EPA/g", "num2"),
                 ("targets_pg", "Targets/g", "num1"), ("rec_yards_pg", "Rec Yds/g", "num1"),
                 ("ppr_pg", "PPR/g", "num1")],
        "def": "RB", "def_stat": "rush_yards_pg",
        "proj": {"rush_yards": "rush_yards_pg", "rush_tds": "rush_tds_pg",
                 "receptions": "rec_pg", "rec_yards": "rec_yards_pg"},
    },
    "QB": {
        "base": [("pass_yards_pg", 0.26), ("pass_tds_pg", 0.18), ("cpoe", 0.16),
                 ("pass_epa_pg", 0.16), ("comp_pct", 0.12), ("pacr", 0.12)],
        "radar": ["Pass Yds", "Pass TD", "CPOE", "EPA", "Comp %", "PACR"],
        "radar_src": {"Pass Yds": "pass_yards_pg", "Pass TD": "pass_tds_pg", "CPOE": "cpoe",
                      "EPA": "pass_epa_pg", "Comp %": "comp_pct", "PACR": "pacr"},
        "grid": [("pass_yards_pg", "Pass Yds/g", "num1"), ("pass_tds_pg", "Pass TD/g", "num2"),
                 ("cpoe", "CPOE", "num1"), ("pass_epa_pg", "Pass EPA/g", "num2"),
                 ("comp_pct", "Comp %", "pctp"), ("pacr", "PACR", "num2"),
                 ("att_pg", "Att/g", "num1")],
        "def": None, "def_stat": "pass_yards_pg",
        "proj": {"pass_yards": "pass_yards_pg", "pass_tds": "pass_tds_pg", "completions": "cmp_pg"},
    },
}
_POS["FB"] = _POS["RB"]


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
                  dranks=_def_ranks(data.get("defense", {})))
    return data


# ---- matchup leaks: rank each defense league-wide on what it allows by position ----
_LEAK_STATS = {
    "WR": [("rec_yards_pg", "receiving yards", 1), ("rec_tds_pg", "receiving TDs", 2),
           ("rec_pg", "receptions", 1)],
    "TE": [("rec_yards_pg", "receiving yards", 1), ("rec_tds_pg", "receiving TDs", 2),
           ("rec_pg", "receptions", 1)],
    "RB": [("rush_yards_pg", "rushing yards", 1), ("rush_tds_pg", "rushing TDs", 2),
           ("rec_yards_pg", "receiving yards", 1)],
    "QB": [("pass_yards_pg", "passing yards", 1), ("pass_tds_pg", "passing TDs", 2)],
}
_DEF_POS = {"WR": "WR", "TE": "TE", "RB": "RB", "FB": "RB", "QB": "QB"}


def _def_ranks(defense):
    """{pos: {stat: {team: rank}}}, rank 1 = allows the MOST per game league-wide."""
    R = {}
    for pos, cfg in _LEAK_STATS.items():
        R[pos] = {}
        for st, _lab, _nd in cfg:
            pairs = [(t, d[pos][st]) for t, d in defense.items()
                     if isinstance(d.get(pos), dict) and d[pos].get(st) is not None]
            pairs.sort(key=lambda x: x[1], reverse=True)
            R[pos][st] = {t: i + 1 for i, (t, _v) in enumerate(pairs)}
    return R


def _ordinal_most(r):
    return "most" if r == 1 else ("2nd-most" if r == 2 else
           ("3rd-most" if r == 3 else str(r) + "th-most"))


def _leaks(opp_abbr, pos, defense):
    """Favorable matchup bullets: where the opposing defense ranks worst (top-12 most
    generous) against this player's position."""
    dpos = _DEF_POS.get(pos)
    ranks = _CACHE.get("dranks") or {}
    cfg = _LEAK_STATS.get(dpos) or []
    dd = (defense.get(opp_abbr) or {}).get(dpos) or {}
    out = []
    for st, label, nd in cfg:
        r = ranks.get(dpos, {}).get(st, {}).get(opp_abbr)
        v = dd.get(st)
        if r is None or v is None or r > 10:
            continue
        pg = (round(v, 1) if nd == 1 else round(v, 2))
        out.append({"text": "%s allows the %s %s to %ss this season (%s/g)."
                    % (opp_abbr, _ordinal_most(r), label, pos, pg),
                    "rank": r, "strong": r <= 5})
    out.sort(key=lambda x: x["rank"])
    return out[:3]


def _stats(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if len(vals) < 5:
        return (0.0, 1.0)
    m = sum(vals) / len(vals)
    return (m, math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0)


def _league(players):
    """Per-position mean/std for every metric used."""
    lg = {}
    for pos, cfg in _POS.items():
        ps = [p for p in players.values() if p.get("pos") == pos and (p.get("games") or 0) >= 3]
        keys = set(k for k, _ in cfg["base"]) | set(cfg["radar_src"].values()) | set(k for k, _, _ in cfg["grid"])
        lg[pos] = {k: _stats([p.get(k) for p in ps]) for k in keys}
    return lg


def _def_league(defense):
    """League mean/std of yards allowed to each position (the matchup baseline)."""
    out = {}
    for pos in ("WR", "TE", "RB"):
        out[pos] = {
            "rec_yards_pg": _stats([d.get(pos, {}).get("rec_yards_pg") for d in defense.values()]),
            "rush_yards_pg": _stats([d.get(pos, {}).get("rush_yards_pg") for d in defense.values()]),
        }
    out["PASS"] = _stats([sum((d.get(p, {}).get("pass_yards_pg") or 0) for p in ("WR", "TE", "RB"))
                          for d in defense.values()])
    return out


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


def score_player(p, opp_def):
    """Full Logic card for one player vs the opposing defense dict."""
    load()
    pos = p.get("pos")
    cfg = _POS.get(pos)
    if not cfg:
        return None
    lg = _CACHE["lg"].get(pos, {})
    dlg = _CACHE["dlg"]
    # baseline
    comps, acc, wsum = {}, 0.0, 0.0
    for k, w in cfg["base"]:
        pc = _pctile(_z(p.get(k), lg.get(k, (0, 1))))
        comps[k] = pc
        acc += w * pc
        wsum += w
    baseline = round(acc / (wsum or 1), 1)
    # opportunity from opposing defense
    opp, edge = 50.0, 0.0
    if opp_def:
        if pos == "QB":
            allowed = sum((opp_def.get(x, {}).get("pass_yards_pg") or 0) for x in ("WR", "TE", "RB"))
            z = _z(allowed, dlg.get("PASS", (0, 1)))
        else:
            dstat = cfg["def_stat"]
            allowed = (opp_def.get(cfg["def"], {}) or {}).get(dstat)
            z = _z(allowed, dlg.get(cfg["def"], {}).get(dstat, (0, 1)))
        opp = _pctile(z)
        edge = round((opp - 50) / 5.0, 1)
    logic = round(0.62 * baseline + 0.38 * opp, 1)
    mf = 1.0 + max(-0.18, min(0.18, edge / 100.0))
    # projections
    proj = {}
    for out_key, src in cfg["proj"].items():
        v = p.get(src)
        if v is not None:
            proj[out_key] = round(v * mf, 2)
    # radar (position spokes) + Matchup + Logic
    radar = {}
    for lab in cfg["radar"]:
        src = cfg["radar_src"][lab]
        radar[lab] = _pctile(_z(p.get(src), lg.get(src, (0, 1))))
    radar["Matchup"] = round(opp, 1)
    radar["Logic"] = logic
    # grid
    grid = []
    for key, label, kind in cfg["grid"]:
        val = p.get(key)
        if val is None:
            continue
        z = _z(val, lg.get(key, (0, 1)))
        grid.append({"label": label, "value": val, "kind": _KIND.get(kind, "num"), "grade": _grade(z)})
    return {
        "player": p.get("name"), "id": p.get("id"), "type": "nfl", "pos": pos,
        "team": p.get("team"), "headshot": p.get("headshot"),
        "logic_score": logic, "baseline": baseline, "opportunity": round(opp, 1),
        "edge_pct": edge, "games": p.get("games"),
        "grid": grid, "radar": radar, "projections": proj,
    }


_KIND = {"pctp": "pct01", "num1": "num1", "num2": "num2", "num3": "num3", "int": "int", "avg": "avg"}


def game_cards(home, away, players_by_team, defense):
    """home/away = team abbrevs. players_by_team = {team:[player dicts]}.
    Each player scored vs the OTHER team's defense."""
    cards = []
    for team, opp in ((home, away), (away, home)):
        opp_def = defense.get(_tnorm(opp)) or defense.get(opp) or {}
        for pl in players_by_team.get(team, []):
            c = score_player(pl, opp_def)
            if not c:
                continue
            c["opp"] = opp
            c["leaks"] = _leaks(_tnorm(opp), pl.get("pos"), defense)
            cards.append(c)
    cards.sort(key=lambda c: c.get("logic_score") or 0, reverse=True)
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return cards


def register(app):
    from fastapi.responses import JSONResponse

    @app.get("/api/nfl/venom/{game_id}")
    def nfl_venom(game_id: str, date: str | None = None,
                  home: str | None = None, away: str | None = None):
        data = load()
        if data is None:
            return JSONResponse({"error": "nfl profiles not built yet",
                                 "build": "/api/nfl/profiles/build?confirm=yes"})
        if not (home and away):
            # fall back to resolving the game's teams from the NFL provider
            try:
                import espn_provider as ep
                d = dt.date.fromisoformat(date) if date else None
                g = ep.get_game("nfl", game_id, d) if hasattr(ep, "get_game") else None
                if g:
                    home = home or (g.get("home") or {}).get("abbr") or g.get("home_abbr")
                    away = away or (g.get("away") or {}).get("abbr") or g.get("away_abbr")
            except Exception:
                pass
        if not home or not away:
            return JSONResponse({"error": "pass ?home=ABBR&away=ABBR (teams not resolved)"})
        H, A = _tnorm(home), _tnorm(away)
        players = data.get("players", {})
        by_team = {H: [], A: []}
        MIN = {"WR": 1.5, "TE": 1.0, "RB": 2.0, "FB": 2.0, "QB": 5.0}
        for pl in players.values():
            t = _tnorm(pl.get("team"))
            if t not in by_team:
                continue
            pos = pl.get("pos")
            use = (pl.get("targets_pg") or 0) if pos in ("WR", "TE") else \
                  (pl.get("carries_pg") or 0) if pos in ("RB", "FB") else \
                  (pl.get("att_pg") or 0)
            if (pl.get("games") or 0) >= 2 and use >= MIN.get(pos, 1):
                by_team[t].append(pl)
        cards = game_cards(H, A, {H: by_team[H], A: by_team[A]}, data.get("defense", {}))
        return {"game_id": game_id, "home_team": home, "away_team": away,
                "count": len(cards), "cards": cards}

    print("[nfl] edge endpoint registered")
