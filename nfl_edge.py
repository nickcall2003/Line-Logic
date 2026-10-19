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


# ---- injury backup boost -----------------------------------------------------
# When a starter is ruled OUT, his vacated per-game volume flows to the next man
# up at that position, so the backup's projection climbs and he's flagged. The
# fractions are how much of the starter's workload the DIRECT backup typically
# absorbs (the rest bleeds to the committee / pass game).
_INHERIT = {
    "RB": {"carries_pg": 0.62, "rush_yards_pg": 0.62, "rush_tds_pg": 0.58,
           "targets_pg": 0.45, "rec_pg": 0.45, "rec_yards_pg": 0.45, "ppr_pg": 0.52},
    "WR": {"target_share": 0.33, "wopr": 0.30, "rec_yards_pg": 0.33,
           "rec_pg": 0.33, "rec_tds_pg": 0.33, "targets_pg": 0.33},
    "TE": {"target_share": 0.42, "wopr": 0.40, "rec_yards_pg": 0.42,
           "rec_pg": 0.42, "rec_tds_pg": 0.42, "targets_pg": 0.42},
    "QB": {"pass_yards_pg": 0.88, "pass_tds_pg": 0.82, "att_pg": 0.95, "cmp_pg": 0.88},
}


def _is_out(status):
    s = (status or "").lower()
    return any(k in s for k in ("out", "injured reserve", "season", " ir",
                                "suspension", "doubtful"))


def apply_injuries(by_team):
    """Mutate {team:[player dicts]} for injuries: drop OUT starters from the prop
    pool and boost the next player at that position by the vacated volume (tagged
    boosted_by). No-op when the injury feed is unavailable."""
    try:
        import injuries
        if not injuries.enabled("nfl"):
            return by_team
    except Exception:
        return by_team
    for team, players in list(by_team.items()):
        info = injuries.for_team("nfl", abbr=str(team).upper(), name=team)
        outs = [o for o in info.get("players", []) if _is_out(o.get("status"))]
        if not outs:
            continue
        out_norm = {_norm(o.get("name")) for o in outs}
        pool = [p for p in players if _norm(p.get("name")) not in out_norm]
        for o in outs:
            pos = (o.get("pos") or "").upper()
            inh = _INHERIT.get(pos)
            if not inh:
                continue
            starter = next((p for p in players
                            if _norm(p.get("name")) == _norm(o.get("name"))), None)
            same = [p for p in pool if (p.get("pos") or "").upper() == pos]
            if not same:
                continue
            use = (lambda p: (p.get("targets_pg") or 0) if pos in ("WR", "TE")
                   else (p.get("carries_pg") or 0) if pos in ("RB", "FB")
                   else (p.get("att_pg") or 0))
            backup = max(same, key=use)
            bk = dict(backup)
            for st, frac in inh.items():
                vac = (starter.get(st) if starter else 0) or 0
                if vac:
                    bk[st] = round((bk.get(st) or 0) + vac * frac, 3)
            bk["boosted_by"] = o.get("name")
            bk["boosted_status"] = (o.get("status") or "OUT")
            pool = [bk if p is backup else p for p in pool]
        by_team[team] = pool
    return by_team


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
                  dranks=_def_ranks(data.get("defense", {})),
                  rates=data.get("league_rates"),
                  depth_def=data.get("depth_def") or {},
                  depth_ranks=_depth_ranks(data.get("depth_def") or {}))
    return data


def _depth_ranks(depth_def):
    """{depth: {team: rank}} ranked by yards/target allowed (1 = softest/most)."""
    from blueprint_nfl import DEPTHS
    R = {}
    for dp in DEPTHS:
        pairs = [(t, d[dp]["ypt"]) for t, d in depth_def.items()
                 if isinstance(d.get(dp), dict) and d[dp].get("ypt") is not None
                 and d[dp].get("tgts", 0) >= 15]
        pairs.sort(key=lambda x: x[1], reverse=True)
        R[dp] = {t: i + 1 for i, (t, _v) in enumerate(pairs)}
    return R


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


