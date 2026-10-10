"""
share_cards.py — branded PNG share cards rendered straight from the DB.

The marketing keystone the audits asked for: one-tap, post-ready images in the
Line Logic look (charcoal & gold). Two cards:

  * PICK card  — matchup, the pick, the model's fair line vs the best market
                 line (with the sportsbook it's at), and the model edge.
  * RECAP card — transparency: W-L, units, ROI and CLV over a window, with a
                 per-sport breakdown. Proof the model beats the close.

Rendered with Pillow (no headless browser), so it works on any host that can
install Pillow — no Chromium dependency on the server. Fonts are bundled in
./fonts so the output is identical regardless of the base image's system fonts.
Everything degrades: a missing logo draws a gold "LL" emblem, a missing font
falls back to Pillow's default. Callers get PNG bytes; never raises on a field
that's absent.
"""
from __future__ import annotations

import datetime as dt
import io
import os

from PIL import Image, ImageDraw, ImageFont

# ── brand ──────────────────────────────────────────────────────────────────
SS = 2                       # supersample factor (render 2x, downscale -> crisp)
W, H = 1080, 1350            # Instagram portrait
BG       = (11, 11, 12)
BG2      = (17, 18, 20)
PANEL    = (20, 21, 24)
PANEL_HI = (27, 28, 32)
LINE     = (42, 43, 48)
GOLD     = (198, 161, 91)
GOLD_HI  = (232, 200, 122)
TEXT     = (244, 244, 245)
MUTED    = (138, 138, 144)
FAINT    = (99, 100, 106)
WIN      = (53, 199, 89)
LOSS     = (255, 77, 77)

_FONTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_FONT_FILES = {
    "bold":  "DejaVuSans-Bold.ttf",
    "reg":   "DejaVuSans.ttf",
    "cond":  "DejaVuSansCondensed-Bold.ttf",
    "mono":  "DejaVuSansMono-Bold.ttf",
}
_font_cache: dict = {}


def _font(kind: str, size: int):
    key = (kind, size)
    f = _font_cache.get(key)
    if f is not None:
        return f
    try:
        f = ImageFont.truetype(os.path.join(_FONTDIR, _FONT_FILES.get(kind, "DejaVuSans.ttf")), size)
    except Exception:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _font_cache[key] = f
    return f


# ── sportsbook chips (mirrors bookMeta() in index.html) ──────────────────────
def _book_meta(name: str):
    n = (name or "").lower()
    table = [
        ("draftking", ("DK",   (83, 211, 55),  (7, 33, 10))),
        ("fanduel",   ("FD",   (20, 147, 255), (6, 36, 63))),
        ("betmgm",    ("MGM",  (198, 161, 91), (26, 18, 6))),
        ("caesar",    ("CZR",  (29, 122, 76),  (234, 255, 242))),
        ("espn",      ("ESPN", (204, 0, 0),    (255, 255, 255))),
        ("betrivers", ("BR",   (25, 97, 180),  (255, 255, 255))),
        ("fanatics",  ("FAN",  (32, 53, 107),  (255, 255, 255))),
        ("hardrock",  ("HR",   (106, 42, 140), (255, 255, 255))),
        ("pointsbet", ("PB",   (228, 0, 43),   (255, 255, 255))),
        ("bovada",    ("BOV",  (177, 19, 19),  (255, 255, 255))),
        ("betonline", ("BOL",  (11, 107, 58),  (255, 255, 255))),
        ("sportsgameodds", ("SGO", (68, 68, 68), (221, 221, 221))),
    ]
    for key, meta in table:
        if key in n:
            return meta
    s = "".join(c for c in (name or "") if c.isalnum())[:3].upper() or "BK"
    return (s, (58, 59, 51), (231, 227, 213))


# ── small drawing helpers ────────────────────────────────────────────────────
def _am(o):
    """American odds as a display string with explicit sign."""
    if o is None:
        return "—"
    try:
        o = int(round(float(o)))
    except Exception:
        return str(o)
    return ("+%d" % o) if o > 0 else str(o)


