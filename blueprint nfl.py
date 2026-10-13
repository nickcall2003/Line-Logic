"""
blueprint_nfl.py — the "Blueprint" engine for NFL Player Edge (Line Logic).

The Blueprint is the situational-split + regression layer: it prices a player's
scoring and efficiency by WHERE and INTO WHAT his touches happen, then compares
that to what the box score actually gave him. Built entirely from free nflverse
data (play-by-play + FTN charting) — no paid charting feed.

Three reads:

  1. SCORING BLUEPRINT (field zone)       — from play-by-play
     Expected TDs, Scored, Scoring Debt, and a per-zone touch table.
     Debt = xTD - actual. Positive = "due to convert"; negative = regression risk.

  2. BOX BLUEPRINT (defenders in the box)  — from FTN charting (free, 2022+)
     A back's efficiency split by light (<=6) / neutral (7) / heavy (8+) box.

  3. SIGNALS                               — derived chips (Goal-Line Role, etc.)

Build half runs in the profile builder (needs the DataFrames once); runtime half
is cheap dict math per matchup off the stored profile.
"""
from __future__ import annotations

ZONES = ("goal", "rz", "fringe", "open")
ZONE_LABEL = {"goal": "Goal Line (in 5)", "rz": "Red Zone (6-20)",
              "fringe": "Fringe (21-40)", "open": "Open Field"}
BOXES = ("light", "neutral", "heavy")
BOX_LABEL = {"light": "Light box (6-)", "neutral": "Neutral (7)", "heavy": "Heavy box (8+)"}

# Real 2025 league TD rates per touch in zone; the builder overwrites these.
_DEFAULT_RATES = {
    "rush": {"goal": 0.373, "rz": 0.071, "fringe": 0.012, "open": 0.0045},
    "pass": {"goal": 0.427, "rz": 0.190, "fringe": 0.051, "open": 0.006},
}


def _zone_of(y):
    try:
        y = float(y)
    except Exception:
        return None
    if y <= 5:
        return "goal"
    if y <= 20:
        return "rz"
    if y <= 40:
        return "fringe"
    return "open"


def _box_of(n):
    try:
        n = float(n)
    except Exception:
        return None
    if n <= 0:
        return None
    return "light" if n <= 6 else ("neutral" if n == 7 else "heavy")


# ═══════════════════════════ BUILD ═══════════════════════════
def zone_profiles(pbp_df):
    """league_rates, {pid: {rush:{zone:ct}, pass:{zone:ct}, rush_td, rec_td, name}}"""
    df = pbp_df.assign(_zone=pbp_df["yardline_100"].map(_zone_of))
    rush = df[(df["rush_attempt"] == 1) & df["_zone"].notna() & df["rusher_player_id"].notna()]
    pas = df[(df["pass_attempt"] == 1) & df["_zone"].notna() & df["receiver_player_id"].notna()]

    def _rate(frame, td):
        return {z: (float(frame[frame["_zone"] == z][td].mean()) if (frame["_zone"] == z).any() else 0.0)
                for z in ZONES}

    league = {"rush": _rate(rush, "rush_touchdown"), "pass": _rate(pas, "pass_touchdown")}
    players = {}

    def _acc(frame, idc, nmc, kind, td):
        for pid, sub in frame.groupby(idc):
            r = players.setdefault(str(pid), {"rush": {z: 0 for z in ZONES},
                                              "pass": {z: 0 for z in ZONES},
                                              "rush_td": 0, "rec_td": 0, "name": None})
            r["name"] = r["name"] or str(sub[nmc].iloc[0])
            for z in ZONES:
                r[kind][z] = int((sub["_zone"] == z).sum())
            r["rush_td" if kind == "rush" else "rec_td"] = int(sub[td].sum())

    _acc(rush, "rusher_player_id", "rusher_player_name", "rush", "rush_touchdown")
    _acc(pas, "receiver_player_id", "receiver_player_name", "pass", "pass_touchdown")
    return league, players


def box_profiles(pbp_df, ftn_df):
    """{pid: {light/neutral/heavy: {att, yards, td}}} from PBP joined to FTN box.
    Join keys: pbp(game_id, play_id) <-> ftn(nflverse_game_id, nflverse_play_id)."""
    cols = ["nflverse_game_id", "nflverse_play_id", "n_defense_box"]
    ftn = ftn_df[[c for c in cols if c in ftn_df.columns]]
    m = pbp_df.merge(ftn, left_on=["game_id", "play_id"],
                     right_on=["nflverse_game_id", "nflverse_play_id"], how="left")
    m = m.assign(_box=m["n_defense_box"].map(_box_of))
    rush = m[(m["rush_attempt"] == 1) & m["rusher_player_id"].notna() & m["_box"].notna()]
    out = {}
    for pid, sub in rush.groupby("rusher_player_id"):
        d = {b: {"att": 0, "yards": 0.0, "td": 0} for b in BOXES}
        for b in BOXES:
            s = sub[sub["_box"] == b]
            d[b] = {"att": int(len(s)), "yards": float(s["yards_gained"].sum()),
                    "td": int(s["rush_touchdown"].sum())}
        out[str(pid)] = d
    return out


