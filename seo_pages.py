"""
seo_pages.py
------------
Server-rendered, indexable SEO landing pages + sitemap/robots.

Why this exists (growth plan #1 traffic lever): the main site is a single-page
app, so Google only ever saw one page ("/"). These are real, crawlable URLs that
target high-intent searches ("nfl predictions today", "nfl player prop
projections", "sports model track record") and funnel visitors into the live
board (/#<sport>) and the Discord. Each page:

  * has its own <title>, meta description, canonical + OG/Twitter cards
  * ships FAQ + WebPage JSON-LD (rich-result eligible)
  * pulls its OWN sport's live record from /api/accuracy, so the page is unique,
    always fresh, and shows real W-L / units / ROI as proof (not thin boilerplate)
  * cross-links every sibling page (internal link graph) + CTAs to board + Discord

Register in main.py:  from seo_pages import router as seo_router; app.include_router(seo_router)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

BASE = "https://www.thelinelogic.com"
OG_IMG = BASE + "/og-image.png"
DISCORD = "https://discord.gg/WmpF9GdWGE"

# ---- page catalog -----------------------------------------------------------
# slug -> config. Predictions + props pages share a builder; track-record is its own.
_SPORTS = {
    "nfl": "NFL",
    "nba": "NBA",
    "mlb": "MLB",
    "nhl": "NHL",
}
# which slugs get a "player props" page too
_PROP_SPORTS = ["nfl", "nba"]

# Human intent copy per sport (kept honest + specific; no guaranteed-winner hype).
_SPORT_BLURB = {
    "nfl": "Every NFL game gets a model projection — a fair spread, total and win "
           "probability built from team ratings, matchup and current injuries — "
           "then compared against the live market to find where the number is off.",
    "nba": "Every NBA game gets a model line — projected margin, total and win "
           "probability from team ratings, pace, rest and injuries — stacked against "
           "the market so you can see which side the number actually favors.",
    "mlb": "Every MLB game gets a model price — projected run total and win "
           "probability built from the starters, lineups, park and bullpen — then "
           "measured against the market to surface real edges, not gut calls.",
    "nhl": "Every NHL game gets a model line — projected goals, win probability and "
           "puck-line value from team ratings, goaltending and injuries — compared "
           "against the market to find mispriced sides and totals.",
}


def _nav():
    items = []
    for s, lab in _SPORTS.items():
        items.append(f'<a href="/{s}-predictions">{lab} Predictions</a>')
    for s in _PROP_SPORTS:
        items.append(f'<a href="/{s}-player-props">{_SPORTS[s]} Props</a>')
    items.append('<a href="/model-track-record">Track Record</a>')
    return '<nav class="xnav">' + "".join(items) + "</nav>"


def _shell(*, title, desc, canonical, h1, body_html, jsonld, data_sport="", data_scope=""):
    """Wrap page content in the full HTML document with SEO head + shared chrome."""
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Line Logic">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{OG_IMG}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{OG_IMG}">
<link rel="icon" href="/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Hanken+Grotesque:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<script type="application/ld+json">{jsonld}</script>
<style>
:root{{--bg:#111211;--panel:#171817;--panel-2:#1E1F1D;--panel-3:#272826;--line:#2A2B29;
--ink:#EDEBE6;--muted:#8E8F8A;--muted-2:#57584F;--win:#5FCF86;--loss:#EE6A54;
--gold:#E8B04B;--gold-soft:#C9932F;--clay-bright:#F2C46A}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(130% 80% at 82% -8%,#1a1710 0%,var(--bg) 55%);background-attachment:fixed;color:var(--ink);font:16px/1.6 "Hanken Grotesque",system-ui,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}}
a{{color:var(--gold);text-decoration:none}}
.wrap{{max-width:880px;margin:0 auto;padding:0 20px}}
header.top{{border-bottom:1px solid var(--line);background:rgba(17,18,17,.86);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);position:sticky;top:0;z-index:5}}
header.top .wrap{{display:flex;align-items:center;gap:14px;height:58px}}
.brand{{font-family:"Archivo",sans-serif;font-weight:800;letter-spacing:-.02em;color:var(--ink);font-size:20px}}
.brand .dot{{color:var(--gold)}}
.xnav{{display:flex;gap:16px;overflow-x:auto;margin-left:auto;white-space:nowrap;scrollbar-width:none}}
.xnav::-webkit-scrollbar{{display:none}}
.xnav a{{color:var(--muted);font-size:13px;font-weight:600;font-family:"Archivo",sans-serif}}
.xnav a:hover{{color:var(--ink)}}
.hero{{padding:46px 0 10px}}
h1{{font-family:"Archivo",sans-serif;font-weight:800;font-size:clamp(27px,5.4vw,42px);line-height:1.12;margin:0 0 16px;letter-spacing:-.03em}}
.lede{{font-size:18px;color:var(--muted);margin:0 0 24px;max-width:62ch}}
.cta{{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 28px}}
.btn{{display:inline-flex;align-items:center;gap:8px;padding:13px 20px;border-radius:12px;font-weight:700;font-size:15px;font-family:"Archivo",sans-serif}}
.btn.p{{background:linear-gradient(180deg,var(--clay-bright),var(--gold));color:#1a1407}}
.btn.p:hover{{filter:brightness(1.05)}}
.btn.s{{background:var(--panel-2);color:var(--ink);border:1px solid var(--line)}}
.btn.s:hover{{border-color:var(--gold)}}
.rec{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 30px}}
.rec .b{{background:linear-gradient(180deg,var(--panel-2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:16px 10px;text-align:center}}
.rec .n{{font-family:"Archivo",sans-serif;font-size:clamp(20px,6vw,26px);font-weight:900;letter-spacing:-.02em;white-space:nowrap}}
.rec .n.pos{{color:var(--win)}}.rec .n.neg{{color:var(--loss)}}
.rec .k{{font-family:"Space Mono",monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:6px;line-height:1.3}}
.rec small{{display:block;color:var(--muted-2);font-size:11px;margin-top:10px;grid-column:1/-1;text-align:center;font-family:"Space Mono",monospace}}
section{{padding:10px 0 6px}}
h2{{font-family:"Archivo",sans-serif;font-weight:800;font-size:22px;margin:30px 0 10px;letter-spacing:-.02em}}
h3{{font-family:"Archivo",sans-serif;font-size:16px;margin:18px 0 4px}}
p{{margin:0 0 14px;color:#D4D3CC}}
ul{{margin:0 0 16px;padding-left:20px;color:#D4D3CC}}li{{margin:6px 0}}
.faq .q{{font-weight:700;margin:18px 0 4px;color:var(--ink)}}
.foot{{border-top:1px solid var(--line);margin-top:40px;padding:26px 0 48px;color:var(--muted);font-size:13px}}
.foot .links{{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:14px}}
.foot a{{color:var(--muted);font-weight:600}}.foot a:hover{{color:var(--ink)}}
.note{{font-size:12px;color:var(--muted);margin-top:18px}}
</style></head>
<body{(' data-sport="'+data_sport+'"') if data_sport else ''}{(' data-scope="'+data_scope+'"') if data_scope else ''}>
<header class="top"><div class="wrap">
  <a class="brand" href="/">Line Logic<span class="dot">.</span></a>{_nav()}
</div></header>
<main class="wrap">
<div class="hero"><h1>{h1}</h1>
{body_html}
</div>
</main>
<footer class="foot"><div class="wrap">
  <div class="links">
    {''.join(f'<a href="/{s}-predictions">{l} predictions</a>' for s,l in _SPORTS.items())}
    {''.join(f'<a href="/{s}-player-props">{_SPORTS[s]} props</a>' for s in _PROP_SPORTS)}
    <a href="/model-track-record">Track record</a>
    <a href="{DISCORD}" rel="nofollow">Discord</a>
  </div>
  <div>Line Logic publishes model projections and a public, graded record for
  informational and entertainment purposes. Not financial advice. 21+. If gambling
  stops being fun, call 1-800-GAMBLER.</div>
</div></footer>
<script>
// Pull this page's own live record so the proof is real + always current.
(function(){{
  var el=document.body, sp=el.getAttribute('data-sport'), scope=el.getAttribute('data-scope');
  var box=document.getElementById('rec'); if(!box) return;
  fetch('/api/accuracy?days=3650').then(function(r){{return r.json();}}).then(function(d){{
    var s = (scope==='overall') ? (d.overall||{{}}) : ((d.by_sport||{{}})[sp]||d.overall||{{}});
    if(!s) return;
    var w=s.alltime_wins||0, l=s.alltime_losses||0;
    var u=s.units_at, roi=s.roi_at;
    function cell(id,val,cls){{var n=document.getElementById(id);if(n){{n.textContent=val;if(cls)n.className='n '+cls;}}}}
    cell('r-wl', w+'–'+l, (w>l?'pos':(l>w?'neg':'')));
    cell('r-u', (u==null?'—':(u>=0?'+':'')+u+'u'), (u==null?'':(u>=0?'pos':'neg')));
    cell('r-roi', (roi==null?'—':(roi>=0?'+':'')+roi+'%'), (roi==null?'':(roi>=0?'pos':'neg')));
  }}).catch(function(){{}});
}})();
</script>
</body></html>"""


