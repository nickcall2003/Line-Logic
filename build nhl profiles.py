#!/usr/bin/env python3
"""
Build nhl_profiles.json — the advanced-stats engine behind the NHL Player Edge.

Sources (both confirmed reachable from the server):
  - NHL official stats API (api.nhle.com): skater + goalie season summaries
    (goals, assists, points, shots, shooting%, TOI, PP, save%, GAA, …)
  - MoneyPuck seasonSummary skaters.csv: expected goals (xG), Corsi, shot attempts,
    on-ice xG% — the "Statcast-equivalent" shot-quality layer.

Produces, per skater (merged by NHL playerId):
  production - goals/g, assists/g, points/g, shots/g (SOG), shooting%, PP pts/g, TOI/g
  advanced   - xG/60, shots/60, individual Corsi/60, Corsi%, shooting talent (G - xG),
               game score/g
And per team: a defense funnel (shots/goals allowed per game) + the primary goalie's
save% — derived from the goalie summary, keyed by the same team abbreviation as skaters.

Env: NHL_SEASON (e.g. 20252026), LOCAL_NHL (dir of saved JSON/CSV), NHL_OUT.
Runs on a host that reaches api.nhle.com and moneypuck.com.
"""
import csv
import io
import json
import os
import datetime as dt
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (LineLogic NHLBuilder)"}
_API = "https://api.nhle.com/stats/rest/en"
_MP = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/skaters.csv"


def _seasons():
    t = dt.date.today()
    cur = f"{t.year}{t.year+1}" if t.month >= 9 else f"{t.year-1}{t.year}"
    prev = f"{int(cur[:4])-1}{int(cur[4:])-1}"
    return cur, prev


def _get_json(url):
    local = os.environ.get("LOCAL_NHL")
    if local:
        path = url.split("?")[0]
        parts = path.split("/en/")
        base = parts[-1].replace("/", "_") if len(parts) > 1 else path.rstrip("/").split("/")[-1]
        p = os.path.join(local, f"{base}.json")
        if os.path.exists(p):
            try:
                return json.load(open(p))
            except Exception:
                return None
        return None
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[nhl] json {url[:70]}: {e}")
        return None


def _api_all(entity, season):
    """Page through api.nhle.com /{entity}/summary for a full season."""
    rows, start = [], 0
    while True:
        url = (f"{_API}/{entity}/summary?isAggregate=false&isGame=false&start={start}"
               f"&limit=100&cayenneExp=seasonId={season}%20and%20gameTypeId=2")
        d = _get_json(url)
        data = (d or {}).get("data") or []
        rows.extend(data)
        if len(data) < 100 or os.environ.get("LOCAL_NHL"):
            break
        start += 100
        if start > 2000:
            break
    return rows