# ═══════════════════════════ RUNTIME ═══════════════════════════
def scoring_blueprint(pz, league=None, games=None):
    if not pz:
        return None
    L = league or _DEFAULT_RATES
    rush_z, pass_z = pz.get("rush") or {}, pz.get("pass") or {}
    if sum(rush_z.values()) + sum(pass_z.values()) < 1:
        return None
    xtd, rows = 0.0, []
    for z in ZONES:
        c, t = int(rush_z.get(z, 0)), int(pass_z.get(z, 0))
        zx = L["rush"][z] * c + L["pass"][z] * t
        xtd += zx
        if c or t:
            rows.append({"zone": z, "label": ZONE_LABEL[z], "carries": c,
                         "targets": t, "xtd": round(zx, 2)})
    scored = int(pz.get("rush_td", 0)) + int(pz.get("rec_td", 0))
    debt = round(xtd - scored, 1)
    if debt >= 1.0:
        reg = {"dir": "up", "label": "Due to convert",
               "note": "Getting scoring-position touches he hasn't cashed yet."}
    elif debt <= -1.0:
        reg = {"dir": "down", "label": "Regression risk",
               "note": "Scoring above what his touch locations support."}
    else:
        reg = {"dir": "flat", "label": "On model", "note": "TDs match his opportunity."}
    gl = int(rush_z.get("goal", 0)) + int(pass_z.get("goal", 0))
    rz = int(rush_z.get("rz", 0)) + int(pass_z.get("rz", 0))
    g = max(1, int(games)) if games else None
    return {"xtd": round(xtd, 1), "scored": scored, "debt": debt, "regress": reg,
            "zones": rows, "goal_touches": gl, "rz_touches": rz,
            "goal_touches_pg": round(gl / g, 1) if g else None}


def box_blueprint(bz):
    """Per-box efficiency table for a back. Returns None if too few box-tagged carries."""
    if not bz:
        return None
    total = sum(bz.get(b, {}).get("att", 0) for b in BOXES)
    if total < 20:
        return None
    rows = []
    for b in BOXES:
        d = bz.get(b) or {"att": 0, "yards": 0.0, "td": 0}
        att = int(d.get("att", 0))
        if not att:
            continue
        rows.append({"box": b, "label": BOX_LABEL[b], "att": att,
                     "ypc": round(d["yards"] / att, 2),
                     "td_rate": round(100.0 * d["td"] / att, 1), "share": round(100.0 * att / total)})
    # the tell: does he gain more vs light boxes, and is the matchup light?
    light = next((r for r in rows if r["box"] == "light"), None)
    heavy = next((r for r in rows if r["box"] == "heavy"), None)
    tell = None
    if light and heavy and light["ypc"] - heavy["ypc"] >= 1.5:
        tell = "Boom back — punishes light boxes (%.1f vs %.1f ypc)." % (light["ypc"], heavy["ypc"])
    elif heavy and heavy["ypc"] >= 4.3 and heavy["share"] >= 25:
        tell = "Grinder — holds up against stacked boxes (%.1f ypc on %d%%)." % (heavy["ypc"], heavy["share"])
    return {"rows": rows, "tell": tell}


def signals(sb, games=None):
    """Blueprint signal chips from the scoring read."""
    out = []
    if not sb:
        return out
    g = max(1, int(games)) if games else None
    glpg = (sb.get("goal_touches", 0) / g) if g else 0
    rzpg = (sb.get("rz_touches", 0) / g) if g else 0
    if glpg >= 1.0:
        out.append({"key": "goal_line", "label": "Goal-Line Role", "on": True})
    if rzpg >= 3.0:
        out.append({"key": "rz_volume", "label": "Red-Zone Volume", "on": True})
    debt = sb.get("debt", 0)
    if debt >= 1.5:
        out.append({"key": "due", "label": "Due For Points", "on": True})
    elif debt <= -1.5:
        out.append({"key": "regress", "label": "Regression Watch", "on": True})
    return out


def blueprint(pz, bz=None, league=None, games=None):
    """The full Blueprint card section for one player."""
    sb = scoring_blueprint(pz, league, games)
    if not sb:
        return None
    return {"scoring": sb, "box": box_blueprint(bz), "signals": signals(sb, games)}


if __name__ == "__main__":
    import os
    if os.path.exists("/tmp/pbp2025.parquet"):
        import pandas as pd
        pbp = pd.read_parquet("/tmp/pbp2025.parquet")
        league, players = zone_profiles(pbp)
        boxes = box_profiles(pbp, pd.read_parquet("/tmp/ftn2025.parquet")) \
            if os.path.exists("/tmp/ftn2025.parquet") else {}
        # match by name -> id
        name2id = {v["name"]: k for k, v in players.items()}
        for want in ("S.Barkley", "D.Henry", "J.Gibbs"):
            pid = next((i for n, i in name2id.items() if want in n), None)
            if not pid:
                continue
            bp = blueprint(players[pid], boxes.get(pid), league, games=6)
            s = bp["scoring"]
            print("%-12s xTD %4.1f  scored %2d  debt %+.1f  [%s]" %
                  (want, s["xtd"], s["scored"], s["debt"], s["regress"]["label"]))
            if bp["box"]:
                print("   box:", [(r["label"], r["ypc"]) for r in bp["box"]["rows"]],
                      "| tell:", bp["box"]["tell"])
            print("   signals:", [x["label"] for x in bp["signals"]])