def _spread(v):
    """A point spread as a signed display string; 0 -> 'PK'."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return str(v)
    if abs(v) < 0.05:
        return "PK"
    s = ("%+.1f" % v)
    return s[:-2] if s.endswith(".0") else s      # +3.0 -> +3


def _book_name(b):
    """A clean full sportsbook name for display (The Odds API titles are already
    tidy; this just trims and title-cases a stray key)."""
    if not b:
        return ""
    b = str(b).strip()
    return b if len(b) <= 18 else b[:18]


def _tw(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1]


def _text(draw, xy, text, font, fill, anchor=None):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _center(draw, cx, y, text, font, fill):
    w, _ = _tw(draw, text, font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _fit(draw, text, kind, size, max_w, min_size=28):
    """Shrink a font until `text` fits in max_w (for long team names)."""
    s = size
    while s > min_size:
        f = _font(kind, s)
        w, _ = _tw(draw, text, f)
        if w <= max_w:
            return f
        s -= 2
    return _font(kind, min_size)


def _panel(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _chip(draw, x, y, book, h=46):
    """Draw a sportsbook chip at (x,y); returns its right edge."""
    s, c, t = _book_meta(book)
    f = _font("bold", int(h * 0.52))
    tw, _ = _tw(draw, s, f)
    w = tw + int(h * 0.9)
    _panel(draw, (x, y, x + w, y + h), radius=h // 2, fill=c)
    _center(draw, x + w / 2, y + (h - int(h * 0.52)) / 2 - 3, s, f, t)
    return x + w


def _load_logo(px):
    """logo.PNG (square-ish, transparent) scaled to px. Returns RGBA or None."""
    for p in ("logo.PNG", "logo.png"):
        if os.path.exists(p):
            try:
                im = Image.open(p).convert("RGBA")
                im.thumbnail((px, px), Image.LANCZOS)
                return im
            except Exception:
                return None
    return None


def _emblem(draw, cx, cy, r):
    """Fallback gold ring + 'LL' when no logo asset is present."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=GOLD, width=max(3, r // 10))
    f = _font("cond", int(r * 1.05))
    _center(draw, cx, cy - r * 0.62, "LL", f, GOLD)


def _header(img, draw, sub=None):
    """Brand header band: logo/emblem + LINE LOGIC wordmark + site."""
    pad = 60 * SS
    y = 52 * SS
    logo = _load_logo(84 * SS)
    lx = pad
    if logo is not None:
        img.paste(logo, (lx, y - 4 * SS), logo)
        lx += logo.width + 24 * SS
    else:
        r = 42 * SS
        _emblem(draw, pad + r, y + r, r)
        lx = pad + 2 * r + 24 * SS
    _text(draw, (lx, y + 2 * SS), "LINE LOGIC", _font("cond", 54 * SS), TEXT)
    _text(draw, (lx + 2, y + 56 * SS), "thelinelogic.com", _font("reg", 26 * SS), GOLD)
    # thin gold rule under the header
    ry = y + 108 * SS
    draw.line((pad, ry, W * SS - pad, ry), fill=LINE, width=2 * SS)
    draw.line((pad, ry, pad + 150 * SS, ry), fill=GOLD, width=3 * SS)
    return ry


def _footer(draw, note="Model projections · not a guarantee · bet responsibly"):
    pad = 60 * SS
    y = (H - 70) * SS
    draw.line((pad, y - 24 * SS, W * SS - pad, y - 24 * SS), fill=LINE, width=2 * SS)
    _text(draw, (pad, y), note, _font("reg", 24 * SS), FAINT)
    r = _font("bold", 24 * SS)
    w, _ = _tw(draw, "21+", r)
    _text(draw, (W * SS - pad - w, y), "21+", r, FAINT)