def _get_csv(url):
    local = os.environ.get("LOCAL_NHL")
    if local:
        p = os.path.join(local, "skaters.csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return list(csv.DictReader(f))
        return []
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            return list(csv.DictReader(io.StringIO(r.read().decode("utf-8", "replace"))))
    except Exception as e:
        print(f"[nhl] csv: {e}")
        return []


def _f(d, k):
    v = d.get(k)
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(season=None, out_path="nhl_profiles.json"):
    cur, prev = _seasons()
    season = season or os.environ.get("NHL_SEASON") or cur
    skaters = _api_all("skater", season)
    # early-season: if the current season is too thin, fall back to last completed
    if not os.environ.get("LOCAL_NHL"):
        maxgp = max((s.get("gamesPlayed") or 0) for s in skaters) if skaters else 0
        if maxgp < 15 and season == cur:
            print(f"[nhl] {season} thin (max GP {maxgp}) — using {prev}")
            season = prev
            skaters = _api_all("skater", season)
    goalies = _api_all("goalie", season)
    mp_year = season[:4]
    mp = _get_csv(_MP.format(yr=mp_year))
    mp_all = {}
    for r in mp:
        if (r.get("situation") or "") != "all":
            continue
        pid = (r.get("playerId") or "").strip()
        if pid:
            mp_all[pid] = r

    players = {}
    for s in skaters:
        pid = str(s.get("playerId") or "")
        gp = s.get("gamesPlayed") or 0
        if not pid or gp < 1:
            continue
        toi = _f(s, "timeOnIcePerGame") or 0      # seconds/game
        mpr = mp_all.get(pid, {})
        ic = _f(mpr, "icetime") or (toi * gp)      # total seconds
        per60 = (3600.0 / ic) if ic else 0.0
        xg = _f(mpr, "I_F_flurryScoreVenueAdjustedxGoals")
        if xg is None:
            xg = _f(mpr, "I_F_xGoals")
        icf = _f(mpr, "I_F_shotAttempts")          # individual Corsi (shot attempts)
        goals = s.get("goals") or 0
        players[pid] = {
            "name": s.get("skaterFullName"), "pos": s.get("positionCode"),
            "team": (s.get("teamAbbrevs") or "").split(",")[-1].strip(), "gp": gp,
            "toi_pg": round(toi / 60.0, 1),         # minutes/game
            "goals_pg": round(goals / gp, 3), "assists_pg": round((s.get("assists") or 0) / gp, 3),
            "points_pg": round((s.get("points") or 0) / gp, 3),
            "shots_pg": round((s.get("shots") or 0) / gp, 2),
            "shooting_pct": round((_f(s, "shootingPct") or 0) * 100, 1),
            "pp_points_pg": round((s.get("ppPoints") or 0) / gp, 3),
            # advanced (MoneyPuck)
            "xg_total": round(xg, 2) if xg is not None else None,
            "xg_pg": round(xg / gp, 3) if xg is not None else None,
            "xg_per60": round(xg * per60, 3) if xg is not None else None,
            "icf_per60": round(icf * per60, 2) if icf is not None else None,
            "corsi_pct": _f(mpr, "onIce_corsiPercentage"),
            "shot_talent": round(goals - xg, 2) if xg is not None else None,
            "game_score_pg": round((_f(mpr, "gameScore") or 0) / gp, 2) if mpr else None,
        }

    # goalies + team defense funnel (from goalie summary)
    goal_prof, team_def = {}, {}
    for g in goalies:
        pid = str(g.get("playerId") or "")
        gp = g.get("gamesPlayed") or 0
        team = (g.get("teamAbbrevs") or "").split(",")[-1].strip()
        sa = g.get("shotsAgainst") or 0
        ga = g.get("goalsAgainst") or 0
        gs = g.get("gamesStarted") or 0
        if pid and gp >= 1:
            goal_prof[pid] = {
                "name": g.get("goalieFullName"), "team": team, "gp": gp, "gs": gs,
                "save_pct": round((g.get("savePct") or 0) * 100, 2),
                "gaa": round(g.get("goalsAgainstAverage") or 0, 2),
                "saves_pg": round((g.get("saves") or 0) / gp, 1),
                "sa_pg": round(sa / gp, 1), "shutouts": g.get("shutouts") or 0,
            }
        if team:
            t = team_def.setdefault(team, {"gs": 0, "sa": 0.0, "ga": 0.0, "goalies": []})
            t["gs"] += gs
            t["sa"] += sa
            t["ga"] += ga
            t["goalies"].append((gs, pid))

    # team offense (shots/goals FOR) aggregated from skaters — for goalie matchups
    team_off = {}
    for s in skaters:
        team = (s.get("teamAbbrevs") or "").split(",")[-1].strip()
        if not team:
            continue
        o = team_off.setdefault(team, {"shots": 0.0, "goals": 0.0})
        o["shots"] += s.get("shots") or 0
        o["goals"] += s.get("goals") or 0

    defense = {}
    for team, t in team_def.items():
        tg = max(1, t["gs"])
        starter = max(t["goalies"], key=lambda x: x[0])[1] if t["goalies"] else None
        off = team_off.get(team, {})
        defense[team] = {
            "games": t["gs"], "shots_against_pg": round(t["sa"] / tg, 1),
            "goals_against_pg": round(t["ga"] / tg, 2),
            "team_save_pct": round(100 * (1 - t["ga"] / t["sa"]), 2) if t["sa"] else None,
            "shots_for_pg": round(off.get("shots", 0) / tg, 1),
            "goals_for_pg": round(off.get("goals", 0) / tg, 2),
            "starter_id": starter,
        }

    out = {"generated": dt.datetime.utcnow().isoformat() + "Z", "season": season,
           "players": players, "goalies": goal_prof, "defense": defense}
    print(f"[nhl] {season}: {len(players)} skaters, {len(goal_prof)} goalies, "
          f"{len(defense)} defenses (mp xG for {sum(1 for p in players.values() if p.get('xg_pg') is not None)})")
    if players:
        with open(out_path, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        print(f"[nhl] wrote {out_path} ({os.path.getsize(out_path)} bytes)")
    else:
        print("[nhl] refusing to write empty profile")
    return out


if __name__ == "__main__":
    build(out_path=os.environ.get("NHL_OUT", "nhl_profiles.json"))
