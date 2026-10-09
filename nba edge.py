"""
nba_edge.py — Player Edge scoring for the NBA/WNBA, live from ESPN + the uploaded
hoops_advanced snapshot (no static profile file).

Per player vs the opposing defense:
  - Logic Score (0-100) = Baseline (usage, scoring, minutes, efficiency percentiles)
    blended with Opportunity (opponent defensive rating + pace + defense-vs-position),
  - a radar of role/efficiency drivers,
  - a graded season-average grid,
  - projections (points / rebounds / assists / 3PM) with the same pace + DvP factors
    the prop model already uses,
  - matchup leaks from real defense-vs-position ranks ("allows the most pts to guards").

Same card shape as venom.py / nfl_edge.py / nhl_edge.py so the one Player Edge UI
renders it. Everything is best-effort and returns [] on any failure — a blocked
host or missing snapshot must never break the page or invent a number.
"""
import math
import datetime as dt

# reference per-game distributions for rotation players (mean, std). Used for the
# baseline percentiles when a league-wide fetch isn't cheap; usage/min/ts come from
# the real hoops snapshot when present.
_REF = {
    "nba": {"points": (12.0, 7.0), "rebounds": (4.6, 2.9), "assists": (2.9, 2.3),
            "threes": (1.4, 1.1), "usg": (18.5, 5.5), "min": (24.0, 8.0),
            "ts": (56.5, 6.0), "def_rtg": (113.0, 3.5), "pace": (99.5, 2.5)},
    "wnba": {"points": (10.5, 6.0), "rebounds": (4.2, 2.6), "assists": (2.6, 2.1),
             "threes": (1.1, 0.9), "usg": (18.5, 5.5), "min": (22.0, 8.0),
             "ts": (54.0, 6.0), "def_rtg": (101.0, 3.5), "pace": (95.0, 2.5)},
}
_LG_DVP = {
    "nba": {"pts": 24.0, "reb": 10.5, "ast": 6.0, "fg3m": 3.2},
    "wnba": {"pts": 18.0, "reb": 8.5, "ast": 4.5, "fg3m": 2.0},
}
_POS_LABEL = {"Guard": "guards", "Forward": "forwards", "Center": "centers"}
_HEADSHOT = {
    "nba": "https://a.espncdn.com/i/headshots/nba/players/full/{id}.png",
    "wnba": "https://a.espncdn.com/i/headshots/wnba/players/full/{id}.png",
}


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _pctile(z):
    return round(100 * 0.5 * (1 + math.erf(z / math.sqrt(2))), 1)


def _z(v, mean, std):
    if v is None:
        return 0.0
    return (float(v) - mean) / (std or 1.0)


def _pz(v, ref):
    return _pctile(_z(v, ref[0], ref[1]))


def _grade(z):
    return "batter" if z >= 0.45 else ("pitcher" if z <= -0.45 else "neutral")