def _rec_box(label):
    return (
        '<div class="rec" id="rec">'
        '<div class="b"><div class="n" id="r-wl">—</div><div class="k">'+label+' record</div></div>'
        '<div class="b"><div class="n" id="r-u">—</div><div class="k">Units (all-time)</div></div>'
        '<div class="b"><div class="n" id="r-roi">—</div><div class="k">ROI (all-time)</div></div>'
        '<small>Live from the tracker — every graded pick, win or lose.</small>'
        '</div>'
    )


def _faq_jsonld(qas):
    items = ",".join(
        '{"@type":"Question","name":%s,"acceptedAnswer":{"@type":"Answer","text":%s}}'
        % (_j(q), _j(a)) for q, a in qas
    )
    return '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[%s]}' % items


def _j(s):
    # minimal JSON string escaper for embedding in <script type=ld+json>
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _predictions_page(sport):
    lab = _SPORTS[sport]
    blurb = _SPORT_BLURB[sport]
    title = f"{lab} Predictions & Model Picks Today | Line Logic"
    desc = (f"Today's {lab} predictions from a transparent betting model: projected "
            f"spreads, totals and win probabilities vs. the live market — with a public, "
            f"graded track record. Every pick, win or lose.")
    canon = f"{BASE}/{sport}-predictions"
    h1 = f"{lab} Predictions Today — Model vs. the Market"
    qas = [
        (f"How does Line Logic make {lab} predictions?",
         f"Line Logic rates every team, projects each game (spread, total and win "
         f"probability), and compares that projection to the current sportsbook line. "
         f"When the model's number and the market disagree, that gap is the edge. "
         f"Injuries and in-season roster moves are factored in before the line is set."),
        ("Is the track record real?",
         "Yes. Every pick is graded publicly, win or lose — you can see the all-time "
         "record, units and ROI on this page and on the full track-record page. Nothing "
         "is deleted after the fact."),
        (f"Where do I see today's {lab} picks?",
         f"Tap “See today's {lab} board” — it opens the live slate with every game's "
         f"model line, the edge vs. the market, and a confidence-tiered wager (1u / 2u / "
         f"3u). New lines post daily."),
    ]
    jsonld = _faq_jsonld(qas)
    body = f"""<p class="lede">{blurb}</p>
<div class="cta">
  <a class="btn p" href="/#{sport}">See today's {lab} board →</a>
  <a class="btn s" href="{DISCORD}" rel="nofollow">Join the Discord</a>
</div>
{_rec_box(lab)}
<section>
<h2>How the {lab} model works</h2>
<p>{blurb}</p>
<ul>
  <li><b>Model line first.</b> Each {lab} game is projected from team ratings, matchup and situation — before looking at the odds — so the number isn't anchored to the book.</li>
  <li><b>Edge vs. the market.</b> The projection is compared to the live spread, total and moneyline. The disagreement is the edge, and it's shown on every game card.</li>
  <li><b>Injuries &amp; trades baked in.</b> A ruled-out starter re-runs the matchup and can flip the side or the margin; in-season trades update the rosters the model reads.</li>
  <li><b>Confidence-tiered wagers.</b> Every pick carries a 1u (low), 2u (medium) or 3u (high) stake, so you know how strongly the model likes it.</li>
</ul>
<h2>Every pick is graded — win or lose</h2>
<p>The numbers above are live from the tracker. There's no cherry-picking: wins and losses both count toward the all-time record, units and ROI. That's the whole point of Line Logic — receipts, not hype.</p>
<h2>Today's {lab} picks</h2>
<p>The full {lab} slate, with each game's model line, the market comparison and the suggested wager, lives on the board. <a href="/#{sport}">Open today's {lab} board →</a></p>
<h2>{lab} questions</h2>
<div class="faq">
  {''.join(f'<div class="q">{q}</div><p>{a}</p>' for q,a in qas)}
</div>
</section>"""
    return _shell(title=title, desc=desc, canonical=canon, h1=h1, body_html=body,
                  jsonld=jsonld, data_sport=sport, data_scope="sport")


