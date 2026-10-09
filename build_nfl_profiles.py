#!/usr/bin/env python3
"""
Build nfl_profiles.json — the advanced-stats engine behind the NFL Player Edge.

Source: nflverse weekly player stats (free), which already carries the Next Gen /
advanced toolkit: CPOE, EPA, target share, air-yards share, WOPR, RACR, aDOT, YAC,
plus the official NFL headshot URL per player.

  https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv

Produces, per player (regular season, aggregated across weeks):
  usage        - targets/g, target_share, air_yards_share, wopr, carries/g, routes-ish
  efficiency   - yards/target, aDOT, YAC/rec, catch rate, EPA/play, CPOE (QB), PACR
  volume       - per-game receptions, rec yards, rush yards, pass yards/TDs
And per team defense, yards allowed to each position per game (the matchup funnel).

Env: NFL_SEASON (default: current season), LOCAL_NFL (dir with stats_player_week_{season}.csv),
     NFL_OUT (default mlb_... no — nfl_profiles.json).
"""
import csv
import io
import json
import os
import datetime as dt
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (LineLogic NFLBuilder)"}
_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
        "stats_player/stats_player_week_{season}.csv")
_SKILL = {"QB", "RB", "WR", "TE", "FB"}


def _season():
    # NFL season spans Sep–Feb; before September, the "current" season is last year.
    t = dt.date.today()
    return t.year if t.month >= 9 else t.year - 1


def _fetch(season):
    local = os.environ.get("LOCAL_NFL")
    if local:
        p = os.path.join(local, f"stats_player_week_{season}.csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return list(csv.DictReader(f))
        return []
    url = _URL.format(season=season)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=90) as r:
            return list(csv.DictReader(io.StringIO(r.read().decode("utf-8", "replace"))))
    except Exception as e:
        print(f"[nfl] fetch {season}: {e}")
        return []


def _f(row, key):
    v = row.get(key)
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _acc():
    return {"g": 0, "sum": {}, "avg": {}, "avgn": {}}


def _add(a, row, sum_keys, avg_keys):
    a["g"] += 1
    for k in sum_keys:
        v = _f(row, k)
        if v is not None:
            a["sum"][k] = a["sum"].get(k, 0.0) + v
    for k in avg_keys:
        v = _f(row, k)
        if v is not None:
            a["avg"][k] = a["avg"].get(k, 0.0) + v
            a["avgn"][k] = a["avgn"].get(k, 0) + 1


_SUM = ["targets", "receptions", "receiving_yards", "receiving_tds", "receiving_air_yards",
        "receiving_yards_after_catch", "receiving_first_downs",
        "carries", "rushing_yards", "rushing_tds",
        "attempts", "completions", "passing_yards", "passing_tds", "passing_air_yards",
        "receiving_epa", "rushing_epa", "passing_epa", "fantasy_points_ppr"]
_AVG = ["target_share", "air_yards_share", "wopr", "racr", "passing_cpoe", "pacr"]


def _rate(s, k, g, nd=2):
    return round(s.get(k, 0.0) / g, nd) if g else None


