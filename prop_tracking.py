"""
prop_tracking.py — persist player props and grade them once the game is final.

Purpose: see whether the PROJECTION MODEL is actually any good. Every prop the
model shows is logged with its projection, the book line, and the lean; after the
game we fill in what the player actually did and mark the lean right or wrong.

IMPORTANT: this is a MODEL-QUALITY scoreboard only. Prop results deliberately do
NOT feed the site's win/loss record, units, ROI, or CLV — those track the game
picks. Keeping them separate means a hot or cold prop stretch can't distort the
betting record (and vice versa).

Nothing here is allowed to break a page: every function is best-effort.
"""
import datetime as dt
import re

from sqlalchemy import select

from models import PropResult

# Stats whose hit rate is structurally skewed by "take the under on almost
# everyone" (rare, low-event props) — excluded from the model-accuracy record so
# they can't flatter the headline. Matched on a normalized stat key.
_SKEW_STATS = {"homeruns", "hr", "stolenbases", "stolenbase", "sb"}


def _stat_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Canonical display label per normalized stat key, so "Points"/"points"/"PTS"
# collapse into ONE category instead of showing up as duplicate rows.
_STAT_CANON = {
    "points": "Points", "pts": "Points", "playerpoints": "Points",
    "rebounds": "Rebounds", "reb": "Rebounds", "rebs": "Rebounds",
    "totalrebounds": "Rebounds", "playerrebounds": "Rebounds",
    "assists": "Assists", "ast": "Assists", "playerassists": "Assists",
    "threepointersmade": "3-Pointers", "threepointmade": "3-Pointers",
    "threept": "3-Pointers", "3pm": "3-Pointers", "madethrees": "3-Pointers",
    "threepm": "3-Pointers", "fg3m": "3-Pointers",
    "steals": "Steals", "stl": "Steals", "blocks": "Blocks", "blk": "Blocks",
    "turnovers": "Turnovers", "to": "Turnovers", "tov": "Turnovers",
    "pointsreboundsassists": "Pts+Reb+Ast", "pra": "Pts+Reb+Ast",
    "pointsrebounds": "Pts+Reb", "pr": "Pts+Reb",
    "pointsassists": "Pts+Ast", "pa": "Pts+Ast",
    "reboundsassists": "Reb+Ast", "ra": "Reb+Ast",
    "passingyards": "Passing Yards", "passyards": "Passing Yards",
    "rushingyards": "Rushing Yards", "rushyards": "Rushing Yards",
    "receivingyards": "Receiving Yards", "recyards": "Receiving Yards",
    "recyds": "Receiving Yards", "rushyds": "Rushing Yards", "passyds": "Passing Yards",
    "receptions": "Receptions", "rec": "Receptions",
    "passingtouchdowns": "Passing TDs", "passtd": "Passing TDs",
    "strikeouts": "Strikeouts", "ks": "Strikeouts", "hits": "Hits",
    "totalbases": "Total Bases", "rbis": "RBIs", "rbi": "RBIs", "runs": "Runs",
    "shotsongoal": "Shots on Goal", "sog": "Shots on Goal", "shots": "Shots on Goal",
    "goals": "Goals", "saves": "Saves",
}


def _stat_label(s):
    k = _stat_key(s)
    return _STAT_CANON.get(k) or (s or "").strip().title() or "Other"


