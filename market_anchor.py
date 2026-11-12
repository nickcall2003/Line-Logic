"""
market_anchor.py — pull a model's projected margin toward the market line.

A model margin that disagrees with a liquid point-spread market by a lot (CBB
-25 when the market is -3.5) is far more likely the model being miscalibrated —
early-season ratings built on 2-3 games, tiny samples — than a real edge. The
sharper and more liquid the market, the truer that is. So we keep the model's
PICK (the side it likes) but shrink the magnitude: the edge over the market is
trusted in full up to a small band, then only a fraction of the excess beyond
that is kept, and it's hard-capped. The win% is then recomputed from the
anchored margin so the spread read and the moneyline read agree.

Sign convention: margins are home-perspective, positive = home favored. A home
favorite's point spread is negative (e.g. -3.5), so market_margin = -spread_home.

Knobs are env-tunable; defaults are deliberately conservative so small and
medium edges survive and only implausible gaps get compressed:
  ANCHOR_TRUST (4.0)  edge kept in full up to this many points
  ANCHOR_TAIL  (0.30) fraction of the excess beyond TRUST that is kept
  ANCHOR_CAP   (9.0)  hard cap on the anchored edge over the market
"""
import math
import os

# Point-spread sports only. NHL/MLB trade on a fixed +/-1.5 line, which is not a
# margin estimate, so anchoring a goal/run margin to it would be meaningless.
ANCHOR_SPORTS = {"ncaab", "ncaaf", "nba", "nfl"}

# Typical final-margin standard deviation per sport (points) for margin -> win%.
_SD = {"ncaab": 11.0, "ncaaf": 16.5, "nba": 11.5, "nfl": 13.0}


def _f(name, d):
    try:
        return float(os.environ.get(name, d))
    except Exception:
        return float(d)


def prob_from_margin(sport, margin):
    """Win probability for the home side implied by a projected margin."""
    sd = _SD.get(sport, 12.0)
    z = float(margin) / sd
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return round(max(0.02, min(0.98, p)), 4)


def anchor_margin(model_margin, spread_home):
    """Shrink a model margin toward the market. Returns (adjusted, raw);
    unchanged (adjusted == raw) when inputs are missing or the model already
    agrees with the market inside the trust band."""
    if model_margin is None or spread_home is None:
        return model_margin, model_margin
    trust = _f("ANCHOR_TRUST", 4.0)
    tail = _f("ANCHOR_TAIL", 0.30)
    cap = _f("ANCHOR_CAP", 9.0)
    market = -float(spread_home)              # home-favored spread is negative
    raw = float(model_margin)
    edge = raw - market
    s = 1.0 if edge >= 0 else -1.0
    e = abs(edge)
    if e > trust:                             # compress only the excess
        e = trust + (e - trust) * tail
    e = min(e, cap)
    return round(market + s * e, 1), round(raw, 1)


def anchor_game(sport, g):
    """Mutate one game dict in place: anchor exp_margin toward the market spread
    and recompute prob_home / prob_away so both reads agree. No-op unless it's a
    spread sport with both a model margin and a real market spread, and the
    anchor actually moves the number. Records model_margin_raw + anchored=True
    when it fires, so the untouched model line stays available."""
    if sport not in ANCHOR_SPORTS:
        return g
    o = g.get("odds") or {}
    sph = o.get("spread_home")
    mm = g.get("exp_margin")
    if sph is None or mm is None:
        return g
    adj, raw = anchor_margin(mm, sph)
    if adj is None or adj == raw:
        return g
    g["model_margin_raw"] = raw
    g["exp_margin"] = adj
    g["prob_home"] = prob_from_margin(sport, adj)
    if g.get("prob_away") is not None:
        g["prob_away"] = round(1.0 - g["prob_home"], 4)
    g["anchored"] = True
    return g
