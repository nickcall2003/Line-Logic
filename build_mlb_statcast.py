#!/usr/bin/env python3
"""
Build mlb_statcast.json — the Statcast engine behind the Venom-style MLB props.

Pulls three Baseball Savant leaderboards (all free, CSV):
  1. batter expected stats     -> xBA / xSLG / xwOBA (expected outcomes)
  2. batter exit-velo / batted -> exit velo, barrel%, hard-hit%, FB/LD%, GB%, etc.
  3. pitcher pitch-arsenal      -> every pitcher's pitch mix + per-pitch results
     (usage%, whiff%, K%, xwOBA-against, hard-hit%-against, run value)

Merged by MLBAM player_id into:
  { "generated", "year",
    "batters":  { "<id>": {name, pa, bip, ba, xba, slg, xslg, woba, xwoba,
                           exit_velo, max_ev, barrel_pct, hard_hit_pct,
                           fb_ld_pct, gb_pct, sweet_spot_pct, avg_hr_dist, ev95plus} },
    "pitchers": { "<id>": {name, team, pitches, arsenal:[{pitch,code,usage,whiff,
                           k_pct,ba,slg,woba,xwoba,hard_hit_pct,rv100}],
                           k_pct, whiff_pct, xwoba, hard_hit_pct} } }   # usage-weighted summary

Runs on a host that can reach baseballsavant.mlb.com (Railway can; the probe proved it).
Env: STATCAST_YEAR (default: current year), STATCAST_MIN (batter min PA, default 1),
     STATCAST_OUT (default mlb_statcast.json), LOCAL_STATCAST (dir of pre-saved CSVs).
"""
import csv
import io
import json
import os
import datetime as dt
import urllib.request
import urllib.error

_UA = {"User-Agent": "Mozilla/5.0 (LineLogic StatcastBuilder)"}


def _savant_url(kind, year, bmin):
    if kind == "batter_expected":
        return (f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
                f"?type=batter&year={year}&position=&team=&min={bmin}&csv=true")
    if kind == "batter_statcast":
        return (f"https://baseballsavant.mlb.com/leaderboard/statcast"
                f"?type=batter&year={year}&min={bmin}&csv=true")
    if kind == "pitch_arsenal":
        return (f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
                f"?type=pitcher&pitchType=&year={year}&team=&min=50&csv=true")
    raise ValueError(kind)