def _norm_pct(v):
    """hoops usg_pct / ts_pct arrive as a fraction (<1) or a percent; normalize to %."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v * 100.0 if v < 1.0 else v


def _pos_bucket(pos):
    p = str(pos or "").upper()
    if p in ("C", "CENTER"):
        return "Center"
    if p in ("G", "PG", "SG", "GUARD"):
        return "Guard"
    if p in ("F", "SF", "PF", "FORWARD"):
        return "Forward"
    return "Forward"


def _league_usage_stats(usage):
    """Real mean/std of usg%/min/ts% from the hoops snapshot, else None."""
    if not usage:
        return None
    out = {}
    for key, src in (("usg", "usg_pct"), ("min", "min"), ("ts", "ts_pct")):
        vals = []
        for u in usage.values():
            v = u.get(src)
            if v is None:
                continue
            v = _norm_pct(v) if key in ("usg", "ts") else float(v)
            if v is not None:
                vals.append(v)
        if len(vals) >= 20:
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)) or 1.0
            out[key] = (m, sd)
    return out or None


def _league_team_stats(team_adv):
    """Real mean/std of def_rtg and pace across all teams, else None."""
    if not team_adv:
        return None
    out = {}
    for key, src in (("def_rtg", "def_rtg"), ("pace", "pace")):
        vals = [float(t[src]) for t in team_adv.values()
                if t.get(src) is not None]
        if len(vals) >= 6:
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)) or 1.0
            out[key] = (m, sd)
    return out or None


def _dvp_ranks(HA, sport, team_names):
    """{bucket: {stat: {TEAM_UPPER: rank}}}, rank 1 = allows the MOST (leaky).
    Built from the uploaded snapshot via defense_vs_position (local, no network)."""
    R = {}
    for bucket in ("Guard", "Forward", "Center"):
        R[bucket] = {}
        rows = {}
        for tn in team_names:
            try:
                row = HA.defense_vs_position(sport, tn, bucket)
            except Exception:
                row = None
            if row:
                rows[str(tn).upper()] = row
        for stat in ("pts", "reb", "ast", "fg3m"):
            pairs = [(t, r.get(stat)) for t, r in rows.items() if r.get(stat) is not None]
            pairs.sort(key=lambda x: float(x[1]), reverse=True)
            R[bucket][stat] = {t: i + 1 for i, (t, _v) in enumerate(pairs)}
    return R


def _ord_most(r):
    return "most" if r == 1 else ("2nd-most" if r == 2 else
           ("3rd-most" if r == 3 else str(r) + "th-most"))


def _leaks(sport, opp_name, bucket, HA, ranks):
    try:
        row = HA.defense_vs_position(sport, opp_name, bucket)
    except Exception:
        row = None
    if not row:
        return []
    key = str(opp_name).upper()
    poslab = _POS_LABEL.get(bucket, "players")
    specs = [("pts", "points"), ("reb", "rebounds"), ("ast", "assists"), ("fg3m", "3-pointers")]
    out = []
    for stat, label in specs:
        r = (ranks.get(bucket, {}).get(stat, {}) or {}).get(key)
        v = row.get(stat)
        if r is None or v is None or r > 10:
            continue
        out.append({"text": "%s allows the %s %s to %s this season (%s/g)."
                    % (key, _ord_most(r), label, poslab, round(float(v), 1)),
                    "rank": r, "strong": r <= 5})
    out.sort(key=lambda x: x["rank"])
    return out[:3]


def _factors(sport, own_adv, opp_adv):
    """pace + defensive-rating multipliers (replicates get_props)."""
    ref = _REF[sport]
    lg_drtg, lg_pace = ref["def_rtg"][0], ref["pace"][0]
    if opp_adv and opp_adv.get("def_rtg"):
        def_factor = _clamp(float(opp_adv["def_rtg"]) / lg_drtg, 0.88, 1.12)
    else:
        def_factor = 1.0
    if own_adv and own_adv.get("pace") and opp_adv and opp_adv.get("pace"):
        pace_factor = _clamp((float(own_adv["pace"]) + float(opp_adv["pace"]))
                             / (2 * lg_pace), 0.90, 1.10)
    else:
        pace_factor = 1.0
    return def_factor, pace_factor


def _dvp_factor(sport, opp_name, bucket, stat_key, HA):
    base = _LG_DVP[sport]
    skey = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m"}.get(stat_key)
    if not skey:
        return 1.0
    try:
        row = HA.defense_vs_position(sport, opp_name, bucket)
    except Exception:
        row = None
    if not row or row.get(skey) is None or not base.get(skey):
        return 1.0
    return _clamp(float(row[skey]) / base[skey], 0.85, 1.15)


def _score_player(sport, name, avgs, bucket, usage_row, usage_lg, team_lg,
                  own_adv, opp_adv, opp_name, HA, ranks, team_abbr, opp_abbr, pid):
    ref = _REF[sport]
    pts, reb, ast = avgs.get("points"), avgs.get("rebounds"), avgs.get("assists")
    thr = avgs.get("threes")
    usg = _norm_pct(usage_row.get("usg_pct")) if usage_row else None
    mins = float(usage_row["min"]) if (usage_row and usage_row.get("min") is not None) else None
    ts = _norm_pct(usage_row.get("ts_pct")) if usage_row else None

    def pz(v, key):
        lg = (usage_lg or {}).get(key)
        return _pctile(_z(v, lg[0], lg[1])) if lg else _pz(v, ref[key])

    p_usg = pz(usg, "usg") if usg is not None else 50.0
    p_min = pz(mins, "min") if mins is not None else 50.0
    p_ts = pz(ts, "ts") if ts is not None else 50.0
    p_pts = _pz(pts, ref["points"]) if pts is not None else 0.0
    p_reb = _pz(reb, ref["rebounds"]) if reb is not None else 0.0
    p_ast = _pz(ast, ref["assists"]) if ast is not None else 0.0
    play = round((p_reb + p_ast) / 2, 1)
    baseline = round(0.28 * p_pts + 0.26 * p_usg + 0.18 * p_min
                     + 0.12 * p_ts + 0.16 * play, 1)

    # opportunity: weak opp D (high def_rtg) + fast pace + favorable DvP for pts
    def_factor, pace_factor = _factors(sport, own_adv, opp_adv)
    tl = team_lg or {}
    z_def = _z(opp_adv.get("def_rtg"), *tl["def_rtg"]) if (opp_adv and tl.get("def_rtg")) else 0.0
    z_pace = 0.0
    if tl.get("pace") and own_adv and opp_adv and own_adv.get("pace") and opp_adv.get("pace"):
        z_pace = _z((float(own_adv["pace"]) + float(opp_adv["pace"])) / 2, *tl["pace"])
    dvp_pts = _dvp_factor(sport, opp_name, bucket, "points", HA)
    z_dvp = (dvp_pts - 1.0) / 0.06
    opp = _pctile(0.45 * z_def + 0.30 * z_pace + 0.25 * z_dvp)
    edge = round((opp - 50) / 5.0, 1)
    logic = round(0.62 * baseline + 0.38 * opp, 1)

    proj = {}
    if pts is not None:
        proj["points"] = round(pts * pace_factor * _dvp_factor(sport, opp_name, bucket, "points", HA), 2)
    if reb is not None:
        proj["rebounds"] = round(reb * pace_factor * _dvp_factor(sport, opp_name, bucket, "rebounds", HA), 2)
    if ast is not None:
        proj["assists"] = round(ast * pace_factor * _dvp_factor(sport, opp_name, bucket, "assists", HA), 2)
    if thr is not None:
        proj["threes"] = round(thr * pace_factor * _dvp_factor(sport, opp_name, bucket, "threes", HA), 2)

    radar = {"Usage": p_usg, "Scoring": p_pts, "Minutes": p_min,
             "Efficiency": p_ts, "Playmaking": play,
             "Matchup": round(opp, 1), "Logic": logic}
    grid = []
    for key, label, kind, val in (
            ("points", "Points/g", "num1", pts), ("rebounds", "Reb/g", "num1", reb),
            ("assists", "Ast/g", "num1", ast), ("threes", "3PM/g", "num1", thr),
            ("usg", "Usage %", "num1", usg), ("min", "Minutes", "num1", mins),
            ("ts", "TS %", "num1", ts)):
        if val is None:
            continue
        rf = ref.get(key)
        z = _z(val, rf[0], rf[1]) if rf else 0.0
        grid.append({"label": label, "value": round(float(val), 1), "kind": kind,
                     "grade": _grade(z)})
    head = _HEADSHOT.get(sport, "").format(id=pid) if pid else None
    return {"player": name, "id": pid, "type": "nba", "pos": _pos_disp(bucket),
            "team": team_abbr, "opp": opp_abbr, "headshot": head,
            "logic_score": logic, "baseline": baseline, "opportunity": round(opp, 1),
            "edge_pct": edge, "grid": grid, "radar": radar, "projections": proj,
            "leaks": _leaks(sport, opp_name, bucket, HA, ranks)}


def _pos_disp(bucket):
    return {"Guard": "G", "Forward": "F", "Center": "C"}.get(bucket, "")


def game_cards(sport, game_id, date):
    try:
        import espn_provider as EP
        import hoops_advanced as HA
    except Exception:
        return None
    try:
        d = dt.date.fromisoformat(date) if date else dt.date.today()
    except Exception:
        d = dt.date.today()
    g = EP.get_game(sport, d, game_id)
    if not g:
        return {"error": "game not found", "cards": []}
    team_adv = HA.team_advanced(sport) or {}
    usage = HA.player_usage(sport) or {}
    usage_lg = _league_usage_stats(usage)
    team_lg = _league_team_stats(team_adv)
    team_names = list(team_adv.keys())
    ranks = _dvp_ranks(HA, sport, team_names)

    def _adv_for(name):
        if not team_adv or not name:
            return None
        n = str(name).upper()
        for k, v in team_adv.items():
            if k == n or n in k or k in n:
                return v
        return None

    cards = []
    for side in ("home", "away"):
        opp = "away" if side == "home" else "home"
        tid = g[side].get("team_id")
        tabbr = g[side].get("abbr", "")
        oabbr = g[opp].get("abbr", "")
        oname = g[opp].get("name") or oabbr
        own_adv = _adv_for(g[side].get("name") or tabbr)
        opp_adv = _adv_for(oname)
        avgs = EP._team_athlete_avgs(sport, tid)
        if not avgs:
            last = dt.date.today().year - (1 if dt.date.today().month >= 8 else 2)
            try:
                avgs = EP._team_athlete_avgs(sport, tid, season=last) or {}
            except Exception:
                avgs = {}
        try:
            roster = EP._team_roster_ids(sport, tid) or {}
        except Exception:
            roster = {}
        for name, vals in avgs.items():
            if not vals or vals.get("points") is None:
                continue
            if (vals.get("points") or 0) < 2 and (vals.get("rebounds") or 0) < 2 \
                    and (vals.get("assists") or 0) < 1.5:
                continue
            bucket = _pos_bucket(vals.get("_pos"))
            urow = usage.get(str(name).lower())
            pid = roster.get(str(name).lower())
            c = _score_player(sport, name, vals, bucket, urow, usage_lg, team_lg,
                              own_adv, opp_adv, oname, HA, ranks, tabbr, oabbr, pid)
            cards.append(c)
    cards.sort(key=lambda c: c.get("logic_score") or 0, reverse=True)
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return {"game_id": game_id, "home_team": g["home"].get("abbr"),
            "away_team": g["away"].get("abbr"), "count": len(cards), "cards": cards}


def register(app):
    from fastapi.responses import JSONResponse

    @app.get("/api/nba/venom/{game_id}")
    def nba_venom(game_id: str, date: str | None = None,
                  home: str | None = None, away: str | None = None):
        try:
            res = game_cards("nba", game_id, date)
        except Exception as e:
            return JSONResponse({"error": str(e)[:200], "cards": []})
        if res is None:
            return JSONResponse({"error": "nba edge unavailable", "cards": []})
        return res

    @app.get("/api/wnba/venom/{game_id}")
    def wnba_venom(game_id: str, date: str | None = None,
                   home: str | None = None, away: str | None = None):
        try:
            res = game_cards("wnba", game_id, date)
        except Exception as e:
            return JSONResponse({"error": str(e)[:200], "cards": []})
        if res is None:
            return JSONResponse({"error": "wnba edge unavailable", "cards": []})
        return res

    print("[nba] edge endpoint registered")