def _canvas():
    img = Image.new("RGB", (W * SS, H * SS), BG)
    draw = ImageDraw.Draw(img)
    # subtle vertical gradient wash so it isn't a flat black
    top = Image.new("RGB", (1, H * SS), BG)
    for yy in range(H * SS):
        f = yy / (H * SS)
        r = int(BG[0] + (BG2[0] - BG[0]) * (1 - abs(f - 0.0)))
        top.putpixel((0, yy), (max(BG[0], r), BG[1] + int(2 * (1 - f)), BG[2] + int(3 * (1 - f))))
    img.paste(top.resize((W * SS, H * SS)), (0, 0))
    draw = ImageDraw.Draw(img)
    # faint gold frame
    m = 20 * SS
    draw.rounded_rectangle((m, m, W * SS - m, H * SS - m), radius=36 * SS,
                           outline=(70, 60, 38), width=2 * SS)
    return img, draw


def _finish(img):
    img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── PICK CARD ────────────────────────────────────────────────────────────────
def render_pick_card(d: dict) -> bytes:
    """d: sport, away, home, pick, line?, fair?, market?, book?, edge?, prob?,
    confidence?, event_time?  All optional; missing fields render as em dashes."""
    img, draw = _canvas()
    pad = 60 * SS
    ry = _header(img, draw)

    sport = (d.get("sport") or "").upper()
    # sport pill + date row
    py = ry + 34 * SS
    if sport:
        pf = _font("bold", 30 * SS)
        tw, _ = _tw(draw, sport, pf)
        _panel(draw, (pad, py, pad + tw + 44 * SS, py + 52 * SS), radius=26 * SS,
               fill=PANEL_HI, outline=GOLD, width=2 * SS)
        _text(draw, (pad + 22 * SS, py + 8 * SS), sport, pf, GOLD_HI)
    datestr = (d.get("event_time") or dt.date.today().strftime("%b %-d, %Y"))
    df = _font("reg", 30 * SS)
    w, _ = _tw(draw, str(datestr), df)
    _text(draw, (W * SS - pad - w, py + 10 * SS), str(datestr), df, MUTED)

    # matchup
    away = d.get("away") or "Away"
    home = d.get("home") or "Home"
    my = py + 130 * SS
    _text(draw, (pad, my), "MATCHUP", _font("bold", 26 * SS), FAINT)
    mf = _fit(draw, away, "cond", 74 * SS, W * SS - 2 * pad)
    _text(draw, (pad, my + 40 * SS), away, mf, TEXT)
    sep = "vs" if d.get("vs") else "@"
    af = _font("cond", 40 * SS)
    sw, _ = _tw(draw, sep, af)
    _text(draw, (pad, my + 40 * SS + 86 * SS), sep, af, GOLD)
    hf = _fit(draw, home, "cond", 74 * SS, W * SS - 2 * pad - (sw + 26 * SS))
    _text(draw, (pad + sw + 26 * SS, my + 40 * SS + 82 * SS), home, hf, TEXT)

    # THE PICK panel
    pb_y = my + 254 * SS
    _panel(draw, (pad, pb_y, W * SS - pad, pb_y + 196 * SS), radius=28 * SS,
           fill=PANEL, outline=(70, 60, 38), width=2 * SS)
    _text(draw, (pad + 34 * SS, pb_y + 26 * SS), "THE PICK", _font("bold", 28 * SS), GOLD)
    pick = d.get("pick") or "—"
    pkf = _fit(draw, pick, "cond", 76 * SS, W * SS - 2 * pad - 68 * SS, min_size=40)
    _text(draw, (pad + 34 * SS, pb_y + 66 * SS), pick, pkf, TEXT)
    # betting line under the pick: the spread when we have one, else the passed line
    pick_line = None
    if d.get("market_spread") is not None:
        pick_line = _spread(d.get("market_spread"))
    elif d.get("line"):
        pick_line = str(d.get("line"))
    if pick_line:
        _text(draw, (pad + 34 * SS, pb_y + 150 * SS), pick_line,
              _font("mono", 36 * SS), GOLD_HI)
    prob = d.get("prob")
    if prob is not None:
        try:
            ptxt = "MODEL %d%% TO HIT" % round(float(prob) * (100 if float(prob) <= 1 else 1))
        except Exception:
            ptxt = ""
        if ptxt:
            w, _ = _tw(draw, ptxt, _font("bold", 26 * SS))
            _text(draw, (W * SS - pad - 34 * SS - w, pb_y + 156 * SS), ptxt,
                  _font("bold", 26 * SS), MUTED)

    # ── LINES: model vs market, spread + moneyline, best book per row ──────────
    Wi = W * SS - 2 * pad
    rows = []
    msp, mksp = d.get("model_spread"), d.get("market_spread")
    if msp is not None or mksp is not None:
        rows.append(("SPREAD", _spread(msp), _spread(mksp), d.get("spread_book")))
    fair, mkt = d.get("fair"), d.get("market")
    if fair is not None or mkt is not None:
        rows.append(("MONEYLINE", _am(fair), _am(mkt), d.get("book")))
    if not rows:
        rows.append(("MONEYLINE", "—", "—", None))
    any_market = (mksp is not None) or (mkt is not None)

    ly = pb_y + 216 * SS
    rowh = 80 * SS
    head_h = 48 * SS
    note_h = 34 * SS if not any_market else 0
    panelH = 24 * SS + head_h + len(rows) * rowh + 12 * SS + note_h
    _panel(draw, (pad, ly, W * SS - pad, ly + panelH), radius=26 * SS, fill=PANEL,
           outline=(70, 60, 38), width=2 * SS)
    model_x = pad + int(0.44 * Wi)
    market_x = pad + int(0.69 * Wi)
    _text(draw, (pad + 32 * SS, ly + 22 * SS), "LINE", _font("bold", 24 * SS), FAINT)
    _text(draw, (model_x, ly + 22 * SS), "MODEL", _font("bold", 24 * SS), MUTED)
    _text(draw, (market_x, ly + 22 * SS), "MARKET", _font("bold", 24 * SS), MUTED)
    ry2 = ly + head_h + 6 * SS
    vfont = _font("mono", 50 * SS)
    lfont = _font("cond", 40 * SS)
    for lab, mval, kval, bk in rows:
        _text(draw, (pad + 32 * SS, ry2 + 16 * SS), lab, lfont, GOLD_HI)
        _text(draw, (model_x, ry2 + 12 * SS), mval or "—", vfont, TEXT)
        _text(draw, (market_x, ry2 + 12 * SS), kval or "—", vfont,
              GOLD_HI if (kval and kval != "—") else FAINT)
        if bk and kval and kval != "—":
            s, _c, _t = _book_meta(bk)
            cf = _font("bold", int(38 * SS * 0.52))
            cw, _ = _tw(draw, s, cf)
            chw = cw + int(38 * SS * 0.9)
            _chip(draw, W * SS - pad - 30 * SS - chw, ry2 + 20 * SS, bk, h=38 * SS)
        ry2 += rowh
    if not any_market:
        _text(draw, (pad + 32 * SS, ry2 + 2 * SS),
              "Market line not posted yet — model number shown",
              _font("reg", 24 * SS), FAINT)

    # ── EDGE + BEST BOOK banner ───────────────────────────────────────────────
    edge = d.get("edge")
    try:
        ev = float(edge) if edge is not None else None
    except Exception:
        ev = None
    by = ly + panelH + 26 * SS
    bh = 130 * SS
    _panel(draw, (pad, by, W * SS - pad, by + bh), radius=24 * SS,
           fill=(24, 22, 14), outline=GOLD, width=2 * SS)
    midx = pad + Wi / 2
    draw.line((midx, by + 26 * SS, midx, by + bh - 26 * SS), fill=(70, 60, 38), width=2 * SS)
    _text(draw, (pad + 34 * SS, by + 26 * SS), "MODEL EDGE", _font("bold", 26 * SS), MUTED)
    etxt = ("%+.1f%%" % ev) if ev is not None else "—"
    _text(draw, (pad + 34 * SS, by + 60 * SS), etxt, _font("cond", 60 * SS),
          WIN if (ev is not None and ev >= 0) else LOSS if ev is not None else MUTED)
    best = d.get("best_book") or d.get("spread_book") or d.get("book")
    _text(draw, (midx + 34 * SS, by + 26 * SS), "BEST BOOK", _font("bold", 26 * SS), MUTED)
    if best:
        cx = _chip(draw, midx + 34 * SS, by + 60 * SS, best, h=46 * SS) + 16 * SS
        nm = _book_name(best)
        nf = _fit(draw, nm, "cond", 40 * SS, (pad + Wi) - cx - 20 * SS, min_size=24)
        _text(draw, (cx, by + 66 * SS), nm, nf, TEXT)
    else:
        _text(draw, (midx + 34 * SS, by + 60 * SS), "—", _font("cond", 48 * SS), MUTED)

    _footer(draw)
    return _finish(img)


