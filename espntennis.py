"""
espntennis.py — FREE ATP/WTA schedule + live provider via ESPN's public tennis API.

Why this exists: the paid api-tennis feed and the SofaScore scrape both went away
(one costs money, the other is IP-gated). But the model itself never broke — it's a
surface-Elo engine trained on Jeff Sackmann's free match history (ratings.json). The
only missing piece was a free way to know WHICH matches are on today. ESPN's public
scoreboard gives exactly that for the ATP and WTA tours (and the Slams), at no cost
and with no key.

This is a drop-in TennisProvider: it returns the same neutral MatchInfo / LiveScore /
MatchStats objects every other provider does, so seed.build_day, the prediction engine
and the Odds API de-vig all keep working untouched. Select it with TENNIS_PROVIDER=espn.

Coverage note: ESPN's /atp and /wta endpoints carry tour-level singles plus the Slams.
Challenger and ITF are NOT on these endpoints — those players still get RATINGS from
Sackmann history, they just won't appear as a live board here (books don't post lines
on them anyway). Doubles are skipped. Surface isn't in ESPN's feed, so we infer it from
the tournament name the same way the old paid provider did.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

from base import TennisProvider, MatchInfo, LiveScore, PlayerStats, MatchStats

# Reuse the exact surface-inference and tournament-formatting the paid provider used,
# so a tournament reads the same surface no matter which feed supplied it. If apitennis
# can't import for any reason, fall back to a hard-court default (never crash the board).
try:
    from apitennis import _infer_surface as _surface_of, _fmt_tournament as _fmt_tourn
except Exception:  # pragma: no cover - defensive
    def _surface_of(name, tier=None, when=None):
        n = (name or "").lower()
        if any(w in n for w in ("roland garros", "french", "clay", "madrid", "rome",
                                "monte", "estoril", "barcelona", "hamburg", "kitzbuhel",
                                "bucharest", "gstaad", "umag", "bastad", "cordoba")):
            return "Clay"
        if any(w in n for w in ("wimbledon", "queen", "halle", "grass", "eastbourne",
                                "newport", "stuttgart", "s-hertogenbosch", "mallorca")):
            return "Grass"
        return "Hard"
    def _fmt_tourn(n):
        return n or "Tennis"

_LEAGUES = {"ATP": "atp", "WTA": "wta"}
_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/tennis/{lg}/scoreboard?dates={d}&limit=900"
_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/tennis/{lg}/summary?event={eid}"

# Slam names -> best-of-5 for the men's draw. Women are best-of-3 everywhere.
_SLAMS = ("australian open", "roland garros", "french open", "wimbledon", "us open")

# tiny per-(league, date) cache so get_schedule + get_live_score don't refetch the
# same scoreboard seconds apart. ESPN is generous but there's no reason to hammer it.
_CACHE: dict = {}
_CACHE_TTL = 45.0


def _http_get(url: str, timeout: int = 12):
    """GET JSON. Uses requests if the app already has it (keeps proxy/retry behavior),
    else stdlib urllib. Returns a dict, or {} on any failure — the board degrades to
    'no matches' rather than 500-ing."""
    try:
        import requests  # type: ignore
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "LineLogic/1.0"})
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LineLogic/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def _scoreboard(lg: str, day: dt.date):
    key = (lg, day.isoformat())
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    url = _SCOREBOARD.format(lg=lg, d=day.strftime("%Y%m%d"))
    data = _http_get(url)
    _CACHE[key] = (time.time(), data)
    return data


def _parse_dt(s: str):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return dt.datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        except Exception:
            return None


def _state(comp) -> str:
    st = (((comp.get("status") or {}).get("type") or {}))
    state = (st.get("state") or "").lower()
    if st.get("completed"):
        return "finished"
    if state == "post":
        return "finished"
    if state == "in":
        return "live"
    return "scheduled"


def _name(athlete) -> str:
    """Best full name for Elo keying. displayName ('Carlos Alcaraz') keys cleanly to
    the Sackmann ratings; fullName is the fallback."""
    return (athlete.get("displayName") or athlete.get("fullName")
            or athlete.get("shortName") or "").strip()


def _order_ab(competitors):
    """Return (a, b) competitor dicts with a stable, deterministic a/b assignment so
    prob_a always means the same side. Prefer explicit 'order', then homeAway, then
    list order."""
    comps = list(competitors or [])
    if len(comps) < 2:
        return None, None

    def rank(c):
        o = c.get("order")
        if isinstance(o, int):
            return o
        return 0 if (c.get("homeAway") == "home") else 1
    comps.sort(key=rank)
    return comps[0], comps[1]


def _iter_matches(data, tier: str):
    """Yield (comp, event_name) for every SINGLES match in a scoreboard payload."""
    for ev in (data.get("events") or []):
        ename = ev.get("name") or ev.get("shortName") or "Tennis"
        groupings = ev.get("groupings")
        # Some payloads nest matches under groupings[] (Men's/Women's Singles, Doubles);
        # older/edge ones put competitions directly on the event. Handle both.
        buckets = []
        if groupings:
            for g in groupings:
                gname = ((g.get("grouping") or {}).get("displayName")
                         or g.get("displayName") or (g.get("type") or {}).get("text") or "")
                if "double" in gname.lower():
                    continue  # singles only
                buckets.append(g.get("competitions") or [])
        else:
            buckets.append(ev.get("competitions") or [])
        for comps in buckets:
            for comp in comps:
                yield comp, ename


class ESPNTennisProvider(TennisProvider):
    """Free ATP/WTA feed. Pairs with the Sackmann-trained ratings for predictions and
    the Odds API for lines — no paid tennis subscription required."""

    name = "espn"

    def __init__(self):
        self.last_error = None

    # ---- the three required methods -------------------------------------
    def get_schedule(self, day: datetime) -> list[MatchInfo]:
        """Schedule for one day. The Odds API is the PRIMARY source because ESPN's
        free tennis feed lags and misses events (it had no Shanghai Masters while the
        tournament was live). ESPN is the fallback when the Odds API is off or empty."""
        d = day.date() if isinstance(day, dt.datetime) else day
        oa = self._schedule_from_oddsapi(d)
        if oa:
            return oa
        return self._schedule_from_espn(d)

    def _schedule_from_oddsapi(self, d):
        """Upcoming ATP/WTA matches from The Odds API (needs ODDS_TENNIS=1)."""
        try:
            import odds_api
            evs = odds_api.get_tennis_events()
        except Exception as e:
            self.last_error = f"oddsapi schedule: {e}"
            return []
        out = []
        self._oa_odds = {}
        for e in evs:
            when = _parse_dt(e.get("commence"))
            if not when:
                continue
            # The board shows Central time; bucket a match on its Central calendar day.
            local = when - dt.timedelta(hours=5)
            if local.date() != d:
                continue
            a, b = e.get("home"), e.get("away")
            if not a or not b:
                continue
            tier = e.get("tour") or "ATP"
            title = e.get("title") or "Tennis"
            is_slam = any(s in title.lower() for s in _SLAMS)
            best_of = 5 if (is_slam and tier == "ATP") else 3
            pid = "oa:" + str(e.get("id") or f"{a}|{b}")
            # store the Central wall-clock so the board files the match on the SAME
            # day we bucketed it into (the board filters scheduled by naive date).
            sched = local.replace(tzinfo=None) if getattr(local, "tzinfo", None) else local
            out.append(MatchInfo(
                provider_match_id=pid, tier=tier, tournament=_fmt_tourn(title),
                surface=_surface_of(title, tier, when), player_a=a, player_b=b,
                scheduled=sched, best_of=best_of, status="scheduled"))
            self._oa_odds[pid] = {"ml_a": e.get("ml_home"), "ml_b": e.get("ml_away")}
        return out

    def _schedule_from_espn(self, d):
        out: list[MatchInfo] = []
        for tier, lg in _LEAGUES.items():
            data = _scoreboard(lg, d)
            for comp, ename in _iter_matches(data, tier):
                a, b = _order_ab(comp.get("competitors"))
                if not a or not b:
                    continue
                na = _name(a.get("athlete") or {})
                nb = _name(b.get("athlete") or {})
                if not na or not nb:
                    continue
                cid = str(comp.get("id") or "")
                if not cid:
                    continue
                when = _parse_dt(comp.get("date") or comp.get("startDate")) or dt.datetime.combine(d, dt.time(12, 0))
                # store Central wall-clock to match the board's naive-date filter
                if getattr(when, "tzinfo", None):
                    when_naive = (when - dt.timedelta(hours=5)).replace(tzinfo=None)
                else:
                    when_naive = when
                surface = _surface_of(ename, tier, when)
                is_slam = any(s in ename.lower() for s in _SLAMS)
                best_of = 5 if (is_slam and tier == "ATP") else 3
                out.append(MatchInfo(
                    provider_match_id=f"{lg}:{cid}",
                    tier=tier,
                    tournament=_fmt_tourn(ename),
                    surface=surface,
                    player_a=na,
                    player_b=nb,
                    scheduled=when_naive,
                    best_of=best_of,
                    status=_state(comp),
                ))
        return out

    def get_live_score(self, provider_match_id: str) -> LiveScore:
        lg, cid = self._split(provider_match_id)
        if not cid:
            return LiveScore(status="scheduled")
        # look across today and yesterday (a match can run past midnight UTC)
        today = dt.date.today()
        for d in (today, today - dt.timedelta(days=1), today + dt.timedelta(days=1)):
            data = _scoreboard(lg, d)
            for comp, _ in _iter_matches(data, lg.upper()):
                if str(comp.get("id")) != cid:
                    continue
                return self._livescore_from_comp(comp)
        return LiveScore(status="scheduled")

    def get_match_stats(self, provider_match_id: str) -> MatchStats:
        lg, cid = self._split(provider_match_id)
        if not cid:
            return MatchStats()
        data = _http_get(_SUMMARY.format(lg=lg, eid=cid))
        try:
            return self._stats_from_summary(data)
        except Exception:
            return MatchStats()

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _split(pmid: str):
        s = str(pmid or "")
        if ":" in s:
            lg, cid = s.split(":", 1)
            return lg, cid
        return "atp", s  # legacy ids default to ATP

    @staticmethod
    def _livescore_from_comp(comp) -> LiveScore:
        a, b = _order_ab(comp.get("competitors"))
        ls = LiveScore(status=_state(comp))
        if not a or not b:
            return ls
        sets_a = [int(x.get("value")) for x in (a.get("linescores") or []) if x.get("value") is not None]
        sets_b = [int(x.get("value")) for x in (b.get("linescores") or []) if x.get("value") is not None]
        ls.sets_a, ls.sets_b = sets_a, sets_b
        if a.get("winner"):
            ls.winner = "a"
        elif b.get("winner"):
            ls.winner = "b"
        return ls

    @staticmethod
    def _stats_from_summary(data) -> MatchStats:
        """Best-effort serve/return line from ESPN's summary boxscore. Many matches
        (especially early-round) carry none — we return an empty MatchStats and the UI
        shows it gracefully."""
        box = (data.get("boxscore") or {})
        players = box.get("players") or []
        if not players:
            return MatchStats()

        def one(side) -> PlayerStats:
            ps = PlayerStats()
            stats = {}
            for grp in (side.get("statistics") or []):
                labels = grp.get("labels") or grp.get("names") or []
                vals = grp.get("totals") or grp.get("stats") or []
                for lbl, val in zip(labels, vals):
                    stats[str(lbl).lower()] = val
            def num(*keys):
                for k in keys:
                    if k in stats:
                        try:
                            return float(str(stats[k]).replace("%", "").split("/")[0])
                        except Exception:
                            return None
                return None
            ps.aces = _as_int(num("aces", "ace"))
            ps.double_faults = _as_int(num("double faults", "df", "double_faults"))
            fs = num("1st serve %", "first serve %", "first serve pct")
            ps.first_serve_pct = (fs / 100.0) if fs is not None else None
            return ps

        a_side = players[0] if len(players) > 0 else None
        b_side = players[1] if len(players) > 1 else None
        return MatchStats(
            player_a=one(a_side) if a_side else None,
            player_b=one(b_side) if b_side else None,
        )

    # ---- optional methods the app may call (safe, non-crashing) ---------
    def get_rankings(self):
        """ESPN rankings aren't wired yet; the Sackmann history ratings already cover
        the live player pool. Return [] so the ranking-fallback loader is a no-op."""
        return []

    def get_odds(self, day=None, match_key=None):
        """Moneyline captured alongside the Odds-API schedule, by provider_match_id.
        The main odds layer also matches by player name via odds_api.get_tennis_odds()."""
        if match_key:
            return (getattr(self, "_oa_odds", None) or {}).get(match_key, {})
        return {}

    def _refresh_live(self):
        return None

    def fixture_meta(self, provider_match_id):
        return {}

    def get_match_context(self, key_a, key_b, match_dt=None):
        return {}

    def get_h2h(self, key_a, key_b):
        return {}

    def raw_fixture(self, provider_match_id):
        return {}

    def final_results(self, day):
        return {}

    def player_serve_averages(self, *a, **k):
        return None

    def match_statistics(self, match_key):
        """Detail-view stats payload. ESPN's summary rarely carries serve splits, so
        return an empty dict rather than error the match-detail endpoint."""
        return {}

    def raw_fixture_probe(self, match_id):
        return {}

    def get_match_stats_dict(self, provider_match_id):
        ms = self.get_match_stats(provider_match_id)
        return {"available": ms.available}


def _as_int(v):
    try:
        return int(round(float(v)))
    except Exception:
        return None