# ---------- record (when props are shown) ----------
def log_props(db, sport, game_ref, props, when=None):
    """Upsert the props we showed for a game (projection/line/lean), ungraded."""
    if not props:
        return 0
    when = when or dt.datetime.now()
    n = 0
    for p in props:
        try:
            player = (p.get("player") or "").strip()
            stat = (p.get("stat") or p.get("label") or "").strip()
            line, proj = p.get("line"), p.get("projection")
            if not player or not stat or line is None or proj is None:
                continue
            lean = "OVER" if float(proj) > float(line) else "UNDER"
            odds = p.get("over_odds") if lean == "OVER" else p.get("under_odds")
            existing = db.execute(
                select(PropResult).where(
                    PropResult.sport == sport, PropResult.game_ref == str(game_ref),
                    PropResult.player == player, PropResult.stat == stat)
            ).scalar_one_or_none()
            if existing:
                if existing.actual is None:      # not graded yet -> refresh the numbers
                    existing.line = float(line)
                    existing.projection = float(proj)
                    existing.lean = lean
                    existing.odds = odds
                continue
            db.add(PropResult(
                sport=sport, game_ref=str(game_ref), settled_date=when,
                player=player, stat=stat, line=float(line), projection=float(proj),
                lean=lean, odds=odds, actual=None, correct=None))
            n += 1
        except Exception:
            continue
    if n:
        db.commit()
    return n


# ---------- projection-movement snapshots ----------
_SNAP_LAST = {}   # per-process throttle: sport|game_ref -> epoch seconds


def snapshot_cards_bg(sport, game_ref, cards):
    """Fire-and-forget: snapshot the venom cards' projections in a background
    thread so the venom endpoint takes no extra latency. Throttled per game."""
    try:
        import threading
        threading.Thread(target=_snapshot_cards_worker,
                         args=(sport, str(game_ref), cards), daemon=True).start()
    except Exception:
        pass


def _snapshot_cards_worker(sport, game_ref, cards, throttle_s=900):
    import time
    key = sport + "|" + game_ref
    now = time.time()
    last = _SNAP_LAST.get(key)
    if last and (now - last) < throttle_s:   # at most one snapshot per game / 15 min
        return
    _SNAP_LAST[key] = now
    try:
        from db import SessionLocal
        from models import PropSnapshot
        when = dt.datetime.now()
        with SessionLocal() as db:
            n = 0
            for c in (cards or []):
                player = (c.get("player") or "").strip()
                projs = c.get("projections")
                if not player or not isinstance(projs, dict):
                    continue
                for skey, val in projs.items():
                    if val is None:
                        continue
                    try:
                        proj = float(val)
                    except (TypeError, ValueError):
                        continue
                    last_row = db.execute(
                        select(PropSnapshot).where(
                            PropSnapshot.sport == sport, PropSnapshot.game_ref == game_ref,
                            PropSnapshot.player == player, PropSnapshot.stat == skey)
                        .order_by(PropSnapshot.captured_at.desc()).limit(1)
                    ).scalar_one_or_none()
                    if last_row is not None and last_row.projection is not None \
                            and abs(last_row.projection - proj) < 0.05:
                        continue
                    db.add(PropSnapshot(sport=sport, game_ref=game_ref, player=player,
                                        stat=skey, projection=proj, line=None, captured_at=when))
                    n += 1
            if n:
                db.commit()
    except Exception:
        pass


def game_movement(db, sport, game_ref, max_points=24):
    """Per-prop projection history for a game, for the movement UI:
    {player||statkey: [{t, proj}]}. Only props with >= 2 points are returned."""
    from models import PropSnapshot
    rows = db.execute(
        select(PropSnapshot).where(
            PropSnapshot.sport == sport, PropSnapshot.game_ref == str(game_ref))
        .order_by(PropSnapshot.captured_at.asc())
    ).scalars().all()
    out = {}
    for r in rows:
        k = r.player + "||" + _stat_key(r.stat)
        out.setdefault(k, []).append({
            "t": r.captured_at.isoformat() if r.captured_at else None,
            "proj": round(r.projection, 2) if r.projection is not None else None})
    for k in list(out.keys()):
        if len(out[k]) < 2:
            del out[k]
        elif len(out[k]) > max_points:
            out[k] = out[k][-max_points:]
    return out