def _props_page(sport):
    lab = _SPORTS[sport]
    title = f"{lab} Player Prop Projections Today | Line Logic"
    desc = (f"Model {lab} player prop projections vs. the posted lines — points, "
            f"yards, assists and more, adjusted for injuries and roster changes. Public, "
            f"graded record. See today's props on the board.")
    canon = f"{BASE}/{sport}-player-props"
    h1 = f"{lab} Player Prop Projections — Model vs. the Line"
    qas = [
        (f"How are {lab} player props projected?",
         f"Each player's prop (points, yards, assists, etc.) is projected from role, "
         f"usage and matchup, then compared to the posted line to find overs and unders "
         f"the market has mispriced."),
        ("What happens to props when a starter is out?",
         "When a starter is ruled out, the model boosts the backups who inherit the "
         "role — more carries, more minutes, more targets — so the prop projections "
         "update automatically instead of using stale baselines."),
        (f"Where are today's {lab} props?",
         f"Open the {lab} board and tap any game — each matchup card lists the player "
         f"props with the model projection next to the line, flagged when a player is "
         f"boosted by an injury or just traded in."),
    ]
    jsonld = _faq_jsonld(qas)
    body = f"""<p class="lede">Every {lab} matchup breaks down to the player level: the model projects
each prop, lines it up against the posted number, and flags the overs and unders where they
disagree — with backups boosted the moment a starter is ruled out.</p>
<div class="cta">
  <a class="btn p" href="/#{sport}">See today's {lab} board →</a>
  <a class="btn s" href="{DISCORD}" rel="nofollow">Join the Discord</a>
</div>
{_rec_box(lab)}
<section>
<h2>How {lab} prop projections work</h2>
<ul>
  <li><b>Role-based projections.</b> Each prop is built from the player's usage and the matchup, not last season's average.</li>
  <li><b>Injury boosts.</b> Starter out? The backup who inherits the carries, minutes or targets gets boosted, and the card marks it “↑ [player] out.”</li>
  <li><b>Trades update rosters.</b> An in-season trade moves the player to the new team before props are projected, flagged “↔ traded in.”</li>
  <li><b>Lined up against the market.</b> Every projection sits next to the posted prop line so the over/under edge is obvious.</li>
</ul>
<h2>Graded like everything else</h2>
<p>Prop results feed the same public record as the game picks — the all-time numbers above update win or lose.</p>
<h2>Today's {lab} props</h2>
<p>Props live inside each game card on the board. <a href="/#{sport}">Open today's {lab} board →</a> and tap a matchup.</p>
<h2>{lab} prop questions</h2>
<div class="faq">
  {''.join(f'<div class="q">{q}</div><p>{a}</p>' for q,a in qas)}
</div>
</section>"""
    return _shell(title=title, desc=desc, canonical=canon, h1=h1, body_html=body,
                  jsonld=jsonld, data_sport=sport, data_scope="sport")


