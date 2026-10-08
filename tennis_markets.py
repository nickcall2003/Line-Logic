"""
tennis_markets.py — model lines for every tennis market from two serve numbers.

The whole markets board (moneyline, game spread, total games, set betting) is
driven by exactly two inputs: each player's probability of winning a point ON
THEIR OWN SERVE. Tennis scoring is fully determined once you know how often each
player holds, so from those two numbers we can price everything.

    ps_a = P(player A wins a point when A is serving)
    ps_b = P(player B wins a point when B is serving)

These come from the profile layer (1st-serve %, 1st/2nd-serve win %, adjusted by
the opponent's return); this module just turns them into prices.

Method: a closed-form game-win probability (exact), a fast Monte-Carlo over games
for the set/match/total-games/handicap distributions, and a short point-level
tiebreak sim at 6-6. ~20k trials keeps lines stable to well under a tenth of a
game. Pure stdlib — runs anywhere the app does and is trivially unit-testable.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


# ---------------------------------------------------------------- game math
def game_win_prob(p: float) -> float:
    """Exact probability the SERVER wins a game, given p = point-win-on-serve.
    Sum over winning to love/15/30, plus reaching deuce and winning it."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    q = 1.0 - p
    base = p**4 * (1 + 4*q + 10*q*q)          # win to love / 15 / 30
    deuce_reach = 20 * p**3 * q**3            # 3-3 in points
    deuce_win = (p*p) / (p*p + q*q)           # win from deuce
    return base + deuce_reach * deuce_win


# ---------------------------------------------------------------- tiebreak
def _sim_tiebreak(ps_a: float, ps_b: float, a_serves_first: bool, rng: random.Random) -> str:
    """7-point tiebreak, win by 2. Serve order A,B,B,A,A,... when A serves first."""
    a = b = 0
    serv_a = a_serves_first
    pts = 0
    while True:
        p = ps_a if serv_a else (1.0 - ps_b)   # prob A wins this point
        if rng.random() < p:
            a += 1
        else:
            b += 1
        pts += 1
        if (a >= 7 or b >= 7) and abs(a - b) >= 2:
            return "a" if a > b else "b"
        if pts == 1 or (pts % 2) == 1:         # switch after point 1, then every 2
            serv_a = not serv_a


# ---------------------------------------------------------------- one set
def _sim_set(ps_a: float, ps_b: float, a_serves_first: bool, rng: random.Random):
    """Simulate one set at the game level. Returns (games_a, games_b, next_first)."""
    pg_a = game_win_prob(ps_a)                # A holds when A serves
    pg_b = game_win_prob(ps_b)                # B holds when B serves
    ga = gb = 0
    serv_a = a_serves_first
    while True:
        if serv_a:
            if rng.random() < pg_a: ga += 1
            else: gb += 1
        else:
            if rng.random() < pg_b: gb += 1
            else: ga += 1
        serv_a = not serv_a
        if ga >= 6 or gb >= 6:
            if abs(ga - gb) >= 2 or ga == 7 or gb == 7:   # 6-x(≥2) or 7-5
                break
            if ga == 6 and gb == 6:                        # tiebreak
                if _sim_tiebreak(ps_a, ps_b, serv_a, rng) == "a": ga += 1
                else: gb += 1
                break
    # standard alternation: whoever served first this set serves second next set
    return ga, gb, (not a_serves_first)


# ---------------------------------------------------------------- match
@dataclass
class MarketResult:
    p_match_a: float
    exp_total_games: float
    set_scores: dict          # "2-0","2-1","1-2","0-2" -> prob
    total_over: dict          # line -> P(total games > line)
    spread_a: dict            # handicap -> P(A games - B games > handicap)
    n: int = 0


