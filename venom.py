"""
venom.py — the Venom-style scoring engine for MLB props.

Turns the Statcast profiles (mlb_statcast.json) into, per batter-vs-pitcher matchup:
  - a composite VENOM SCORE (0-100) with Baseline (batter quality) + Opportunity
    (matchup) components and a model-edge %,
  - a radar of the drivers,
  - the Statcast metric grid (xwOBA, barrel%, hard-hit%, exit velo, FB%, …) each
    graded batter-edge / neutral / pitcher-edge,
  - the opposing pitcher's arsenal ("cheat code"),
  - per-stat projections (HR, hits, total bases, RBIs, runs) grounded in the
    expected-outcome rates and nudged by the matchup.

Pure library: no network, no framework. load() reads the cached JSON the builder
writes; register(app) adds /api/mlb/venom/{game_id} using the existing provider.
"""
import json
import os
import math
import unicodedata
import datetime as dt

_CACHE = {"data": None, "mtime": 0.0, "bidx": None, "pidx": None, "lg": None}

# weights for the batter Baseline (sum ~1) — power/contact quality blend
_BWEIGHTS = {"barrel_pct": 0.22, "hard_hit_pct": 0.18, "exit_velo": 0.14,
             "xwoba": 0.24, "xslg": 0.14, "fb_ld_pct": 0.08}
# expected PA by lineup slot (top of order sees more)
_PA_BY_ORDER = {1: 4.6, 2: 4.5, 3: 4.4, 4: 4.3, 5: 4.1, 6: 4.0, 7: 3.9, 8: 3.8, 9: 3.7}


def _norm(name):
    if not name:
        return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(name))
                if not unicodedata.combining(c)).lower()
    for junk in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
        if s.endswith(junk):
            s = s[: -len(junk)]
    return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip()


def _path():
    for p in ("/data/mlb_statcast.json", "mlb_statcast.json"):
        if os.path.exists(p):
            return p
    return None


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
    bidx, pidx = {}, {}
    for pid, b in data.get("batters", {}).items():
        b["id"] = pid
        bidx.setdefault(_norm(b.get("name")), b)
    for pid, pit in data.get("pitchers", {}).items():
        pit["id"] = pid
        pidx.setdefault(_norm(pit.get("name")), pit)
    _CACHE.update(data=data, mtime=mt, bidx=bidx, pidx=pidx,
                  lg=_league(data.get("batters", {}), data.get("pitchers", {})))
    return data


