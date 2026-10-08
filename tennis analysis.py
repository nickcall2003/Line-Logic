"""
tennis_analysis.py — assembles the full analysis payload for one tennis match.

Pulls the two players' profiles (build_tennis_profiles.py output), the tournament's
Court Pace Index, the stored weather, and the live market odds, runs the match
through tennis_markets.py for the model lines, and returns one JSON the analysis
page renders directly. The heavy lifting (build_payload) is a pure function over
plain dicts, so it is unit-tested without a DB or network; the FastAPI route is a
thin wrapper that gathers the inputs.
"""
from __future__ import annotations

import json
import os

import tennis_markets as TM

_PROFILES_CACHE = {"mtime": 0.0, "data": None}


def load_profiles(path=None):
    """Load tennis_profiles.json (cached by mtime). Returns {} if missing."""
    path = path or os.environ.get("PROFILES_FILE", "tennis_profiles.json")
    for p in (path, "/data/tennis_profiles.json"):
        try:
            mt = os.path.getmtime(p)
            if _PROFILES_CACHE["data"] is not None and _PROFILES_CACHE["mtime"] == mt:
                return _PROFILES_CACHE["data"]
            with open(p) as f:
                data = json.load(f)
            _PROFILES_CACHE.update(mtime=mt, data=data)
            return data
        except Exception:
            continue
    return {}


# ---------------------------------------------------------------- helpers
def _wl_pct(rec):
    w, l = rec.get("w", 0), rec.get("l", 0)
    return round(100 * w / (w + l)) if (w + l) else None


def _tier_score(rank):
    if not rank:
        return 50
    return max(5, min(100, round(100 * (0.5 + 0.5 * (2.718 ** (-(rank - 1) / 40.0))))))


def _momentum(form5, streak):
    f = form5 if form5 is not None else 0.5
    return max(5, min(100, round(50 + (f - 0.5) * 70 + (streak or 0) * 4)))


def _radar(p, surface):
    """0-100 on the comparison axes from what the profile carries."""
    season = None
    if p.get("by_year"):
        yr = sorted(p["by_year"].keys(), reverse=True)[0]
        season = _wl_pct(p["by_year"][yr])
    surf = _wl_pct((p.get("surface") or {}).get(surface, {}))
    return {
        "Form": round((p.get("form10") or 0.5) * 100),
        "Momentum": _momentum(p.get("form5"), p.get("streak")),
        "Energy": 70,                      # proxy until match-load (minutes) is tracked
        "First Set %": round((p.get("first_set_pct") or 0.5) * 100),
        "Surface": surf if surf is not None else 50,
        "Season %": season if season is not None else 50,
        "Tier": _tier_score(p.get("rank")),
        "H2H": 50,                         # even until pairwise H2H pass is added
    }


def _mkt_row(group, selection, line, model_prob, market_dec):
    row = {
        "group": group, "selection": selection, "line": line,
        "model_prob": round(model_prob * 100, 1),
        "model_fair": TM.fair_odds(model_prob),
        "market": market_dec,
        "edge": None,
    }
    if market_dec:
        row["edge"] = round(100 * TM.edge_ev(model_prob, market_dec), 1)
    return row


