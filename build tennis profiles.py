"""
build_tennis_profiles.py — the free data layer behind the tennis analysis page.

Reads Jeff Sackmann's match CSVs (ATP + WTA, the same free data the ratings model
already uses) and rolls every match up into one profile per player plus a court-pace
table per tournament. Writes tennis_profiles.json, which the site serves to the
analysis page and feeds to tennis_markets.py for the model lines.

Everything the page shows comes from columns Sackmann publishes per match:
  w_ace w_df w_svpt w_1stIn w_1stWon w_2ndWon w_SvGms w_bpSaved w_bpFaced  (winner serve)
  l_ace ...                                                                (loser serve)
plus score, surface, round, rank, age.

Two facts make the derived rates exact rather than guessed:
  * Serve points won % = (1stWon + 2ndWon) / svpt.
  * Games broken = bpFaced - bpSaved EXACTLY: a held game saves all its break
    points, and a broken game loses on exactly one (the game ends the moment a
    break point is converted). So Hold% = (SvGms - (bpFaced - bpSaved)) / SvGms,
    and a returner's Break% = (oppBpFaced - oppBpSaved) / oppSvGms.

A player's RETURN numbers come from the OPPONENT's serve row in each match, so
serve and return are both measured, which is exactly what the market engine needs.

Run (on Railway or a GitHub Action, which have open internet):
    python build_tennis_profiles.py            # last 4 yrs recent + career totals
Design note: the aggregation (aggregate()) is a pure function over row dicts, so it
is unit-tested on synthetic rows without any network.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request

# Sackmann's CSVs, fetched over plain HTTPS (no git clone). raw.githubusercontent
# is refused on some hosts (returns 404), so try the jsDelivr CDN mirror FIRST — it
# serves the same GitHub files and is rarely blocked — then fall back to raw. Also
# try both branches in case of a rename. The first working (source, branch) is
# remembered so the rest of the build is fast.
_SOURCES = [
    "https://cdn.jsdelivr.net/gh/JeffSackmann/tennis_{tour}@{branch}/{stem}_{yr}.csv",
    "https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}/{branch}/{stem}_{yr}.csv",
]
_BRANCHES = ["master", "main"]
_good = None   # (source_template, branch) once one works
SURFACES = ("Hard", "Clay", "Grass")

# Which Sackmann files to pull per tour. Main draw PLUS the qualifying/challenger
# file so lower-ranked players (qualifiers, Challenger regulars) also get profiles
# instead of showing blank on the analysis page. Each is fetched over plain HTTPS
# (raw.githubusercontent) — no git clone, so GitHub's auth change doesn't apply.
_FILE_STEMS = {
    "atp": ["atp_matches", "atp_matches_qual_chall"],
    "wta": ["wta_matches", "wta_matches_qual_itf"],
}

# Court Pace Index calibration (anchored to the Hawk-Eye majors the user supplied).
# We measure a tournament's ace rate vs the surface average and map the ratio onto
# the CPI scale. base = CPI at ratio 1.0 (surface average); slope = CPI points per
# +100% ace ratio. Anchors: AO~42, USO~35 (hard); Wimbledon~37 (grass); RG~21 (clay).
_CPI_BASE = {"Hard": 37.0, "Clay": 24.0, "Grass": 37.0}
_CPI_SLOPE = {"Hard": 16.0, "Clay": 14.0, "Grass": 16.0}


def _strip(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def name_key(full_name: str):
    """last name + first initial, accent/hyphen-insensitive — matches the ratings model."""
    if not full_name:
        return None
    name = _strip(full_name.strip().lower()).replace("-", " ").replace(".", ". ")
    name = re.sub(r"\s+", " ", name).strip()
    m = re.match(r"^([a-z])\.\s+(.+)$", name)
    if m:
        initial, last = m.group(1), m.group(2).split()[-1]
    else:
        parts = name.split()
        if len(parts) < 2:
            return None
        initial, last = parts[0][0], parts[-1]
    return f"{last}|{initial}"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def first_set_won(score: str, player_is_winner: bool):
    """Did the player win the FIRST set? Sackmann scores are written winner-first,
    e.g. '6-4 3-6 7-5' = winner won set 1 6-4, lost set 2. Returns True/False/None."""
    if not score:
        return None
    tok = score.strip().split()
    if not tok:
        return None
    m = re.match(r"(\d+)-(\d+)", tok[0])
    if not m:
        return None
    wg, lg = int(m.group(1)), int(m.group(2))
    if wg == lg:
        return None
    winner_won_set1 = wg > lg
    return winner_won_set1 if player_is_winner else (not winner_won_set1)


def _blank_serve():
    return dict(svpt=0, firstIn=0, firstWon=0, secondWon=0, aces=0, df=0,
               svgms=0, bpFaced=0, bpSaved=0, matches=0)


def _blank_ret():
    return dict(svpt=0, firstIn=0, firstWon=0, secondWon=0, svgms=0, bpFaced=0, bpSaved=0)


def _new_player():
    return {
        "name": None, "ioc": None, "hand": None, "last_age": None,
        "last_rank": None, "last_date": None, "career_high": None,
        "wins": 0, "losses": 0,
        "by_year": {}, "surf_rec": {}, "titles": 0,
        "serve": _blank_serve(), "serve_surf": {},
        "ret": _blank_ret(), "ret_surf": {},
        "results": [],         # (date_int, 'W'/'L')
        "first_sets": [],
        "minutes": [],         # (date_int, minutes) for energy/fatigue
    }


def _ddiff(a_int, b_int):
    """Whole days between two YYYYMMDD integer dates (abs)."""
    try:
        da = dt.date(a_int // 10000, (a_int // 100) % 100, a_int % 100)
        db = dt.date(b_int // 10000, (b_int // 100) % 100, b_int % 100)
        return abs((da - db).days)
    except Exception:
        return 9999


def _add_serve(acc, row, pre):
    acc["matches"] += 1
    for dst, col in (("svpt", "svpt"), ("firstIn", "1stIn"), ("firstWon", "1stWon"),
                     ("secondWon", "2ndWon"), ("aces", "ace"), ("df", "df"),
                     ("svgms", "SvGms"), ("bpFaced", "bpFaced"), ("bpSaved", "bpSaved")):
        v = _i(row.get(f"{pre}_{col}"))
        if v is not None:
            acc[dst] += v


def _add_ret(acc, row, opp_pre):
    for dst, col in (("svpt", "svpt"), ("firstIn", "1stIn"), ("firstWon", "1stWon"),
                     ("secondWon", "2ndWon"), ("svgms", "SvGms"),
                     ("bpFaced", "bpFaced"), ("bpSaved", "bpSaved")):
        v = _i(row.get(f"{opp_pre}_{col}"))
        if v is not None:
            acc[dst] += v


def aggregate(rows):
    """Pure: fold an iterable of Sackmann row-dicts into (players, tournaments).
    No network — unit-tested directly on synthetic rows."""
    players: dict = {}
    tourneys: dict = {}
    h2h: dict = {}     # "keyA|keyB" (sorted) -> {key: wins}

    for row in rows:
        surface = (row.get("surface") or "").strip().title()
        if surface not in SURFACES:
            surface = None
        date = _i(row.get("tourney_date")) or 0
        yr = str(date)[:4] if date else "?"
        rnd = (row.get("round") or "").strip().upper()
        mins = _i(row.get("minutes"))

        # pairwise head-to-head (one entry per match, keyed by the sorted pair)
        kw, kl = name_key(row.get("winner_name") or ""), name_key(row.get("loser_name") or "")
        if kw and kl and kw != kl:
            pk = "~~".join(sorted([kw, kl]))   # keys contain '|', so pair uses '~~'
            rec = h2h.setdefault(pk, {})
            rec[kw] = rec.get(kw, 0) + 1

        for who, opp, win in (("winner", "loser", True), ("loser", "winner", False)):
            name = row.get(f"{who}_name")
            k = name_key(name or "")
            if not k:
                continue
            P = players.setdefault(k, _new_player())
            # identity (latest by date wins)
            if date >= (P["last_date"] or 0):
                P["last_date"] = date
                P["name"] = name
                P["ioc"] = row.get(f"{who}_ioc") or P["ioc"]
                P["hand"] = row.get(f"{who}_hand") or P["hand"]
                age = _f(row.get(f"{who}_age"))
                if age:
                    P["last_age"] = age
                rk = _i(row.get(f"{who}_rank"))
                if rk:
                    P["last_rank"] = rk
            rk = _i(row.get(f"{who}_rank"))
            if rk:
                P["career_high"] = rk if P["career_high"] is None else min(P["career_high"], rk)
            # records
            if win:
                P["wins"] += 1
            else:
                P["losses"] += 1
            yrec = P["by_year"].setdefault(yr, [0, 0])
            yrec[0 if win else 1] += 1
            if surface:
                srec = P["surf_rec"].setdefault(surface, [0, 0])
                srec[0 if win else 1] += 1
            if win and rnd == "F":
                P["titles"] += 1
            P["results"].append((date, "W" if win else "L"))
            fs = first_set_won(row.get("score", ""), win)
            if fs is not None:
                P["first_sets"].append(1 if fs else 0)
            # serve counters (this player's own serve row)
            pre = "w" if win else "l"
            _add_serve(P["serve"], row, pre)
            if surface:
                _add_serve(P["serve_surf"].setdefault(surface, _blank_serve()), row, pre)
            # return counters (the OPPONENT's serve row)
            opp_pre = "l" if win else "w"
            _add_ret(P["ret"], row, opp_pre)
            if surface:
                _add_ret(P["ret_surf"].setdefault(surface, _blank_ret()), row, opp_pre)
            if mins:
                P["minutes"].append((date, mins))

        # tournament court-pace accumulation (total aces / total serve points)
        tname = (row.get("tourney_name") or "").strip()
        if tname and surface:
            tk = tname.lower()
            T = tourneys.setdefault(tk, {"name": tname, "surface": surface, "aces": 0, "svpt": 0})
            for pre in ("w", "l"):
                a, s = _i(row.get(f"{pre}_ace")), _i(row.get(f"{pre}_svpt"))
                if a is not None:
                    T["aces"] += a
                if s is not None:
                    T["svpt"] += s

    return players, tourneys, h2h


def _rate(num, den):
    return round(num / den, 4) if den else None


def finalize_player(P: dict) -> dict:
    s, r = P["serve"], P["ret"]
    def serve_block(s):
        svpt = s["svpt"]
        second = svpt - s["firstIn"]
        return {
            "spw": _rate(s["firstWon"] + s["secondWon"], svpt),
            "first_in": _rate(s["firstIn"], svpt),
            "first_win": _rate(s["firstWon"], s["firstIn"]),
            "second_win": _rate(s["secondWon"], second),
            "ace_pm": _rate(s["aces"], s["matches"]),
            "df_pm": _rate(s["df"], s["matches"]),
            "hold": _rate(s["svgms"] - (s["bpFaced"] - s["bpSaved"]), s["svgms"]),
        }
    def ret_block(r):
        svpt = r["svpt"]
        second = svpt - r["firstIn"]
        breaks = r["bpFaced"] - r["bpSaved"]
        return {
            "rpw": _rate(svpt - r["firstWon"] - r["secondWon"], svpt),
            "ret1": _rate(r["firstIn"] - r["firstWon"], r["firstIn"]),
            "ret2": _rate(second - r["secondWon"], second),
            "brk": _rate(breaks, r["svgms"]),
            "bp_conv": _rate(breaks, r["bpFaced"]),
        }

    # recent form from chronologically-sorted results
    res = sorted(P["results"])
    seq = [x[1] for x in res]
    last5 = seq[-5:]
    last10 = seq[-10:]
    streak = 0
    for v in reversed(seq):
        if v == "W":
            streak += 1
        else:
            break
    overall = serve_block(s); overall.update(ret_block(r))

    # Energy / freshness: recent match load in the 14 days up to the last match.
    # More minutes on court recently = more fatigue = lower energy. A quiet fortnight
    # reads fresh. Scaled so a heavy week (~3 long matches) lands mid-pack.
    last = P["last_date"] or 0
    load = sum(mm for (dd, mm) in P["minutes"] if last and _ddiff(last, dd) <= 14)
    energy = max(20, min(100, round(100 - load / 9.0)))

    by_surface = {}
    for surf in SURFACES:
        ss, rr = P["serve_surf"].get(surf), P["ret_surf"].get(surf)
        rec = P["surf_rec"].get(surf, [0, 0])
        blk = {"w": rec[0], "l": rec[1]}
        if ss:
            blk.update(serve_block(ss))
        if rr:
            blk.update(ret_block(rr))
        by_surface[surf] = blk

    return {
        "name": P["name"], "ioc": P["ioc"], "hand": P["hand"],
        "age": round(P["last_age"], 1) if P["last_age"] else None,
        "rank": P["last_rank"], "career_high": P["career_high"],
        "career": {"w": P["wins"], "l": P["losses"]},
        "by_year": {y: {"w": wl[0], "l": wl[1]} for y, wl in sorted(P["by_year"].items(), reverse=True)},
        "surface": by_surface,
        "overall": overall,
        "titles": P["titles"],
        "form5": _rate(last5.count("W"), len(last5)),
        "form10": _rate(last10.count("W"), len(last10)),
        "streak": streak,
        "last5": last5,
        "first_set_pct": _rate(sum(P["first_sets"]), len(P["first_sets"])),
        "matches": P["wins"] + P["losses"],
        "energy": energy,
    }


def finalize_tournaments(tourneys: dict) -> dict:
    # surface mean ace rate across tournaments, then each event's ratio -> CPI estimate
    surf_rates = {}
    for T in tourneys.values():
        if T["svpt"]:
            surf_rates.setdefault(T["surface"], []).append(T["aces"] / T["svpt"])
    surf_mean = {s: (sum(v) / len(v)) for s, v in surf_rates.items() if v}
    out = {}
    for tk, T in tourneys.items():
        if not T["svpt"]:
            continue
        ace_pct = T["aces"] / T["svpt"]
        mean = surf_mean.get(T["surface"]) or ace_pct
        ratio = ace_pct / mean if mean else 1.0
        base = _CPI_BASE.get(T["surface"], 35.0)
        slope = _CPI_SLOPE.get(T["surface"], 15.0)
        cpi = base + slope * (ratio - 1.0)
        cat = (1 if cpi <= 29 else 2 if cpi < 35 else 3 if cpi < 40 else 4 if cpi < 45 else 5)
        out[tk] = {
            "name": T["name"], "surface": T["surface"],
            "ace_pct": round(ace_pct, 4), "speed_ratio": round(ratio, 3),
            "cpi": round(cpi, 1), "category": cat,
        }
    return out


# ------------------------------------------------------------------ fetch + main
def _fetch(tour: str, stem: str, yr: int):
    """Fetch one Sackmann CSV, trying CDN mirror + raw across both branches.
    Returns [] on 404/any error (not every stem exists for every year)."""
    global _good
    combos = [_good] if _good else [(s, b) for s in _SOURCES for b in _BRANCHES]
    for (src, br) in combos:
        url = src.format(tour=tour, branch=br, stem=stem, yr=yr)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LineLogic/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("utf-8", "replace")
            _good = (src, br)
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue          # not here — try the next source/branch
            print(f"[profiles] {stem}_{yr}: HTTP {e.code} ({url})")
            continue
        except Exception as e:
            print(f"[profiles] {stem}_{yr}: {e} ({url})")
            continue
    return []


def build(years_recent=4, out_path="tennis_profiles.json", tours=("atp", "wta")):
    this_year = dt.date.today().year
    years = range(this_year - years_recent + 1, this_year + 1)
    all_players, all_tourneys = {}, {}
    out = {"generated": dt.datetime.utcnow().isoformat() + "Z", "tours": {}}
    for tour in tours:
        rows = []
        for stem in _FILE_STEMS.get(tour, [f"{tour}_matches"]):
            for yr in years:
                rows.extend(_fetch(tour, stem, yr))
        players, tourneys, h2h = aggregate(rows)
        fin = {k: finalize_player(P) for k, P in players.items() if (P["wins"] + P["losses"]) >= 3}
        # keep only head-to-heads where both players survived the min-match filter
        h2h = {pk: rec for pk, rec in h2h.items() if all(k in fin for k in pk.split("~~"))}
        out["tours"][tour] = {
            "players": fin,
            "tournaments": finalize_tournaments(tourneys),
            "h2h": h2h,
        }
        print(f"[profiles] {tour}: {len(fin)} players, {len(tourneys)} tournaments, "
              f"{len(h2h)} head-to-heads from {len(rows)} matches")
    # Never clobber an existing good file with an empty build (e.g. when the host
    # can't reach the data). If nothing was fetched, leave the current file alone.
    total_players = sum(len(t.get("players", {})) for t in out["tours"].values())
    if total_players == 0:
        print("[profiles] 0 players fetched — NOT writing (data source unreachable; "
              "existing file left intact). Build this via the GitHub Action instead.")
        return out
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"[profiles] wrote {out_path} ({total_players} players)")
    return out


if __name__ == "__main__":
    import os
    build(years_recent=int(os.environ.get("PROFILE_YEARS", "4")),
          out_path=os.environ.get("PROFILES_FILE", "tennis_profiles.json"))