# ── PROP CARD ────────────────────────────────────────────────────────────────
def render_prop_card(d: dict) -> bytes:
    """A Player Edge prop share. d: sport, player, pos?, team?, opp?, prop (label,
    e.g. 'Rec Yds'), proj (model number), line?, side? ('over'/'under'), hit?
    (0-1 or 0-100), book?, odds?, score? (Logic Score 0-100). Missing fields
    render as em dashes."""
    img, draw = _canvas()
    pad = 60 * SS
    ry = _header(img, draw)

    sport = (d.get("sport") or "").upper()
    py = ry + 34 * SS
    if sport:
        pf = _font("bold", 30 * SS)
        tw, _ = _tw(draw, sport, pf)
        _panel(draw, (pad, py, pad + tw + 44 * SS, py + 52 * SS), radius=26 * SS,
               fill=PANEL_HI, outline=GOLD, width=2 * SS)
        _text(draw, (pad + 22 * SS, py + 8 * SS), sport, pf, GOLD_HI)
    label = "PLAYER EDGE"
    lf = _font("bold", 28 * SS)
    w, _ = _tw(draw, label, lf)
    _text(draw, (W * SS - pad - w, py + 10 * SS), label, lf, FAINT)

    # player name (+ pos chip)
    ny = py + 126 * SS
    _text(draw, (pad, ny), "PLAYER", _font("bold", 26 * SS), FAINT)
    player = d.get("player") or "Player"
    nf = _fit(draw, player, "cond", 86 * SS, W * SS - 2 * pad)
    _text(draw, (pad, ny + 36 * SS), player, nf, TEXT)
    # team vs opp
    team = d.get("team") or ""
    opp = d.get("opp") or ""
    sub = team
    if opp:
        sub = (team + "  vs  " + opp) if team else ("vs " + opp)
    pos = d.get("pos") or ""
    if pos:
        sub = (pos + "   ·   " + sub) if sub else pos
    if sub:
        _text(draw, (pad, ny + 132 * SS), sub, _font("bold", 32 * SS), GOLD_HI)

    # THE PROJECTION panel
    pb_y = ny + 226 * SS
    _panel(draw, (pad, pb_y, W * SS - pad, pb_y + 256 * SS), radius=28 * SS,
           fill=PANEL, outline=(70, 60, 38), width=2 * SS)
    prop = (d.get("prop") or "PROP").upper()
    _text(draw, (pad + 34 * SS, pb_y + 28 * SS), "MODEL PROJECTION · " + prop,
          _font("bold", 28 * SS), GOLD)
    proj = d.get("proj")
    ptxt = ("%.1f" % float(proj)) if proj is not None else "—"
    pf2 = _font("cond", 150 * SS)
    _text(draw, (pad + 30 * SS, pb_y + 64 * SS), ptxt, pf2, TEXT)
    # line + side + hit%  (right side of the panel)
    line = d.get("line")
    side = (d.get("side") or "").lower()
    hit = d.get("hit")
    try:
        hitv = None if hit is None else (float(hit) * (100 if float(hit) <= 1 else 1))
    except Exception:
        hitv = None
    if line is not None:
        pre = ("o" if side == "over" else "u" if side == "under" else "")
        ltxt = "%s%s" % (pre, line)
        lf2 = _font("mono", 44 * SS)
        lw, _ = _tw(draw, ltxt, lf2)
        lx = W * SS - pad - 34 * SS - lw
        _text(draw, (lx, pb_y + 150 * SS), ltxt, lf2,
              WIN if side == "over" else LOSS if side == "under" else GOLD_HI)
        llab = _font("bold", 24 * SS)
        lwl, _ = _tw(draw, "LINE", llab)
        _text(draw, (W * SS - pad - 34 * SS - lwl, pb_y + 114 * SS), "LINE", llab, MUTED)
    if hitv is not None:
        htxt = "%d%% TO HIT" % round(hitv)
        hf2 = _font("bold", 30 * SS)
        hw, _ = _tw(draw, htxt, hf2)
        _text(draw, (W * SS - pad - 34 * SS - hw, pb_y + 198 * SS), htxt, hf2, GOLD_HI)

    # bottom row: LOGIC SCORE | BEST BOOK (price + chip)
    cy = pb_y + 300 * SS
    colw = (W * SS - 2 * pad - 28 * SS) / 2
    th = 160 * SS
    _panel(draw, (pad, cy, pad + colw, cy + th), radius=24 * SS, fill=PANEL_HI)
    _text(draw, (pad + 28 * SS, cy + 22 * SS), "LOGIC SCORE", _font("bold", 26 * SS), MUTED)
    sc = d.get("score")
    sctxt = ("%.1f" % float(sc)) if sc is not None else "—"
    _text(draw, (pad + 28 * SS, cy + 58 * SS), sctxt, _font("cond", 64 * SS), GOLD_HI)
    mx = pad + colw + 28 * SS
    _panel(draw, (mx, cy, mx + colw, cy + th), radius=24 * SS, fill=PANEL_HI)
    _text(draw, (mx + 28 * SS, cy + 22 * SS), "BEST BOOK", _font("bold", 26 * SS), MUTED)
    if d.get("book"):   # chip on the label row, never over the number
        s, _c, _t = _book_meta(d["book"])
        cf = _font("bold", int(40 * SS * 0.52))
        cw, _ = _tw(draw, s, cf)
        chw = cw + int(40 * SS * 0.9)
        _chip(draw, mx + colw - 28 * SS - chw, cy + 16 * SS, d["book"], h=40 * SS)
    odds = d.get("odds")
    otxt = _am(odds) if odds is not None else "—"
    _text(draw, (mx + 28 * SS, cy + 66 * SS), otxt, _font("mono", 56 * SS), TEXT)

    _footer(draw)
    return _finish(img)


