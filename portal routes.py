"""
portal_routes.py — the FBS transfer-portal board.

Restores thelinelogic.com/portal:
  GET /portal                      -> the board page (portal.html)
  GET /api/portal                  -> cached board JSON (built data)
  GET /api/portal/status           -> is it built? season / counts / age
  GET /api/portal/build?confirm=yes&year=2026
                                   -> rebuild from CFBD (needs CFBD_API_KEY)

Build writes to the /data volume so the board survives redeploys, exactly like
the NCAAF SP+ refresh. The page itself is served with no-cache so a deploy
always reaches the browser.
"""
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

_DATA = "/data/cfb_portal.json"


@router.get("/portal")
@router.head("/portal")
def portal_page():
    return FileResponse("portal.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


@router.get("/api/portal")
def portal_data():
    import cfb_portal
    cfb_portal.OUT = _DATA
    data = cfb_portal.load()
    if not data:
        return JSONResponse(
            {"error": "not built yet",
             "tip": "append ?confirm=yes to /api/portal/build to pull from CFBD"},
            status_code=404)
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=1800"})


@router.get("/api/portal/status")
def portal_status():
    import cfb_portal
    cfb_portal.OUT = _DATA
    data = cfb_portal.load()
    if not data:
        return {"built": False, "tip": "run /api/portal/build?confirm=yes"}
    return {"built": True, "season": data.get("season"),
            "ranked": data.get("ranked"), "entries": data.get("count_entries"),
            "built_at": data.get("built_at")}


@router.get("/api/portal/build")
def portal_build(confirm: str = "", year: int = 0):
    if confirm != "yes":
        return JSONResponse({"note": "append ?confirm=yes (optional &year=2026) "
                             "to rebuild the transfer-portal board from CFBD"})
    import cfb_portal
    cfb_portal.OUT = _DATA
    try:
        data = cfb_portal.build(year or None)
    except SystemExit as e:
        return JSONResponse({"error": str(e), "tip": "set CFBD_API_KEY on Railway"},
                            status_code=400)
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-800:]}, status_code=500)
    return JSONResponse({"status": "done", "season": data.get("season"),
                         "ranked": data.get("ranked"),
                         "entries": data.get("count_entries"),
                         "diag": data.get("diag"),
                         "top": [{"rank": t["rank"], "team": t["team"],
                                  "net": t["net"]} for t in data["teams"][:10]]})
