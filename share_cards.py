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
def _prop_verdict(side, hitv):
    """Derive a model verdict from the chosen side and its hit%."""
    if not side or hitv is None:
        return ("MODEL READ", GOLD_HI)
    s = side.upper()
    col = WIN if side == "over" else LOSS
    if hitv >= 60:
        return (s + " · STRONG", col)
    if hitv >= 53:
        return (s + " LEAN", col)
    return ("LEAN " + s, GOLD_HI)


def _grad_panel(img, box, radius, top, bot, outline=None, width=2):
    """Rounded panel with a vertical gradient fill."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    col = Image.new("RGB", (1, h))
    for yy in range(h):
        f = yy / max(1, h - 1)
        col.putpixel((0, yy), tuple(int(top[i] + (bot[i] - top[i]) * f) for i in range(3)))
    grad = col.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(radius), fill=255)
    img.paste(grad, (x0, y0), mask)
    if outline:
        ImageDraw.Draw(img).rounded_rectangle((x0, y0, x1, y1), radius=int(radius),
                                              outline=outline, width=int(width))


def _sport_glow(sport):
    """(base, glow) for the hero spotlight — a deep base with a soft sport-tinted
    lift near the top so the player is lit, not sitting on flat black. Subtle on
    purpose; reads premium, not clip-art."""
    sp = (sport or "").lower()
    if sp in ("nfl", "ncaaf"):
        return (10, 15, 12), (28, 52, 36)
    if sp in ("nba", "wnba", "ncaab", "ncaabb", "wncaab"):
        return (13, 12, 10), (54, 41, 25)
    if sp == "nhl":
        return (10, 14, 19), (30, 44, 62)
    if sp == "mlb":
        return (13, 12, 9), (50, 38, 26)
    return (13, 14, 13), (44, 46, 54)


def _spotlight(img, box, radius, base, glow):
    """A radial spotlight gradient (bright near top-center, fading to the deep
    base) clipped to the rounded hero panel."""
    import math
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    S = 150
    small = Image.new("RGB", (S, S))
    px = small.load()
    cx, cy = S / 2.0, S * 0.30
    maxd = math.hypot(max(cx, S - cx), max(cy, S - cy))
    for yy in range(S):
        for xx in range(S):
            f = max(0.0, 1.0 - math.hypot(xx - cx, yy - cy) / maxd)
            f = f * f
            px[xx, yy] = (int(base[0] + (glow[0] - base[0]) * f),
                          int(base[1] + (glow[1] - base[1]) * f),
                          int(base[2] + (glow[2] - base[2]) * f))
    big = small.resize((w, h), Image.BILINEAR)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(radius), fill=255)
    img.paste(big, (x0, y0), mask)


def _venue_backdrop(img, box, radius, bgimg):
    """Cover-fit a real venue/fanart image into the hero, darkened for brand
    consistency and text legibility, clipped to the rounded panel."""
    try:
        from PIL import ImageChops, ImageEnhance
        x0, y0, x1, y1 = [int(v) for v in box]
        w, h = x1 - x0, y1 - y0
        bw, bh = bgimg.size
        if w <= 0 or h <= 0 or bw <= 0 or bh <= 0:
            return False
        scale = max(w / bw, h / bh)
        nb = bgimg.convert("RGB").resize((max(1, int(bw * scale)), max(1, int(bh * scale))), Image.LANCZOS)
        cxl, cyl = (nb.width - w) // 2, (nb.height - h) // 2
        nb = nb.crop((cxl, cyl, cxl + w, cyl + h))
        nb = ImageEnhance.Brightness(nb).enhance(0.46)
        nb = ImageEnhance.Color(nb).enhance(0.88)
        # darken the top (projection) and bottom (name) bands a bit more
        gr = Image.new("L", (1, h))
        for yy in range(h):
            f = yy / max(1, h - 1)
            v = 255
            if f < 0.24:
                v = int(255 * (1 - (0.24 - f) / 0.24 * 0.5))
            elif f > 0.52:
                v = int(255 * (1 - (f - 0.52) / 0.48 * 0.6))
            gr.putpixel((0, yy), max(0, min(255, v)))
        gr = gr.resize((w, h))
        nb = ImageChops.multiply(nb, Image.merge("RGB", (gr, gr, gr)))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(radius), fill=255)
        img.paste(nb, (x0, y0), mask)
        return True
    except Exception:
        return False


def _fetch_photo(url):
    """Fetch a player headshot PNG (transparent) for compositing. None on failure
    (e.g. a thin roster id); the card falls back to an initials medallion."""
    if not url:
        return None
    try:
        import httpx
        import io as _io
        r = httpx.get(url, timeout=8, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        return Image.open(_io.BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None


def _shadow_text(draw, xy, text, font, fill, sh=3):
    x, y = int(xy[0]), int(xy[1])
    draw.text((x + sh * SS, y + sh * SS), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


def render_prop_card(d: dict) -> bytes:
    """A Player Edge prop share. d: sport, player, pos?, team?, opp?, prop (label,
    e.g. 'Rec Yds'), proj (model number), line?, side? ('over'/'under'), hit?
    (0-1 or 0-100), book?, odds?, score? (Logic Score 0-100). Missing fields
    render as em dashes."""
    img, draw = _canvas()
    pad = 60 * SS
    ry = _header(img, draw)
    style = (d.get("style") or "full").lower()

    sport = (d.get("sport") or "").upper()
    py = ry + 30 * SS
    if sport:
        pf = _font("bold", 30 * SS)
        tw, _ = _tw(draw, sport, pf)
        _panel(draw, (pad, py, pad + tw + 44 * SS, py + 52 * SS), radius=26 * SS,
               fill=PANEL_HI, outline=GOLD, width=2 * SS)
        _text(draw, (pad + 22 * SS, py + 8 * SS), sport, pf, GOLD_HI)
    lab = "PLAYER EDGE"
    lf = _font("bold", 28 * SS)
    w, _ = _tw(draw, lab, lf)
    _text(draw, (W * SS - pad - w, py + 12 * SS), lab, lf, FAINT)

    # ---- HERO: player photo over a gradient stage ----
    proj = d.get("proj")
    line = d.get("line")
    side = (d.get("side") or "").lower()
    hit = d.get("hit")
    try:
        hitv = None if hit is None else (float(hit) * (100 if float(hit) <= 1 else 1))
    except Exception:
        hitv = None

    hx0, hx1 = pad, W * SS - pad
    hy0 = py + 92 * SS
    reserve = (182 if style == "spotlight" else 360) * SS
    hy1 = (H - 104) * SS - reserve
    hero_w, hero_h = hx1 - hx0, hy1 - hy0
    # Resolve TheSportsDB assets once (player cutout + team venue/fanart).
    _assets = {}
    try:
        import sportsdb
        _assets = sportsdb.player_assets(d.get("player"), d.get("sport")) or {}
    except Exception:
        _assets = {}

    # Backdrop: a real venue/fanart image where available, else the spotlight.
    _drew_bg = False
    _bgurl = _assets.get("fanart") or _assets.get("stadium")
    if _bgurl:
        _bgimg = _fetch_photo(_bgurl)
        if _bgimg is not None:
            _drew_bg = _venue_backdrop(img, (hx0, hy0, hx1, hy1), 30 * SS, _bgimg)
    if not _drew_bg:
        _base, _glow = _sport_glow(d.get("sport"))
        _spotlight(img, (hx0, hy0, hx1, hy1), 30 * SS, _base, _glow)
    ImageDraw.Draw(img).rounded_rectangle((hx0, hy0, hx1, hy1), radius=30 * SS,
                                          outline=(74, 63, 40), width=2 * SS)

    # Player: TheSportsDB cutout → ESPN headshot (`img`) → initials.
    photo = None
    _curl = _assets.get("cutout") or _assets.get("render")
    if _curl:
        photo = _fetch_photo(_curl)
    if photo is None:
        photo = _fetch_photo(d.get("img"))
    if photo is not None:
        target_h = int(hero_h - 96 * SS)
        scale = target_h / photo.height
        pw, ph = int(photo.width * scale), target_h
        maxw = int(hero_w * 0.98)
        if pw > maxw:
            scale = maxw / photo.width
            pw, ph = maxw, int(photo.height * scale)
        photo = photo.resize((pw, ph), Image.LANCZOS)
        img.paste(photo, (int(hx0 + (hero_w - pw) / 2), int(hy1 - ph - 6 * SS)), photo)
    else:
        r0 = 150 * SS
        cx, cyc = (hx0 + hx1) // 2, (hy0 + hy1) // 2
        draw.ellipse((cx - r0, cyc - r0, cx + r0, cyc + r0), fill=PANEL_HI,
                     outline=GOLD, width=3 * SS)
        ini = "".join([p[0] for p in (d.get("player") or "?").split()[:2]]).upper()
        f0 = _font("cond", 118 * SS)
        iw, ih = _tw(draw, ini, f0)
        _text(draw, (cx - iw // 2, cyc - ih // 2 - 12 * SS), ini, f0, GOLD_HI)

    # archetype chip — top-left of the stage
    arch = (d.get("arch") or "").strip()
    if arch:
        atxt = "◆ " + arch.upper()
        af = _font("bold", 26 * SS)
        aw, _ = _tw(draw, atxt, af)
        _panel(draw, (hx0 + 28 * SS, hy0 + 28 * SS, hx0 + 28 * SS + aw + 42 * SS,
                      hy0 + 28 * SS + 50 * SS), radius=25 * SS, fill=(13, 14, 11),
               outline=GOLD, width=2 * SS)
        _text(draw, (hx0 + 28 * SS + 21 * SS, hy0 + 28 * SS + 9 * SS), atxt, af, GOLD_HI)

    # big projection — top-right of the stage (shadowed over the photo)
    prop = (d.get("prop") or "PROP").upper()
    ptxt = ("%.1f" % float(proj)) if proj is not None else "—"
    pf2 = _font("cond", 128 * SS)
    pw2, ph2 = _tw(draw, ptxt, pf2)
    prx = hx1 - 32 * SS - pw2
    pry = hy0 + 24 * SS
    _shadow_text(draw, (prx, pry), ptxt, pf2, TEXT)
    lf2 = _font("bold", 30 * SS)
    lw2, _ = _tw(draw, prop, lf2)
    _shadow_text(draw, (hx1 - 32 * SS - lw2, pry + ph2 + 2 * SS), prop, lf2, GOLD_HI)

    # player name + team — bottom-left of the stage (shadowed)
    player = d.get("player") or "Player"
    nf = _fit(draw, player, "cond", 72 * SS, hero_w - 64 * SS)
    npx = hx0 + 32 * SS
    npy = hy1 - 120 * SS
    _shadow_text(draw, (npx, npy), player, nf, TEXT)
    team, opp, pos = d.get("team") or "", d.get("opp") or "", d.get("pos") or ""
    sub = "  ·  ".join([x for x in [pos, team, ("vs " + opp if opp else "")] if x])
    if sub:
        _shadow_text(draw, (npx + 2 * SS, npy + 80 * SS), sub, _font("bold", 30 * SS), GOLD_HI)

    # ---- LINE + EDGE strip (what the user bets on) ----
    sy = hy1 + 26 * SS
    sh = 156 * SS
    _grad_panel(img, (pad, sy, W * SS - pad, sy + sh), 24 * SS, (27, 28, 32), (18, 19, 22),
                outline=(74, 63, 40), width=2 * SS)
    # left: book line + side + hit%
    _text(draw, (pad + 30 * SS, sy + 22 * SS), "BOOK LINE", _font("bold", 24 * SS), MUTED)
    if line is not None:
        pre = "o" if side == "over" else "u" if side == "under" else ""
        lcol = WIN if side == "over" else LOSS if side == "under" else GOLD_HI
        _text(draw, (pad + 30 * SS, sy + 56 * SS), "%s%s" % (pre, line), _font("cond", 74 * SS), lcol)
        if hitv is not None:
            _text(draw, (pad + 30 * SS, sy + 128 * SS), "%d%% to hit" % round(hitv),
                  _font("bold", 24 * SS), MUTED)
    else:
        _text(draw, (pad + 30 * SS, sy + 56 * SS), "—", _font("cond", 74 * SS), GOLD_HI)
    # right: edge vs market + verdict
    edge = d.get("edge")
    elab = "EDGE vs MARKET"
    elf = _font("bold", 24 * SS)
    elw, _ = _tw(draw, elab, elf)
    _text(draw, (W * SS - pad - 30 * SS - elw, sy + 22 * SS), elab, elf, MUTED)
    if edge is not None:
        ev = float(edge)
        etxt = ("+%.1f%%" % ev) if ev >= 0 else ("%.1f%%" % ev)
        ecol = WIN if ev >= 0 else LOSS
        ef = _font("cond", 74 * SS)
        ew, _ = _tw(draw, etxt, ef)
        _text(draw, (W * SS - pad - 30 * SS - ew, sy + 56 * SS), etxt, ef, ecol)
    else:
        etxt = "—"
        ef = _font("cond", 74 * SS)
        ew, _ = _tw(draw, etxt, ef)
        _text(draw, (W * SS - pad - 30 * SS - ew, sy + 56 * SS), etxt, ef, GOLD_HI)
    vtxt, vcol = _prop_verdict(side, hitv)
    vf = _font("bold", 26 * SS)
    vw, _ = _tw(draw, vtxt, vf)
    _text(draw, (W * SS - pad - 30 * SS - vw, sy + 128 * SS), vtxt, vf, vcol)

    # ---- FULL only: Logic Score | Best Book row ----
    if style != "spotlight":
        cy = sy + sh + 22 * SS
        th = (H - 104) * SS - cy
        colw = (W * SS - 2 * pad - 24 * SS) / 2
        _panel(draw, (pad, cy, pad + colw, cy + th), radius=22 * SS, fill=PANEL_HI)
        _text(draw, (pad + 26 * SS, cy + 20 * SS), "LOGIC SCORE", _font("bold", 24 * SS), MUTED)
        sc = d.get("score")
        _text(draw, (pad + 26 * SS, cy + 52 * SS),
              ("%.1f" % float(sc)) if sc is not None else "—", _font("cond", 60 * SS), GOLD_HI)
        mx = pad + colw + 24 * SS
        _panel(draw, (mx, cy, mx + colw, cy + th), radius=22 * SS, fill=PANEL_HI)
        _text(draw, (mx + 26 * SS, cy + 20 * SS), "BEST BOOK", _font("bold", 24 * SS), MUTED)
        if d.get("book"):
            s, _c, _t = _book_meta(d["book"])
            cf = _font("bold", int(40 * SS * 0.52))
            cw, _ = _tw(draw, s, cf)
            chw = cw + int(40 * SS * 0.9)
            _chip(draw, mx + colw - 26 * SS - chw, cy + 14 * SS, d["book"], h=40 * SS)
        odds = d.get("odds")
        _text(draw, (mx + 26 * SS, cy + 56 * SS), _am(odds) if odds is not None else "—",
              _font("mono", 52 * SS), TEXT)

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