def simulate(ps_a: float, ps_b: float, best_of: int = 3, n: int = 20000,
             total_lines=None, spread_lines=None, seed: int | None = None) -> MarketResult:
    """Monte-Carlo the match in a single pass; return every board market."""
    rng = random.Random(seed)
    sets_needed = best_of // 2 + 1
    total_lines = list(total_lines or [])
    spread_lines = list(spread_lines or [])

    a_wins = 0
    tot_games_sum = 0.0
    score_counts: dict[str, int] = {}
    over_counts = {L: 0 for L in total_lines}
    spread_counts = {h: 0 for h in spread_lines}

    for i in range(n):
        sa = sb = 0
        a_games = b_games = 0
        serves_first = (i % 2 == 0)            # alternate; washes out over n
        while sa < sets_needed and sb < sets_needed:
            ga, gb, serves_first = _sim_set(ps_a, ps_b, serves_first, rng)
            a_games += ga; b_games += gb
            if ga > gb: sa += 1
            else: sb += 1
        if sa > sb: a_wins += 1
        tg = a_games + b_games
        tot_games_sum += tg
        score_counts[f"{sa}-{sb}"] = score_counts.get(f"{sa}-{sb}", 0) + 1
        for L in total_lines:
            if tg > L: over_counts[L] += 1
        margin = a_games - b_games
        for h in spread_lines:
            if margin > h: spread_counts[h] += 1

    return MarketResult(
        p_match_a=a_wins / n,
        exp_total_games=tot_games_sum / n,
        set_scores={k: v / n for k, v in sorted(score_counts.items())},
        total_over={L: over_counts[L] / n for L in total_lines},
        spread_a={h: spread_counts[h] / n for h in spread_lines},
        n=n,
    )


# ---------------------------------------------------------------- pricing
def fair_odds(prob: float):
    """Decimal fair odds from a probability (no vig)."""
    return round(1.0 / prob, 2) if prob > 0 else None


def devig_two_way(dec_a: float, dec_b: float):
    """Strip the book's margin from a two-way market -> (fair_a, fair_b) summing to 1."""
    ia, ib = 1.0 / dec_a, 1.0 / dec_b
    tot = ia + ib
    return ia / tot, ib / tot


def edge_ev(model_prob: float, market_dec: float) -> float:
    """EV per 1u staked at the book's price: model_prob * decimal - 1."""
    return model_prob * market_dec - 1.0


# ---------------------------------------------------------------- serve inputs
def serve_point_win(first_pct, first_won, second_won):
    """A player's RAW serve-points-won from their serve splits (no opponent yet).
    Inputs 0..1: 1st-serve in %, 1st-serve win %, 2nd-serve win %."""
    return first_pct * first_won + (1 - first_pct) * second_won


# Tour-average serve-points-won by surface (ATP). Return average = 1 - this.
# Clay is slower so returners win more points; grass/hard reward the server.
TOUR_SPW = {"Hard": 0.642, "Clay": 0.625, "Grass": 0.655, "default": 0.640}


def expected_serve_pts(server_spw, returner_rpw, surface="Hard"):
    """Opponent-adjusted point-win-on-serve. A hold is a battle between THIS
    server's serve and THAT opponent's return, so we anchor to the surface's tour
    average and add the server's edge while subtracting the returner's edge:

        ps = tour_spw + (server_spw - tour_spw) - (returner_rpw - tour_rpw)
           = server_spw - returner_rpw + tour_rpw

    server_spw = server's career/surface serve-points-won (0..1)
    returner_rpw = opponent's career/surface RETURN-points-won (0..1)
    Equal, average players return the tour baseline; a great returner pulls the
    server down, a weak one lets them hold easier. Clamped to a sane band."""
    s = TOUR_SPW.get(surface, TOUR_SPW["default"])
    tour_rpw = 1.0 - s
    ps = server_spw - returner_rpw + tour_rpw
    return min(0.80, max(0.50, ps))


def price_match(a: dict, b: dict, surface: str = "Hard", best_of: int = 3,
                n: int = 20000, total_lines=None, spread_lines=None, seed: int | None = None):
    """End-to-end: take two player profiles that each carry serve-points-won
    ('spw') and return-points-won ('rpw'), combine them opponent-adjusted (so BOTH
    serve and return matter), simulate, and return the priced markets plus the two
    derived serve numbers for transparency.

    a, b: {"spw": 0.68, "rpw": 0.40, ...}  (surface-specific rates preferred)
    """
    ps_a = expected_serve_pts(a["spw"], b["rpw"], surface)   # A serving vs B returning
    ps_b = expected_serve_pts(b["spw"], a["rpw"], surface)   # B serving vs A returning
    res = simulate(ps_a, ps_b, best_of=best_of, n=n,
                   total_lines=total_lines, spread_lines=spread_lines, seed=seed)
    return {
        "ps_a": round(ps_a, 4), "ps_b": round(ps_b, 4),
        "hold_a": round(game_win_prob(ps_a), 4), "hold_b": round(game_win_prob(ps_b), 4),
        "markets": res,
    }