# ---- Matchup Tax: HIM vs what the defense allows to his position, with rank ----
_TAX_LABEL = {"rec_yards_pg": "Rec Yds", "rec_pg": "Receptions", "rec_tds_pg": "Rec TD",
              "rush_yards_pg": "Rush Yds", "rush_tds_pg": "Rush TD",
              "pass_yards_pg": "Pass Yds", "pass_tds_pg": "Pass TD"}
_NTEAM = 32


def _tax_grade(r):
    """r = league rank for how much the defense ALLOWS (1 = most generous)."""
    if r is None:
        return "neutral"
    if r <= 10:
        return "batter"     # soft spot — favors the player (gold)
    if r >= 23:
        return "pitcher"    # tough matchup (red)
    return "neutral"


def matchup_tax(p, opp_abbr, defense):
    """A HIM | DEF ALLOWS | RANK table for the player's position vs this defense.
    Reuses the same per-position allowed stats + league ranks the leaks use."""
    pos = p.get("pos")
    dpos = _DEF_POS.get(pos)
    cfg = _LEAK_STATS.get(dpos) or []
    dd = (defense.get(opp_abbr) or {}).get(dpos) or {}
    ranks = _CACHE.get("dranks") or {}
    rows, soft = [], []
    for st, _lab, nd in cfg:
        him = p.get(st)
        allows = dd.get(st)
        r = ranks.get(dpos, {}).get(st, {}).get(opp_abbr)
        if allows is None:
            continue
        rows.append({"label": _TAX_LABEL.get(st, st),
                     "him": round(him, (1 if nd == 1 else 2)) if him is not None else None,
                     "allows": round(allows, (1 if nd == 1 else 2)),
                     "rank": r, "grade": _tax_grade(r)})
        if r is not None and r <= 8:
            soft.append(_TAX_LABEL.get(st, st))
    if not rows:
        return None
    posname = {"WR": "wide receivers", "TE": "tight ends", "RB": "running backs",
               "QB": "quarterbacks"}.get(pos, pos)
    if soft:
        headline = "%s is top-8 generous in %s to %s." % (
            opp_abbr, " & ".join(soft[:2]), posname)
    else:
        headline = "%s is middle-of-the-pack against %s." % (opp_abbr, posname)
    return {"opp": opp_abbr, "pos": pos, "headline": headline, "rows": rows}


# ---- auto descriptor: a two-phrase "player trait + matchup trait" tagline ----
_TRAIT = {  # top radar spoke -> player trait phrase
    "Catch %": "Reliable hands", "Tgt Share": "Target magnet", "WOPR": "Featured target",
    "Rec Yds": "Yardage producer", "aDOT": "Field stretcher", "Air Yds": "Field stretcher",
    "EPA": "Efficient weapon", "YPC": "Efficient runner", "Carries": "Workhorse volume",
    "Rush Yds": "Ground producer", "Targets": "Receiving back", "PPR": "Every-down back",
    "Rush EPA": "Explosive runner", "Pass Yds": "Volume passer", "Pass TD": "Scoring passer",
    "CPOE": "Accurate passer", "Comp %": "Efficient passer", "PACR": "Air-yards earner",
}


def _descriptor(p, c, tax):
    """Venom-style two-phrase summary: the player's standout trait + the matchup
    trait. e.g. 'Reliable hands + Leaky coverage'."""
    pos = p.get("pos")
    radar = c.get("radar") or {}
    skip = {"Matchup", "Logic"}
    spokes = sorted(((k, v) for k, v in radar.items() if k not in skip),
                    key=lambda kv: kv[1], reverse=True)
    trait = None
    for k, v in spokes:
        if v >= 62 and k in _TRAIT:
            trait = _TRAIT[k]
            break
    # matchup phrase from how soft the draw is
    opp = c.get("opportunity") or 50
    mphrase = None
    if opp >= 62:
        mphrase = {"WR": "Leaky coverage", "TE": "Leaky coverage",
                   "RB": "Soft front", "QB": "Soft secondary"}.get(pos)
    # blueprint scoring edge as a fallback matchup phrase
    bp = (c.get("blueprint") or {}).get("scoring") or {}
    if not mphrase and bp.get("debt", 0) >= 1.5:
        mphrase = "Due for points"
    parts = [x for x in (trait, mphrase) if x]
    return " + ".join(parts) if parts else None


