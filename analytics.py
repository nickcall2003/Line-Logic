"""
analytics.py
------------
First-party, privacy-safe page analytics — no third-party account, no cookies,
no IP logging. A tiny beacon on each page POSTs its path + referrer to /api/hit;
we store only our own path, the referrer HOST (not the full URL), and a day
bucket. /stats is a small owned dashboard that reads it back.

Why first-party: Nick wanted to SEE traffic without wiring up Google Analytics
(account setup, cookie banner). He already has FastAPI + Postgres, so this gives
him an analytics view he fully owns, live the moment it deploys. The PageView
table auto-creates via init_db()'s create_all.

Optional lock: set env STATS_KEY=<something> and the dashboard/API require
?k=<something>. Unset = open (data isn't sensitive — just view counts).

Register in main.py:  from analytics import router as analytics_router; app.include_router(analytics_router)
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func

from db import SessionLocal
from models import PageView

router = APIRouter()

SELF_HOSTS = {"thelinelogic.com", "www.thelinelogic.com"}
_BOT = ("bot", "crawl", "spider", "slurp", "preview", "headless", "monitor",
        "pingdom", "uptime", "curl", "wget", "python-requests", "facebookexternalhit",
        "embedly", "quora link", "whatsapp", "telegrambot", "discordbot")
# NB: discordbot/whatsapp/facebookexternalhit are link-unfurlers, not real
# visitors — they'd inflate counts every time a link is pasted, so we drop them.


def _is_bot(ua: str) -> bool:
    ua = (ua or "").lower()
    if not ua:
        return True
    return any(b in ua for b in _BOT)


def _ref_host(referrer: str) -> str:
    """Referrer HOST only (privacy: never store the full URL/query)."""
    if not referrer:
        return "direct"
    try:
        h = (urlparse(referrer).hostname or "").lower()
    except Exception:
        return "direct"
    if not h:
        return "direct"
    if h in SELF_HOSTS or h.endswith(".thelinelogic.com"):
        return "internal"
    if h.startswith("www."):
        h = h[4:]
    return h[:60]


@router.post("/api/hit", include_in_schema=False)
async def hit(request: Request):
    # Never error the client; a failed beacon must not show up as a console error.
    try:
        if _is_bot(request.headers.get("user-agent", "")):
            return Response(status_code=204)
        raw = await request.body()
        data = {}
        if raw:
            try:
                data = _json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                data = {}
        path = (data.get("p") or "/")[:80]
        if not path.startswith("/"):
            path = "/" + path
        path = path.split("?")[0].split("#")[0]  # strip any query/hash defensively
        ref = _ref_host(data.get("r") or "")
        now = _dt.datetime.utcnow()
        with SessionLocal() as db:
            db.add(PageView(ts=now, day=now.strftime("%Y-%m-%d"), path=path, ref=ref))
            db.commit()
    except Exception:
        pass
    return Response(status_code=204)


def _gate(request: Request):
    """Return None if allowed, else a 401 response."""
    key = os.environ.get("STATS_KEY", "").strip()
    if not key:
        return None
    if request.query_params.get("k", "") == key:
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@router.get("/api/stats", include_in_schema=False)
def stats(request: Request, days: int = 30):
    g = _gate(request)
    if g is not None:
        return g
    days = max(1, min(int(days or 30), 365))
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out = {"days": days, "total": 0, "by_path": [], "by_ref": [], "by_day": []}
    try:
        with SessionLocal() as db:
            q = db.query(PageView).filter(PageView.day >= since)
            out["total"] = q.count()
            out["by_path"] = [
                {"path": p, "views": v} for p, v in
                db.query(PageView.path, func.count(PageView.id))
                  .filter(PageView.day >= since)
                  .group_by(PageView.path).order_by(func.count(PageView.id).desc())
                  .limit(50).all()
            ]
            out["by_ref"] = [
                {"ref": r, "views": v} for r, v in
                db.query(PageView.ref, func.count(PageView.id))
                  .filter(PageView.day >= since)
                  .group_by(PageView.ref).order_by(func.count(PageView.id).desc())
                  .limit(30).all()
            ]
            out["by_day"] = [
                {"day": d, "views": v} for d, v in
                db.query(PageView.day, func.count(PageView.id))
                  .filter(PageView.day >= since)
                  .group_by(PageView.day).order_by(PageView.day.asc()).all()
            ]
    except Exception as e:
        out["error"] = str(e)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


_DASH = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Line Logic — Traffic</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Hanken+Grotesque:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#111211;--panel:#171817;--panel-2:#1E1F1D;--line:#2A2B29;--ink:#EDEBE6;--muted:#8E8F8A;--gold:#E8B04B;--win:#5FCF86}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(130% 80% at 82% -8%,#1a1710 0%,var(--bg) 55%);background-attachment:fixed;color:var(--ink);font:15px/1.5 "Hanken Grotesque",system-ui,sans-serif;min-height:100vh}
.wrap{max-width:820px;margin:0 auto;padding:26px 18px 60px}
h1{font-family:"Archivo",sans-serif;font-weight:800;letter-spacing:-.02em;font-size:26px;margin:0 0 4px}
h1 .d{color:var(--gold)}
.sub{color:var(--muted);font-family:"Space Mono",monospace;font-size:12px;margin-bottom:20px}
.ctrls{display:flex;gap:8px;margin-bottom:18px}
.ctrls a{font-family:"Archivo",sans-serif;font-weight:700;font-size:13px;color:var(--muted);text-decoration:none;padding:7px 12px;border:1px solid var(--line);border-radius:9px;background:var(--panel-2)}
.ctrls a.on{color:#1a1407;background:var(--gold);border-color:var(--gold)}
.big{font-family:"Archivo",sans-serif;font-weight:900;font-size:40px;letter-spacing:-.02em}
.big .u{font-size:14px;color:var(--muted);font-family:"Space Mono",monospace;font-weight:400;margin-left:8px}
.spark{display:flex;align-items:flex-end;gap:3px;height:70px;margin:14px 0 26px}
.spark .bar{flex:1;background:linear-gradient(180deg,var(--gold),#8a6a24);border-radius:3px 3px 0 0;min-height:2px;position:relative}
.spark .bar span{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);font:9px "Space Mono",monospace;color:var(--muted);white-space:nowrap;opacity:0;margin-bottom:3px}
.spark .bar:hover span{opacity:1}
h2{font-family:"Archivo",sans-serif;font-weight:800;font-size:16px;margin:24px 0 8px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
td,th{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13px}
th{font-family:"Space Mono",monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
tr:last-child td{border-bottom:0}
td.v{text-align:right;font-family:"Space Mono",monospace;font-weight:700;color:var(--win)}
a.path{color:var(--gold);text-decoration:none}
.empty{color:var(--muted);font-family:"Space Mono",monospace;font-size:12px;padding:20px 0}
</style></head><body><div class="wrap">
<h1>Line Logic<span class="d"> traffic.</span></h1>
<div class="sub" id="sub">loading…</div>
<div class="ctrls" id="ctrls"></div>
<div class="big" id="total">—<span class="u">views</span></div>
<div class="spark" id="spark"></div>
<h2>Top pages</h2><div id="paths"></div>
<h2>Where they came from</h2><div id="refs"></div>
</div>
<script>
var K=new URLSearchParams(location.search).get('k')||'';
var DAYS=parseInt(new URLSearchParams(location.search).get('days')||'30',10);
function qs(d){return '/api/stats?days='+d+(K?'&k='+encodeURIComponent(K):'');}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function ctrls(){var opt=[7,30,90],h='';opt.forEach(function(d){h+='<a href="?days='+d+(K?'&k='+encodeURIComponent(K):'')+'"'+(d===DAYS?' class="on"':'')+'>'+d+'d</a>';});document.getElementById('ctrls').innerHTML=h;}
function table(host,rows,keyName,label){
  var el=document.getElementById(host);
  if(!rows||!rows.length){el.innerHTML='<div class="empty">No data yet — views show up here as people visit.</div>';return;}
  var h='<table><tr><th>'+label+'</th><th style="text-align:right">Views</th></tr>';
  rows.forEach(function(r){
    var k=r[keyName];
    var cell=(host==='paths')?'<a class="path" href="'+esc(k)+'" target="_blank" rel="noopener">'+esc(k)+'</a>':esc(k);
    h+='<tr><td>'+cell+'</td><td class="v">'+r.views+'</td></tr>';
  });
  el.innerHTML=h+'</table>';
}
ctrls();
fetch(qs(DAYS)).then(function(r){return r.json();}).then(function(d){
  if(d.error){document.getElementById('sub').textContent=d.error;return;}
  document.getElementById('sub').textContent='last '+d.days+' days · updated '+new Date().toLocaleString();
  document.getElementById('total').innerHTML=(d.total||0).toLocaleString()+'<span class="u">views</span>';
  var days=d.by_day||[],max=1;days.forEach(function(x){if(x.views>max)max=x.views;});
  document.getElementById('spark').innerHTML=days.map(function(x){
    var h=Math.round(x.views/max*100);return '<div class="bar" style="height:'+h+'%"><span>'+x.day.slice(5)+': '+x.views+'</span></div>';
  }).join('')||'<div class="empty">No days yet.</div>';
  table('paths',d.by_path,'path','Page');
  table('refs',d.by_ref,'ref','Referrer');
}).catch(function(e){document.getElementById('sub').textContent='failed to load: '+e;});
</script></body></html>"""


@router.get("/stats", include_in_schema=False)
def stats_dashboard(request: Request):
    g = _gate(request)
    if g is not None:
        return HTMLResponse("<h3 style='font-family:sans-serif'>Unauthorized — add ?k=YOUR_KEY</h3>",
                            status_code=401)
    return HTMLResponse(_DASH, headers={"Cache-Control": "no-store"})
