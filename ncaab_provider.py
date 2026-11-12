"""
ncaab_provider.py
-----------------
File-backed adjusted-efficiency lookup + win-probability model for men's NCAA
basketball, fed by ncaab_ratings.json (refresh_cbbd_ratings.py). No network on
the request path.

Model (standard Pythagorean efficiency):
    league avg efficiency = avg_eff (points per 100 possessions)
    home_eff = AdjO_home * AdjD_away / avg_eff   (expected pts/100 for home)
    away_eff = AdjO_away * AdjD_home / avg_eff
    margin   = (home_eff - away_eff) * tempo/100 + HOME_COURT
    total    = (home_eff + away_eff) * tempo/100
    P(home)  = normal CDF of margin / MARGIN_SD   (CBB margin SD ~ 11 pts)

Returns None (caller falls back to win%-Elo) if the file is missing or a team
can't be matched.
"""
from __future__ import annotations
import os
import json
import math
import unicodedata

_PATH = (os.environ.get("NCAAB_RATINGS_PATH")
         or ("/data/ncaab_ratings.json" if os.path.exists("/data/ncaab_ratings.json") else "ncaab_ratings.json"))
HOME_COURT = float(os.environ.get("NCAAB_HOME_COURT", "3.5"))
MARGIN_SD = float(os.environ.get("NCAAB_MARGIN_SD", "11.0"))
_data = None
_logged_miss = set()

# norm(espn location) -> norm(cbbd team), seeded with common men's-CBB oddities.
_ALIASES = {
    "connecticut": "uconn", "uconn": "connecticut",
    "saintmarys": "stmarys", "stmarys": "saintmarys",
    "stjohns": "saintjohns", "saintjohns": "stjohns",
    "ole miss".replace(" ", ""): "mississippi", "mississippi": "olemiss",
    "nccentral": "northcarolinacentral",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _load():
    global _data
    if _data is None:
        try:
            with open(_PATH) as f:
                _data = json.load(f)
        except Exception:
            _data = {"teams": {}, "avg_eff": 104.0, "tempo": 68.0}
    return _data


def reload():
    global _data
    _data = None
    return _load()


def _lookup(name):
    d = _load()
    teams = d.get("teams") or {}
    if not teams:
        return None
    n = _norm(name)
    if n in teams:
        return teams[n]
    for ak, av in _ALIASES.items():
        if (n == ak or n.startswith(ak)) and av in teams:
            return teams[av]
    best = None
    for k, v in teams.items():
        if k.startswith(n):
            pass                        # ESPN short name is a prefix of the fuller
                                        # ratings name (miami -> miamifl): safe.
        elif n.startswith(k) and len(k) / len(n) >= 0.85:
            pass                        # ratings key is a prefix of a LONGER name;
                                        # only trust it when it covers most of the
                                        # name. Blocks "virginialynchburg" (non-D1)
                                        # from matching "virginia" (UVA).
        else:
            continue
        if best is None or len(k) > len(best[0]):
            best = (k, v)
    return best[1] if best else None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def predict(home_name, away_name):
    d = _load()
    h = _lookup(home_name)
    a = _lookup(away_name)
    # D1-vs-non-D1 mismatch. Our ratings cover D1 only, so a team that doesn't
    # match is a non-D1 (buy-game) opponent. Never fall through to win%-seeding
    # for these: it favored tiny schools carrying gaudy records against OTHER
    # tiny schools (a 2-0 NAIA side shown as a 96% favorite over a D1 team).
    # Default the D1 side to a decisive win so the weaker team is never favored.
    if bool(h) != bool(a):
        miss = away_name if h else home_name
        if miss not in _logged_miss:
            _logged_miss.add(miss)
            print(f"[ncaab] non-D1 opponent -> default D1 win: {miss!r}")
        d1 = h or a
        try:
            net = float(d1.get("net", 0.0) or 0.0)
        except Exception:
            net = 0.0
        base = 18.0 + max(0.0, min(14.0, net * 0.5))   # stronger D1 -> bigger blowout (18..32)
        margin = (base + HOME_COURT) if h else (-base + HOME_COURT)
        return {
            "prob_home": 0.97 if h else 0.03,
            "exp_margin": round(margin, 1),
            "home_rating": round(h.get("net", 0.0), 1) if h else None,
            "away_rating": round(a.get("net", 0.0), 1) if a else None,
            "confidence": "low",
            "avg_total": None,
            "model": "default-d1",
            "mismatch": True,
        }
    if not h or not a:        # neither side is D1 -> can't rank them, stay neutral
        return None
    avg = d.get("avg_eff") or 104.0
    tempo = d.get("tempo") or 68.0
    try:
        home_eff = h["off"] * a["def"] / avg
        away_eff = a["off"] * h["def"] / avg
    except Exception:
        return None
    margin = (home_eff - away_eff) * tempo / 100.0 + HOME_COURT
    total = (home_eff + away_eff) * tempo / 100.0
    prob_home = max(0.02, min(0.98, _norm_cdf(margin / MARGIN_SD)))
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),
        "home_rating": round(h.get("net", 0.0), 1),
        "away_rating": round(a.get("net", 0.0), 1),
        "confidence": "high",
        "avg_total": round(total, 1),
        "model": "cbbd-adj",
    }