def _track_record_page():
    title = "Model Track Record — Every Pick Graded | Line Logic"
    desc = ("Line Logic's public track record: all-time record, units and ROI across "
            "NFL, NBA, MLB, NHL and more. Every pick graded, win or lose — no deleted "
            "losers, no cherry-picking.")
    canon = f"{BASE}/model-track-record"
    h1 = "The Track Record — Every Pick Graded, Win or Lose"
    qas = [
        ("What does Line Logic track?",
         "Every pick the model makes is logged with the line it was taken at and graded "
         "once the game settles. The record, units and ROI you see are the real totals — "
         "winners and losers both."),
        ("Why does transparency matter?",
         "Most touts show you winners and quietly bury losers. A model is only worth "
         "following if its full record is visible, so Line Logic grades everything in "
         "public and lets the numbers speak."),
        ("How do units work?",
         "Picks are staked 1u (low), 2u (medium) or 3u (high) by the model's confidence. "
         "Units won is the bottom line; ROI is units won per unit risked."),
    ]
    jsonld = _faq_jsonld(qas)
    links = "".join(
        f'<a class="btn s" href="/{s}-predictions">{l} picks</a>' for s, l in _SPORTS.items()
    )
    body = f"""<p class="lede">No cherry-picking and no deleted losers. Every pick the model makes is
graded in public, win or lose. Here's the all-time scoreboard — and it updates live as today's
games settle.</p>
<div class="cta">
  <a class="btn p" href="/">Open the live board →</a>
  <a class="btn s" href="{DISCORD}" rel="nofollow">Join the Discord</a>
</div>
{_rec_box("All-time")}
<section>
<h2>Why this page exists</h2>
<p>Anyone can post a hot take. A model is only worth following if you can see its whole record —
so we show it. The numbers above are pulled live from the tracker across every sport.</p>
<h2>How to read it</h2>
<ul>
  <li><b>Record</b> — all-time wins–losses across every graded pick.</li>
  <li><b>Units</b> — net units won, staked 1u / 2u / 3u by the model's confidence.</li>
  <li><b>ROI</b> — units won per unit risked.</li>
</ul>
<h2>See it by sport</h2>
<div class="cta">{links}</div>
<h2>Track-record questions</h2>
<div class="faq">
  {''.join(f'<div class="q">{q}</div><p>{a}</p>' for q,a in qas)}
</div>
</section>"""
    return _shell(title=title, desc=desc, canonical=canon, h1=h1, body_html=body,
                  jsonld=jsonld, data_sport="", data_scope="overall")