def _fetch_csv(kind, year, bmin):
    """Return list[dict]. Prefers LOCAL_STATCAST/<kind>_<year>.csv for offline builds."""
    local = os.environ.get("LOCAL_STATCAST")
    if local:
        path = os.path.join(local, f"{kind}_{year}.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                return list(csv.DictReader(f))
        return []
    url = _savant_url(kind, year, bmin)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8-sig", "replace")
        return list(csv.DictReader(io.StringIO(text)))
    except urllib.error.HTTPError as e:
        print(f"[statcast] {kind}: HTTP {e.code}")
    except Exception as e:
        print(f"[statcast] {kind}: {e}")
    return []


def _f(row, *keys):
    """First present, non-empty, numeric value among keys."""
    for k in keys:
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _name(row):
    raw = (row.get("last_name, first_name") or row.get("last_name,first_name")
           or row.get(" last_name, first_name") or "").strip()
    if "," in raw:
        last, first = [p.strip() for p in raw.split(",", 1)]
        return f"{first} {last}".strip()
    return raw


def _pid(row):
    v = (row.get("player_id") or row.get("playerid") or "").strip()
    return v or None


def build_batters(year, bmin):
    batters = {}
    # expected stats (xBA/xSLG/xwOBA)
    for r in _fetch_csv("batter_expected", year, bmin):
        pid = _pid(r)
        if not pid:
            continue
        batters[pid] = {
            "name": _name(r), "pa": _f(r, "pa"), "bip": _f(r, "bip"),
            "ba": _f(r, "ba"), "xba": _f(r, "est_ba"),
            "slg": _f(r, "slg"), "xslg": _f(r, "est_slg"),
            "woba": _f(r, "woba"), "xwoba": _f(r, "est_woba"),
        }
    # exit velo / batted ball
    for r in _fetch_csv("batter_statcast", year, bmin):
        pid = _pid(r)
        if not pid:
            continue
        b = batters.setdefault(pid, {"name": _name(r)})
        b.update({
            "exit_velo": _f(r, "avg_hit_speed", "exit_velocity_avg"),
            "max_ev": _f(r, "max_hit_speed", "max_exit_velocity"),
            "barrel_pct": _f(r, "brl_percent", "barrel_batted_rate"),
            "barrel_pa": _f(r, "brl_pa"),
            "hard_hit_pct": _f(r, "ev95percent", "hard_hit_percent"),
            "ev95plus": _f(r, "ev95plus"),
            "fb_ld_pct": _f(r, "fbld"),
            "gb_pct": _f(r, "gb"),
            "sweet_spot_pct": _f(r, "anglesweetspotpercent", "sweet_spot_percent"),
            "avg_hr_dist": _f(r, "avg_hr_distance"),
            "bbe": _f(r, "attempts"),
        })
        if not b.get("name"):
            b["name"] = _name(r)
    # drop empties
    return {k: v for k, v in batters.items() if v.get("name")}


# pitch_type code -> short label (fallback; CSV usually carries pitch_name)
_PITCH = {"FF": "4-Seam Fastball", "SI": "Sinker", "FC": "Cutter", "SL": "Slider",
          "CH": "Changeup", "CU": "Curveball", "KC": "Knuckle Curve", "FS": "Splitter",
          "ST": "Sweeper", "SV": "Slurve", "FO": "Forkball", "KN": "Knuckleball",
          "EP": "Eephus", "SC": "Screwball"}


def build_pitchers(year):
    pitchers = {}
    for r in _fetch_csv("pitch_arsenal", year, 0):
        pid = _pid(r)
        if not pid:
            continue
        p = pitchers.setdefault(pid, {
            "name": _name(r), "team": (r.get("team_name_alt") or "").strip(),
            "arsenal": [],
        })
        code = (r.get("pitch_type") or "").strip()
        p["arsenal"].append({
            "pitch": (r.get("pitch_name") or _PITCH.get(code, code)).strip(),
            "code": code,
            "usage": _f(r, "pitch_usage"),
            "pitches": _f(r, "pitches"),
            "whiff": _f(r, "whiff_percent"),
            "k_pct": _f(r, "k_percent"),
            "ba": _f(r, "ba"), "slg": _f(r, "slg"), "woba": _f(r, "woba"),
            "xwoba": _f(r, "est_woba"),
            "hard_hit_pct": _f(r, "hard_hit_percent"),
            "rv100": _f(r, "run_value_per_100"),
        })
    # usage-weighted pitcher summary + sort arsenal by usage
    for p in pitchers.values():
        ars = [a for a in p["arsenal"] if a.get("usage")]
        tot = sum(a["usage"] for a in ars) or 1.0
        def wavg(key):
            vals = [(a["usage"], a[key]) for a in ars if a.get(key) is not None]
            w = sum(u for u, _ in vals)
            return round(sum(u * v for u, v in vals) / w, 2) if w else None
        p["k_pct"] = wavg("k_pct")
        p["whiff_pct"] = wavg("whiff")
        p["xwoba"] = wavg("xwoba")
        p["hard_hit_pct"] = wavg("hard_hit_pct")
        p["pitches"] = int(sum(a.get("pitches") or 0 for a in p["arsenal"]))
        p["arsenal"].sort(key=lambda a: a.get("usage") or 0, reverse=True)
    return {k: v for k, v in pitchers.items() if v.get("name")}


def _fetch_json(url):
    local = os.environ.get("LOCAL_STATCAST")
    if local:
        import glob
        # offline: look for a saved statsapi_<year>.json
        for p in glob.glob(os.path.join(local, "statsapi_*.json")):
            try:
                return json.load(open(p))
            except Exception:
                pass
        return None
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[statcast] statsapi: {e}")
        return None


def merge_traditional(batters, year):
    """Add season counting stats (HR, AVG, RBI, R, SB, OPS) from the free MLB Stats
    API, keyed by the same MLBAM player_id Statcast uses."""
    url = (f"https://statsapi.mlb.com/api/v1/stats?stats=season&group=hitting"
           f"&season={year}&gameType=R&playerPool=All&limit=3000")
    data = _fetch_json(url)
    if not data:
        return 0
    n = 0
    for blk in data.get("stats", []):
        for sp in blk.get("splits", []):
            pid = str((sp.get("player") or {}).get("id") or "")
            st = sp.get("stat") or {}
            if not pid:
                continue
            b = batters.get(pid)
            if b is None:
                continue   # only annotate batters we already have Statcast for
            def _n(k):
                v = st.get(k)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            b["home_runs_season"] = _n("homeRuns")
            b["avg_season"] = _n("avg")
            b["rbi_season"] = _n("rbi")
            b["runs_season"] = _n("runs")
            b["sb_season"] = _n("stolenBases")
            b["ops_season"] = _n("ops")
            b["ab_season"] = _n("atBats")
            n += 1
    return n


def build(year=None, out_path="mlb_statcast.json", bmin=None):
    year = int(year or os.environ.get("STATCAST_YEAR") or dt.date.today().year)
    bmin = bmin if bmin is not None else int(os.environ.get("STATCAST_MIN") or 1)
    batters = build_batters(year, bmin)
    pitchers = build_pitchers(year)
    # if the season just rolled over and nothing's posted yet, fall back a year
    if (len(batters) < 20 or len(pitchers) < 20) and not os.environ.get("LOCAL_STATCAST"):
        py = year - 1
        print(f"[statcast] {year} looks empty ({len(batters)}b/{len(pitchers)}p) — trying {py}")
        b2, p2 = build_batters(py, bmin), build_pitchers(py)
        if len(b2) > len(batters):
            batters, pitchers, year = b2, p2, py
    nt = merge_traditional(batters, year)
    print(f"[statcast] merged traditional stats for {nt} batters")
    out = {"generated": dt.datetime.utcnow().isoformat() + "Z", "year": year,
           "batters": batters, "pitchers": pitchers}
    print(f"[statcast] {year}: {len(batters)} batters, {len(pitchers)} pitchers")
    if batters and pitchers:
        with open(out_path, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        print(f"[statcast] wrote {out_path} ({os.path.getsize(out_path)} bytes)")
    else:
        print("[statcast] refusing to write an empty profile")
    return out


if __name__ == "__main__":
    build(out_path=os.environ.get("STATCAST_OUT", "mlb_statcast.json"))