# ---------- grade (after the game) ----------
def grade_props(db, sport, game_ref, actuals, when=None):
    """actuals: {(player_lower, stat_lower): value}. Fills in actual + correct.
    A result exactly ON the line is a push (correct = None)."""
    if not actuals:
        return 0
    rows = db.execute(
        select(PropResult).where(
            PropResult.sport == sport, PropResult.game_ref == str(game_ref),
            PropResult.actual.is_(None))
    ).scalars().all()
    n = 0
    for r in rows:
        val = actuals.get((r.player.lower(), r.stat.lower()))
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        r.actual = val
        if val == r.line:
            r.correct = None                       # push
        else:
            went_over = val > r.line
            r.correct = (went_over and r.lean == "OVER") or ((not went_over) and r.lean == "UNDER")
        if when:
            r.settled_date = when
        n += 1
    if n:
        db.commit()
    return n


# ---------- read (the scoreboard) ----------
def prop_record(db, sport=None, days=30):
    """How the projection model is doing: hit rate by stat, and overall."""
    since = dt.datetime.now() - dt.timedelta(days=days)
    q = select(PropResult).where(PropResult.settled_date >= since,
                                 PropResult.actual.isnot(None))
    if sport:
        q = q.where(PropResult.sport == sport)
    rows = db.execute(q).scalars().all()
    graded = [r for r in rows if r.correct is not None
              and _stat_key(r.stat) not in _SKEW_STATS]
    by_stat = {}
    for r in graded:
        lab = _stat_label(r.stat)          # merge Points/points/PTS into one row
        b = by_stat.setdefault(lab, {"w": 0, "l": 0, "label": lab})
        b["w" if r.correct else "l"] += 1
    for b in by_stat.values():
        t = b["w"] + b["l"]
        b["hit_pct"] = round(100 * b["w"] / t, 1) if t else None
        b["n"] = t
    w = sum(1 for r in graded if r.correct)
    total = len(graded)
    # average absolute projection error — the honest "how close is the model"
    errs = [abs(r.actual - r.projection) for r in graded if r.actual is not None]
    return {
        "sport": sport or "all", "days": days,
        "graded": total, "wins": w, "losses": total - w,
        "hit_pct": round(100 * w / total, 1) if total else None,
        "avg_abs_error": round(sum(errs) / len(errs), 2) if errs else None,
        "pushes": sum(1 for r in rows if r.correct is None and r.actual is not None),
        "by_stat": by_stat,
        "note": ("Model-quality tracking only \u2014 prop results do NOT count toward the "
                 "site's win/loss record, units, or ROI."),
    }


def recent_props(db, sport=None, limit=60):
    """Recently graded props (for a results view)."""
    q = select(PropResult).where(PropResult.actual.isnot(None))
    if sport:
        q = q.where(PropResult.sport == sport)
    rows = db.execute(q.order_by(PropResult.settled_date.desc()).limit(limit)).scalars().all()
    return [{
        "sport": r.sport, "player": r.player, "stat": r.stat,
        "line": r.line, "projection": r.projection, "lean": r.lean,
        "actual": r.actual,
        "result": ("push" if r.correct is None else ("win" if r.correct else "loss")),
        "date": r.settled_date.date().isoformat() if r.settled_date else None,
    } for r in rows]


def stored_props(db, sport, game_ref):
    """The props we logged for a game, in the shape the props UI expects.
    Used once a game ends and the books pull their lines, so a finished game keeps
    showing its props with the graded actuals."""
    rows = db.execute(
        select(PropResult).where(PropResult.sport == sport,
                                 PropResult.game_ref == str(game_ref))
    ).scalars().all()
    out = []
    for r in rows:
        p = {
            "player": r.player, "stat": r.stat, "label": r.stat,
            "line": r.line, "projection": r.projection,
            "actual": r.actual, "lean": r.lean,
            "edge": round(abs((r.projection or 0) - (r.line or 0)), 2),
        }
        if r.odds is not None:
            if r.lean == "OVER":
                p["over_odds"] = r.odds
            else:
                p["under_odds"] = r.odds
        if r.correct is not None:
            p["result"] = "win" if r.correct else "loss"
        elif r.actual is not None:
            p["result"] = "push"
        out.append(p)
    out.sort(key=lambda p: -(p.get("edge") or 0))
    return out