# ---- Coverage matchup: where he's targeted vs how the defense defends it ----
def coverage_matchup(p, opp_abbr, games=None):
    """The free alignment/coverage chart: the receiver's target-depth split (his
    'where' on the field) mapped to the defender role that owns each depth, and how
    soft the opposing defense is there (yards/target allowed + league rank)."""
    from blueprint_nfl import DEPTHS, DEPTH_LABEL, DEPTH_COVERAGE
    dz = p.get("depth")
    if not dz:
        return None
    total = sum(int(dz.get(k, 0) or 0) for k in DEPTHS)
    if total < 10:
        return None
    dd = (_CACHE.get("depth_def") or {}).get(opp_abbr) or {}
    ranks = _CACHE.get("depth_ranks") or {}
    rows, edge = [], None
    for dp in DEPTHS:
        tg = int(dz.get(dp, 0) or 0)
        share = round(100.0 * tg / total)
        dstat = dd.get(dp) or {}
        r = ranks.get(dp, {}).get(opp_abbr)
        grade = "batter" if (r and r <= 12) else ("pitcher" if (r and r >= 21) else "neutral")
        row = {"depth": dp, "label": DEPTH_LABEL[dp], "coverage": DEPTH_COVERAGE[dp],
               "share": share, "def_ypt": dstat.get("ypt"),
               "def_comp": (round(100 * dstat["comp"]) if dstat.get("comp") is not None else None),
               "rank": r, "grade": grade}
        rows.append(row)
        # edge = a depth where he's used a lot AND the D is soft
        if share >= 25 and r and r <= 12 and (edge is None or share > edge["share"]):
            edge = row
    # role from his dominant depth tendency
    under = (int(dz.get("screen", 0)) + int(dz.get("short", 0))) / total
    vert = (int(dz.get("inter", 0)) + int(dz.get("deep", 0))) / total
    if under >= 0.65:
        role = "Underneath / slot role"
    elif vert >= 0.55:
        role = "Perimeter / vertical role"
    else:
        role = "Full-field role"
    note = None
    if edge:
        note = "Edge: %d%% of his targets come %s, where %s is %s%s." % (
            edge["share"], DEPTH_LABEL[edge["depth"]].split(" (")[0].lower(),
            opp_abbr, _ordinal_most(edge["rank"]), " generous")
    return {"opp": opp_abbr, "role": role, "adot": dz.get("adot"),
            "rows": rows, "edge_note": note}


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
    card = {
        "player": p.get("name"), "id": p.get("id"), "type": "nfl", "pos": pos,
        "team": p.get("team"), "headshot": p.get("headshot"),
        "logic_score": logic, "baseline": baseline, "opportunity": round(opp, 1),
        "edge_pct": edge, "games": p.get("games"),
        "grid": grid, "radar": radar, "projections": proj,
        "log": p.get("log"),
    }
    # Blueprint: scoring (zone xTD / Scoring Debt) + box-count splits + signals.
    if p.get("zone"):
        try:
            import blueprint_nfl as BP
            bp = BP.blueprint(p.get("zone"), p.get("box"),
                              _CACHE.get("rates"), p.get("games"))
            if bp:
                card["blueprint"] = bp
        except Exception:
            pass
    return card


_KIND = {"pctp": "pct01", "num1": "num1", "num2": "num2", "num3": "num3", "int": "int", "avg": "avg"}