def build(season=None, out_path="nfl_profiles.json"):
    season = int(season or os.environ.get("NFL_SEASON") or _season())
    rows = _fetch(season)
    if len(rows) < 100 and not os.environ.get("LOCAL_NFL"):
        ps = season - 1
        print(f"[nfl] {season} thin ({len(rows)} rows) — trying {ps}")
        r2 = _fetch(ps)
        if len(r2) > len(rows):
            rows, season = r2, ps

    players, def_acc, team_games = {}, {}, {}
    for r in rows:
        if (r.get("season_type") or "REG") != "REG":
            continue
        pos = (r.get("position") or "").upper()
        pid = r.get("player_id")
        if not pid:
            continue
        P = players.get(pid)
        if P is None:
            P = players[pid] = {
                "name": r.get("player_display_name") or r.get("player_name"),
                "pos": pos, "team": r.get("team"),
                "headshot": r.get("headshot_url") or None, "acc": _acc(),
            }
        P["team"] = r.get("team") or P["team"]   # latest team
        _add(P["acc"], r, _SUM, _AVG)
        # defense funnel: credit the opponent defense with what this position did
        opp = r.get("opponent_team")
        if opp and pos in _SKILL:
            d = def_acc.setdefault(opp, {}).setdefault(pos, _acc())
            _add(d, r, ["receiving_yards", "rushing_yards", "receptions", "targets",
                        "passing_yards", "receiving_tds", "rushing_tds", "passing_tds"], [])
        gid = r.get("game_id")
        if opp and gid:
            team_games.setdefault(opp, set()).add(gid)

    def finalize_player(P):
        a, g = P["acc"], P["acc"]["g"]
        s = a["sum"]
        def av(k):
            n = a["avgn"].get(k, 0)
            return round(a["avg"].get(k, 0.0) / n, 3) if n else None
        tgt = s.get("targets", 0.0); rec = s.get("receptions", 0.0)
        ray = s.get("receiving_air_yards", 0.0); att = s.get("attempts", 0.0)
        out = {
            "name": P["name"], "pos": P["pos"], "team": P["team"], "headshot": P["headshot"],
            "games": g,
            # receiving usage + efficiency
            "targets_pg": _rate(s, "targets", g), "target_share": av("target_share"),
            "air_yards_share": av("air_yards_share"), "wopr": av("wopr"), "racr": av("racr"),
            "rec_pg": _rate(s, "receptions", g), "rec_yards_pg": _rate(s, "receiving_yards", g, 1),
            "rec_tds_pg": _rate(s, "receiving_tds", g, 3),
            "adot": round(ray / tgt, 2) if tgt else None,
            "catch_rate": round(rec / tgt, 3) if tgt else None,
            "yac_pg": _rate(s, "receiving_yards_after_catch", g, 1),
            "rec_epa_pg": _rate(s, "receiving_epa", g, 2),
            "ypt": round(s.get("receiving_yards", 0.0) / tgt, 2) if tgt else None,
            # rushing
            "carries_pg": _rate(s, "carries", g), "rush_yards_pg": _rate(s, "rushing_yards", g, 1),
            "rush_tds_pg": _rate(s, "rushing_tds", g, 3),
            "ypc": round(s.get("rushing_yards", 0.0) / s.get("carries", 0.0), 2) if s.get("carries") else None,
            "rush_epa_pg": _rate(s, "rushing_epa", g, 2),
            # passing
            "att_pg": _rate(s, "attempts", g, 1), "cmp_pg": _rate(s, "completions", g, 1),
            "pass_yards_pg": _rate(s, "passing_yards", g, 1), "pass_tds_pg": _rate(s, "passing_tds", g, 3),
            "cpoe": av("passing_cpoe"), "pacr": av("pacr"), "pass_epa_pg": _rate(s, "passing_epa", g, 2),
            "comp_pct": round(s.get("completions", 0.0) / att, 3) if att else None,
            "ppr_pg": _rate(s, "fantasy_points_ppr", g, 1),
        }
        return out

    fin = {pid: finalize_player(P) for pid, P in players.items()
           if P["acc"]["g"] >= 1 and P["pos"] in _SKILL}

    defense = {}
    for team, bypos in def_acc.items():
        tg = max(1, len(team_games.get(team, set())))
        defense[team] = {"games": tg}
        for pos, d in bypos.items():
            s = d["sum"]
            defense[team][pos] = {
                "rec_yards_pg": round(s.get("receiving_yards", 0.0) / tg, 1),
                "rush_yards_pg": round(s.get("rushing_yards", 0.0) / tg, 1),
                "rec_pg": round(s.get("receptions", 0.0) / tg, 1),
                "pass_yards_pg": round(s.get("passing_yards", 0.0) / tg, 1),
                "tds_pg": round((s.get("receiving_tds", 0.0) + s.get("rushing_tds", 0.0)
                                 + s.get("passing_tds", 0.0)) / tg, 2),
            }

    out = {"generated": dt.datetime.utcnow().isoformat() + "Z", "season": season,
           "players": fin, "defense": defense}
    print(f"[nfl] {season}: {len(fin)} skill players, {len(defense)} defenses from {len(rows)} rows")
    if fin:
        with open(out_path, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        print(f"[nfl] wrote {out_path} ({os.path.getsize(out_path)} bytes)")
    else:
        print("[nfl] refusing to write empty profile")
    return out


if __name__ == "__main__":
    build(out_path=os.environ.get("NFL_OUT", "nfl_profiles.json"))