def build_payload(m: dict, pa: dict, pb: dict, market: dict | None = None,
                  cpi: dict | None = None, n: int = 20000, seed: int | None = 7) -> dict:
    """Pure assembly. m = match fields; pa/pb = finalized profiles; market = odds;
    cpi = {'cpi','category','speed_ratio'} for the tournament."""
    market = market or {}
    surface = m.get("surface") or "Hard"
    best_of = int(m.get("best_of") or 3)
    total_line = market.get("total_line")
    spread_line = market.get("spread_line")

    oa = pa.get("overall", {}); ob = pb.get("overall", {})
    have_rates = oa.get("spw") and ob.get("spw") and oa.get("rpw") and ob.get("rpw")

    model_win_a = None
    rows = []
    if have_rates:
        spreads = [spread_line] if spread_line else []
        totals = [total_line] if total_line else []
        priced = TM.price_match(
            {"spw": oa["spw"], "rpw": oa["rpw"]},
            {"spw": ob["spw"], "rpw": ob["rpw"]},
            surface=surface, best_of=best_of, n=n,
            total_lines=totals, spread_lines=spreads, seed=seed)
        res = priced["markets"]
        model_win_a = res.p_match_a
        na, nb = pa.get("name", "A"), pb.get("name", "B")

        # Moneyline
        rows.append(_mkt_row("Moneyline", na, None, res.p_match_a, market.get("ml_a")))
        rows.append(_mkt_row("Moneyline", nb, None, 1 - res.p_match_a, market.get("ml_b")))
        # Game spread
        if spread_line:
            pa_cov = res.spread_a.get(spread_line, 0.0)
            rows.append(_mkt_row("Game spread", na, f"-{spread_line}", pa_cov, market.get("spread_a")))
            rows.append(_mkt_row("Game spread", nb, f"+{spread_line}", 1 - pa_cov, market.get("spread_b")))
        # Total games
        if total_line:
            over = res.total_over.get(total_line, 0.0)
            rows.append(_mkt_row("Total games", f"Over", total_line, over, market.get("over")))
            rows.append(_mkt_row("Total games", f"Under", total_line, 1 - over, market.get("under")))
        # Set betting (best of 3)
        if best_of == 3:
            ss = res.set_scores
            for label, keyp, mk in ((f"{na} 2-0", "2-0", market.get("set_a20")),
                                    (f"{na} 2-1", "2-1", market.get("set_a21")),
                                    (f"{nb} 2-1", "1-2", market.get("set_b21")),
                                    (f"{nb} 2-0", "0-2", market.get("set_b20"))):
                rows.append(_mkt_row("Set betting", label, None, ss.get(keyp, 0.0), mk))

    def side(p):
        return {
            "name": p.get("name"), "ioc": p.get("ioc"), "rank": p.get("rank"),
            "age": p.get("age"), "career_high": p.get("career_high"),
            "career": p.get("career"), "by_year": p.get("by_year"),
            "surface": p.get("surface"), "overall": p.get("overall"),
            "titles": p.get("titles"), "form5": p.get("form5"), "form10": p.get("form10"),
            "streak": p.get("streak"), "last5": p.get("last5"),
            "first_set_pct": p.get("first_set_pct"), "matches": p.get("matches"),
            "radar": _radar(p, surface),
        }

    return {
        "match": {
            "id": m.get("id"), "tournament": m.get("tournament"), "round": m.get("round"),
            "surface": surface, "best_of": best_of, "scheduled": m.get("scheduled"),
            "event_time": m.get("event_time"), "tier": m.get("tier"),
            "court": m.get("court"), "host_ioc": m.get("host_ioc"),
        },
        "model": {"win_a": model_win_a,
                  "win_b": (1 - model_win_a) if model_win_a is not None else None},
        "conditions": {
            "cpi": (cpi or {}).get("cpi"), "category": (cpi or {}).get("category"),
            "speed_ratio": (cpi or {}).get("speed_ratio"), "surface": surface,
            "weather": m.get("weather"),
        },
        "players": {"a": side(pa), "b": side(pb)},
        "markets": rows,
        "has_model": bool(have_rates),
    }


# ---------------------------------------------------------------- FastAPI route
def register(app):
    """Attach GET /api/tennis/analysis/{match_id} to the app. Imported lazily by main."""
    from fastapi.responses import JSONResponse
    from db import SessionLocal
    from models import Match

    @app.get("/api/tennis/analysis/{match_id}")
    def tennis_analysis(match_id: int):
        prof = load_profiles()
        with SessionLocal() as db:
            m = db.query(Match).filter_by(id=match_id).one_or_none()
            if not m:
                return JSONResponse({"error": "match not found"}, status_code=404)
            tour = "wta" if (m.tier or "").upper() == "WTA" else "atp"
            tp = (prof.get("tours", {}).get(tour, {}))
            players = tp.get("players", {})
            tourneys = tp.get("tournaments", {})

            ka = m.player_a_key or _key(m.player_a)
            kb = m.player_b_key or _key(m.player_b)
            pa = players.get(ka) or {"name": m.player_a}
            pb = players.get(kb) or {"name": m.player_b}
            cpi = tourneys.get((m.tournament or "").lower())

            market = {}
            try:
                from main import _tennis_odds_for
                dec_a, dec_b = _tennis_odds_for(m.provider_match_id, m.player_a, m.player_b)
                market["ml_a"], market["ml_b"] = dec_a, dec_b
            except Exception:
                pass

            payload = build_payload(
                {"id": m.id, "tournament": m.tournament, "round": m.round,
                 "surface": m.surface, "best_of": m.best_of,
                 "scheduled": m.scheduled.isoformat() if m.scheduled else None,
                 "event_time": m.event_time, "tier": m.tier, "weather": m.weather},
                pa, pb, market=market, cpi=cpi)
        return payload


def _key(full_name):
    import re
    import unicodedata
    if not full_name:
        return None
    s = "".join(c for c in unicodedata.normalize("NFKD", full_name.strip().lower())
                if not unicodedata.combining(c)).replace("-", " ").replace(".", ". ")
    s = re.sub(r"\s+", " ", s).strip()
    m = re.match(r"^([a-z])\.\s+(.+)$", s)
    if m:
        return f"{m.group(2).split()[-1]}|{m.group(1)}"
    parts = s.split()
    return f"{parts[-1]}|{parts[0][0]}" if len(parts) >= 2 else None