# Manual in-season trade overrides: {normalized player name -> new team abbr}.
# ESPN's roster feed can lag a trade, so these force a player onto his new team
# immediately. Editable live via /data/nfl_trades.json, e.g.
#   {"Marvin Harrison Jr.": "HOU", "Davante Adams": "LAR"}
# falling back to the seed below.
_TRADE_SEED = {"Marvin Harrison Jr.": "HOU"}


def _trade_overrides():
    import json
    try:
        with open("/data/nfl_trades.json") as f:
            d = json.load(f)
        if isinstance(d, dict) and d:
            return {_norm(k): str(v).upper() for k, v in d.items()}
    except Exception:
        pass
    return {_norm(k): v.upper() for k, v in _TRADE_SEED.items()}


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
            c["tax"] = matchup_tax(pl, _tnorm(opp), defense)
            if pl.get("pos") in ("WR", "TE", "RB"):
                c["coverage"] = coverage_matchup(pl, _tnorm(opp), pl.get("games"))
            c["descriptor"] = _descriptor(pl, c, c["tax"])
            if pl.get("boosted_by"):
                c["boosted_by"] = pl["boosted_by"]
                c["boosted_status"] = pl.get("boosted_status") or "OUT"
            if pl.get("traded_from"):
                c["traded_from"] = pl["traded_from"]
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
        # Manual trade overrides FIRST — reliable and ESPN-independent. A player
        # whose override names this game's team is moved here (flagged); one whose
        # override names a different team is dropped from this team.
        try:
            _ov = _trade_overrides()
            if _ov:
                _idx0 = {_norm(pl.get("name")): pl for pl in players.values()}
                for _tn, _ab in ((H, home), (A, away)):
                    _abN = _tnorm(_ab)
                    by_team[_tn] = [p for p in by_team[_tn]
                                    if _norm(p.get("name")) not in _ov
                                    or _tnorm(_ov[_norm(p.get("name"))]) == _abN]
                    _have0 = {_norm(p.get("name")) for p in by_team[_tn]}
                    for _k, _team in _ov.items():
                        if _tnorm(_team) == _abN and _k not in _have0:
                            _pl = _idx0.get(_k)
                            if _pl:
                                _mv = dict(_pl)
                                _mv["team"], _mv["traded_from"] = _ab, _pl.get("team")
                                by_team[_tn].append(_mv)
                                _have0.add(_k)
        except Exception:
            pass
        # Trade remap vs each team's CURRENT roster: drop a profiled player who is
        # no longer on the team (traded away), and add one who is now on it but
        # tagged elsewhere (traded in), flagged. Best-effort — a failed/empty
        # roster read leaves that team untouched.
        try:
            import espn_provider as _ep
            _d = dt.date.fromisoformat(date) if date else dt.date.today()
            _g = _ep.get_game("nfl", _d, game_id)
            if _g:
                _idx = {_norm(pl.get("name")): pl for pl in players.values()}
                for _side, _tn, _ab in (("home", H, home), ("away", A, away)):
                    _tid = (_g.get(_side) or {}).get("team_id")
                    if not _tid:
                        continue
                    _cur = _ep.team_roster_names("nfl", _tid)   # {norm: displayName}
                    if len(_cur) < 20:
                        continue                                # roster read too thin to trust
                    # REMOVE a profiled player no longer on the roster (traded away) —
                    # but ONLY when most of the list still matches, so a name-format
                    # mismatch can never wipe the whole props list.
                    _orig = by_team[_tn]
                    _kept = [p for p in _orig if _norm(p.get("name")) in _cur]
                    if _orig and len(_kept) >= max(3, int(0.6 * len(_orig))):
                        by_team[_tn] = _kept
                    _have = {_norm(p.get("name")) for p in by_team[_tn]}
                    # ADD: a rostered player profiled under another team was traded in
                    for _k in _cur:
                        _pl = _idx.get(_k)
                        if _pl and _k not in _have and _tnorm(_pl.get("team")) != _tn:
                            _mv = dict(_pl)
                            _mv["team"], _mv["traded_from"] = _ab, _pl.get("team")
                            by_team[_tn].append(_mv)
                            _have.add(_k)
        except Exception:
            pass
        roster = {H: by_team[H], A: by_team[A]}
        try:
            apply_injuries(roster)      # drop OUT starters, boost the next man up
        except Exception:
            pass
        cards = game_cards(H, A, roster, data.get("defense", {}))
        return {"game_id": game_id, "home_team": home, "away_team": away,
                "count": len(cards), "cards": cards}

    _NFL_TEAM_ID = {
        "ARI": 22, "ATL": 1, "BAL": 33, "BUF": 2, "CAR": 29, "CHI": 3, "CIN": 4,
        "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9, "HOU": 34, "IND": 11,
        "JAX": 30, "KC": 12, "LV": 13, "LAC": 24, "LAR": 14, "MIA": 15, "MIN": 16,
        "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21, "PIT": 23, "SF": 25,
        "SEA": 26, "TB": 27, "TEN": 10, "WSH": 28, "WAS": 28,
    }

    @app.get("/api/nfl/roster-diag")
    def nfl_roster_diag(team: str, player: str = ""):
        """Dead-simple check: is `player` on `team`'s CURRENT ESPN roster, and what
        does the frozen profile say? e.g. ?team=HOU&player=harrison"""
        import espn_provider as _ep
        from fastapi.responses import JSONResponse as _J
        tid = _NFL_TEAM_ID.get((team or "").upper())
        if not tid:
            return _J({"error": f"unknown team '{team}'", "known": sorted(_NFL_TEAM_ID)})
        names = _ep.team_roster_names("nfl", tid)
        q = (player or "").lower()
        hits = [v for k, v in names.items() if q and q in k]
        data = load()
        prof = [{"name": p.get("name"), "profile_team": p.get("team"), "pos": p.get("pos")}
                for p in (data or {}).get("players", {}).values()
                if q and q in _norm(p.get("name"))]
        return _J({"team": team.upper(), "team_id": tid, "roster_count": len(names),
                   "query": q, "on_current_roster": hits, "profile_says": prof,
                   "sample_roster": list(names.values())[:8]},
                  headers={"Cache-Control": "no-store"})

    @app.get("/api/nfl/trade-diag")
    def nfl_trade_diag(game_id: str, date: str | None = None,
                       home: str | None = None, away: str | None = None,
                       player: str = "harrison"):
        """What the trade remap sees from ESPN for one game. Hit this to confirm
        whether a player (e.g. 'harrison') is on each team's CURRENT roster and
        what team the frozen profile has him on."""
        import espn_provider as _ep
        data = load()
        out = {"game_id": game_id, "player_query": player.lower()}
        try:
            _d = dt.date.fromisoformat(date) if date else dt.date.today()
            g = _ep.get_game("nfl", _d, game_id)
            out["game_resolved"] = bool(g)
            prof = (data or {}).get("players", {})
            prof_hits = [{"name": p.get("name"), "profile_team": p.get("team"), "pos": p.get("pos")}
                         for p in prof.values()
                         if player.lower() in _norm(p.get("name"))]
            out["profile_matches"] = prof_hits
            sides = {}
            for s in ("home", "away"):
                tid = (g.get(s) or {}).get("team_id") if g else None
                abbr = (g.get(s) or {}).get("abbr") if g else None
                names = _ep.team_roster_names("nfl", tid) if tid else {}
                hits = [v for k, v in names.items() if player.lower() in k]
                sides[s] = {"abbr": abbr, "team_id": tid, "roster_count": len(names),
                            "query_on_roster": hits,
                            "sample": list(names.values())[:5]}
            out["teams"] = sides
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        from fastapi.responses import JSONResponse as _J
        return _J(out, headers={"Cache-Control": "no-store"})

    print("[nfl] edge endpoint registered")