def _league(batters, pitchers):
    """Mean/std of each key metric for z-scoring (batters with enough PA)."""
    def stats(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        if len(vals) < 5:
            return (0.0, 1.0)
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        return (m, math.sqrt(var) or 1.0)
    bs = [b for b in batters.values() if (b.get("pa") or 0) >= 100]
    lg = {"batter": {}, "pitcher": {}}
    for k in ("barrel_pct", "hard_hit_pct", "exit_velo", "xwoba", "xslg",
              "fb_ld_pct", "xba", "barrel_pa"):
        lg["batter"][k] = stats([b.get(k) for b in bs])
    ps = [p for p in pitchers.values() if (p.get("pitches") or 0) >= 200]
    for k in ("xwoba", "hard_hit_pct", "k_pct", "whiff_pct"):
        lg["pitcher"][k] = stats([p.get(k) for p in ps])
    return lg


def _z(val, ms):
    if val is None:
        return 0.0
    m, s = ms
    return (val - m) / (s or 1.0)


def _pctile(z):
    """Normal CDF → 0-100 percentile."""
    return round(100 * 0.5 * (1 + math.erf(z / math.sqrt(2))), 1)


def find_batter(name):
    load()
    return (_CACHE["bidx"] or {}).get(_norm(name))


def find_pitcher(name):
    load()
    return (_CACHE["pidx"] or {}).get(_norm(name))


def _grade(z):
    """batter-edge / neutral / pitcher-edge from a z-score."""
    if z >= 0.45:
        return "batter"
    if z <= -0.45:
        return "pitcher"
    return "neutral"


def score_batter(b, pitcher, order=None):
    """Full Venom card for one batter vs one pitcher (pitcher may be None)."""
    load()
    lg = _CACHE["lg"]
    lb = lg["batter"]
    # ----- Baseline: batter's own quality (0-100) -----
    comps, base_acc, wsum = {}, 0.0, 0.0
    for k, w in _BWEIGHTS.items():
        z = _z(b.get(k), lb.get(k, (0, 1)))
        pc = _pctile(z)
        comps[k] = pc
        base_acc += w * pc
        wsum += w
    baseline = round(base_acc / (wsum or 1), 1)
    # ----- Opportunity: how favorable the matchup is (0-100) -----
    opp, edge_pct, arsenal = 50.0, 0.0, []
    if pitcher:
        lp = lg["pitcher"]
        z_xwoba = _z(pitcher.get("xwoba"), lp.get("xwoba", (0, 1)))     # high = hittable
        z_hard = _z(pitcher.get("hard_hit_pct"), lp.get("hard_hit_pct", (0, 1)))
        z_k = _z(pitcher.get("k_pct"), lp.get("k_pct", (0, 1)))          # high = tough
        opp = _pctile(0.55 * z_xwoba + 0.25 * z_hard - 0.45 * z_k)
        edge_pct = round((opp - 50) / 5.0, 1)                            # ±10% typical
        arsenal = pitcher.get("arsenal", [])
    venom = round(0.62 * baseline + 0.38 * opp, 1)
    # ----- per-stat projections (rate × expected PA × matchup) -----
    pa = _PA_BY_ORDER.get(order or 4, 4.2)
    ab = pa * 0.88
    mf = 1.0 + max(-0.18, min(0.18, edge_pct / 100.0))     # matchup multiplier
    xba = b.get("xba") if b.get("xba") is not None else b.get("ba")
    xslg = b.get("xslg") if b.get("xslg") is not None else b.get("slg")
    brl_pa = b.get("barrel_pa")
    proj = {}
    if xba is not None:
        proj["hits"] = round(xba * ab * mf, 2)
    if xslg is not None:
        proj["total_bases"] = round(xslg * ab * mf, 2)
    if brl_pa is not None:
        proj["home_runs"] = round((brl_pa / 100.0) * 0.42 * pa * mf, 2)
    elif b.get("barrel_pct") is not None and b.get("fb_ld_pct") is not None:
        proj["home_runs"] = round((b["barrel_pct"] / 100.0) * (b["fb_ld_pct"] / 100.0)
                                  * 1.7 * ab * mf, 2)
    if "hits" in proj:
        proj["rbis"] = round(proj.get("total_bases", proj["hits"]) * 0.28 * mf, 2)
        proj["runs"] = round(proj["hits"] * 0.46 * mf, 2)
    # ----- radar + graded metric grid -----
    radar = {
        "Barrel %": comps.get("barrel_pct", 0), "Hard Hit %": comps.get("hard_hit_pct", 0),
        "Exit Velo": comps.get("exit_velo", 0), "xwOBA": comps.get("xwoba", 0),
        "xSLG": comps.get("xslg", 0), "Fly Ball %": comps.get("fb_ld_pct", 0),
        "Matchup": round(opp, 1), "Venom": venom,
    }
    grid = _metric_grid(b, lb)
    return {
        "player": b.get("name"), "venom_score": venom,
        "baseline": baseline, "opportunity": round(opp, 1), "edge_pct": edge_pct,
        "metrics": {k: b.get(k) for k in
                    ("xwoba", "xba", "xslg", "woba", "ba", "slg", "exit_velo",
                     "barrel_pct", "hard_hit_pct", "fb_ld_pct", "gb_pct",
                     "sweet_spot_pct", "avg_hr_dist", "pa")},
        "grid": grid, "radar": radar, "projections": proj,
        "pitcher": (pitcher or {}).get("name"), "arsenal": arsenal,
    }


_GRID_SPEC = [  # (key, label, higher_is_better, kind)
    ("home_runs_season", "HR", True, "int"),
    ("xba", "xBA", True, "avg"), ("xwoba", "xwOBA", True, "avg"),
    ("barrel_pct", "Barrel %", True, "pct"), ("hard_hit_pct", "Hard Hit %", True, "pct"),
    ("fb_ld_pct", "Fly Ball %", True, "pct"), ("exit_velo", "Exit Velo", True, "mph"),
    ("xslg", "xSLG", True, "avg"), ("sweet_spot_pct", "Sweet Spot %", True, "pct"),
]


def _metric_grid(b, lb):
    out = []
    for key, label, hib, kind in _GRID_SPEC:
        val = b.get(key)
        if val is None:
            continue
        ms = lb.get(key)
        z = _z(val, ms) if ms else 0.0
        if not hib:
            z = -z
        out.append({"label": label, "value": val, "kind": kind, "grade": _grade(z)})
    return out


def game_cards(matchups):
    """From get_matchups() output, build Venom cards for every batter on both sides,
    sorted by Venom Score (highest first)."""
    load()
    cards = []
    for side in ("away", "home"):
        s = matchups.get(side) or {}
        pit = find_pitcher(s.get("pitcher"))
        team = matchups.get(side + "_team")
        for row in s.get("batters", []):
            bp = find_batter(row.get("batter"))
            if not bp:
                continue
            card = score_batter(bp, pit, order=row.get("order"))
            card["team"] = team
            card["order"] = row.get("order")
            card["side"] = side
            cards.append(card)
    cards.sort(key=lambda c: c.get("venom_score") or 0, reverse=True)
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return cards


def register(app):
    from fastapi.responses import JSONResponse

    @app.get("/api/mlb/venom/{game_id}")
    def mlb_venom(game_id: int, date: str | None = None):
        if load() is None:
            return JSONResponse({"error": "statcast profiles not built yet",
                                 "build": "/api/mlb/statcast/build?confirm=yes"})
        target = dt.date.fromisoformat(date) if date else dt.date.today()
        try:
            from mlb_provider import get_matchups
            mu = get_matchups(target, game_id)
        except Exception as e:
            return JSONResponse({"error": f"matchups unavailable: {e}"})
        if not mu or mu.get("error"):
            return JSONResponse({"error": "matchups unavailable"})
        cards = game_cards(mu)
        return {"game_id": game_id, "home_team": mu.get("home_team"),
                "away_team": mu.get("away_team"),
                "pitchers": {"home": (mu.get("away") or {}).get("pitcher"),
                             "away": (mu.get("home") or {}).get("pitcher")},
                "count": len(cards), "cards": cards}

    print("[venom] MLB venom endpoint registered")