# ---- routes -----------------------------------------------------------------
_CACHE = {"Cache-Control": "public, max-age=300"}


def _html(s):
    return HTMLResponse(content=s, headers=_CACHE)


for _s in _SPORTS:
    def _mk_pred(s=_s):
        def _r():
            return _html(_predictions_page(s))
        return _r
    router.add_api_route(f"/{_s}-predictions", _mk_pred(), methods=["GET"], include_in_schema=False)

for _s in _PROP_SPORTS:
    def _mk_prop(s=_s):
        def _r():
            return _html(_props_page(s))
        return _r
    router.add_api_route(f"/{_s}-player-props", _mk_prop(), methods=["GET"], include_in_schema=False)


@router.get("/model-track-record", include_in_schema=False)
def _track():
    return _html(_track_record_page())


@router.get("/sitemap.xml", include_in_schema=False)
def _sitemap():
    urls = [BASE + "/"]
    urls += [f"{BASE}/{s}-predictions" for s in _SPORTS]
    urls += [f"{BASE}/{s}-player-props" for s in _PROP_SPORTS]
    urls += [BASE + "/model-track-record", BASE + "/portal"]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(
            f"<url><loc>{u}</loc><changefreq>daily</changefreq>"
            f"<priority>{'1.0' if u.endswith('/') else '0.8'}</priority></url>"
            for u in urls
        )
        + "</urlset>"
    )
    return Response(content=body, media_type="application/xml", headers=_CACHE)


@router.get("/robots.txt", include_in_schema=False)
def _robots():
    body = f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain", headers=_CACHE)