# ── RECAP CARD ───────────────────────────────────────────────────────────────
def render_recap_card(d: dict) -> bytes:
    """d: window ('LAST 30 DAYS'), wins, losses, pushes?, win_pct?, units,
    roi_pct?, avg_clv?, beat_close_pct?, by_sport?[{sport,wins,losses,units}]."""
    img, draw = _canvas()
    pad = 60 * SS
    ry = _header(img, draw)

    _text(draw, (pad, ry + 30 * SS), "TRACK RECORD", _font("cond", 64 * SS), TEXT)
    win = (d.get("window") or "LAST 30 DAYS").upper()
    _text(draw, (pad, ry + 110 * SS), win, _font("bold", 30 * SS), GOLD)

    wins = int(d.get("wins") or 0)
    losses = int(d.get("losses") or 0)
    pushes = int(d.get("pushes") or 0)
    wp = d.get("win_pct")
    if wp is None:
        tot = wins + losses
        wp = round(100.0 * wins / tot, 1) if tot else 0.0

    # big record
    rec_y = ry + 170 * SS
    _panel(draw, (pad, rec_y, W * SS - pad, rec_y + 236 * SS), radius=28 * SS,
           fill=PANEL, outline=(70, 60, 38), width=2 * SS)
    rec = "%d-%d%s" % (wins, losses, ("-%d" % pushes) if pushes else "")
    rf = _fit(draw, rec, "cond", 136 * SS, W * SS - 2 * pad - 80 * SS, min_size=72)
    _center(draw, (W * SS) / 2, rec_y + 24 * SS, rec, rf, TEXT)
    _center(draw, (W * SS) / 2, rec_y + 174 * SS,
            "%.1f%% WIN RATE" % wp, _font("bold", 34 * SS), GOLD_HI)

    # three stat tiles: UNITS / ROI / CLV
    ty = rec_y + 274 * SS
    gap = 24 * SS
    tilew = (W * SS - 2 * pad - 2 * gap) / 3
    units = d.get("units")
    roi = d.get("roi_pct")
    clv = d.get("avg_clv")
    tiles = [
        ("UNITS", (("%+.2f" % units) if units is not None else "—"),
         (WIN if (units is not None and units >= 0) else LOSS if units is not None else MUTED)),
        ("ROI", (("%+.1f%%" % roi) if roi is not None else "—"),
         (WIN if (roi is not None and roi >= 0) else LOSS if roi is not None else MUTED)),
        ("CLV", (("%+.1f%%" % clv) if clv is not None else "—"),
         (GOLD_HI if clv is not None else MUTED)),
    ]
    for i, (lab, val, col) in enumerate(tiles):
        x = pad + i * (tilew + gap)
        _panel(draw, (x, ty, x + tilew, ty + 158 * SS), radius=24 * SS, fill=PANEL_HI)
        _center(draw, x + tilew / 2, ty + 24 * SS, lab, _font("bold", 28 * SS), MUTED)
        vf = _fit(draw, val, "cond", 62 * SS, tilew - 24 * SS, min_size=34)
        _center(draw, x + tilew / 2, ty + 70 * SS, val, vf, col)

    # per-sport breakdown
    by = d.get("by_sport") or []
    if by:
        hy = ty + 196 * SS
        _text(draw, (pad, hy), "BY SPORT", _font("bold", 28 * SS), GOLD)
        rowh = 60 * SS
        y = hy + 44 * SS
        maxrows = max(0, int(((H - 150) * SS - y) / rowh))
        for s in by[:maxrows]:
            sp = str(s.get("sport", "")).upper()
            w_ = int(s.get("wins") or 0)
            l_ = int(s.get("losses") or 0)
            u_ = s.get("units")
            draw.line((pad, y + rowh - 10 * SS, W * SS - pad, y + rowh - 10 * SS),
                      fill=LINE, width=1 * SS)
            _text(draw, (pad + 4 * SS, y), sp, _font("bold", 32 * SS), TEXT)
            recs = "%d-%d" % (w_, l_)
            _text(draw, (pad + 340 * SS, y), recs, _font("mono", 30 * SS), MUTED)
            if u_ is not None:
                ut = "%+.2f u" % u_
                uf = _font("mono", 30 * SS)
                tw, _ = _tw(draw, ut, uf)
                _text(draw, (W * SS - pad - 4 * SS - tw, y), ut, uf,
                      WIN if u_ >= 0 else LOSS)
            y += rowh

    _footer(draw, note="Every graded pick · CLV = beat the closing line")
    return _finish(img)


if __name__ == "__main__":
    # local smoke test
    pb = render_pick_card({
        "sport": "nba", "away": "Los Angeles Lakers", "home": "Boston Celtics",
        "pick": "Celtics ML", "line": "-110", "fair": -175, "market": -142,
        "book": "DraftKings", "edge": 6.4, "prob": 0.64,
    })
    open("/tmp/pick_card.png", "wb").write(pb)
    rb = render_recap_card({
        "window": "Last 30 Days", "wins": 148, "losses": 121, "pushes": 6,
        "units": 23.4, "roi_pct": 8.7, "avg_clv": 3.1,
        "by_sport": [
            {"sport": "mlb", "wins": 61, "losses": 48, "units": 11.2},
            {"sport": "nba", "wins": 40, "losses": 35, "units": 6.1},
            {"sport": "nhl", "wins": 27, "losses": 22, "units": 4.0},
            {"sport": "tennis", "wins": 20, "losses": 16, "units": 2.1},
        ],
    })
    open("/tmp/recap_card.png", "wb").write(rb)
    print("wrote /tmp/pick_card.png and /tmp/recap_card.png")
