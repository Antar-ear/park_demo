"""
FastAPI app.   Run from the project root:

    python -m dp_platform          (or)     uvicorn dp_platform.main:app
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, create_model

import drawing_common as dc
from excel_export import build_workbook

from . import auth, config, db, pipeline, worker

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    auth.seed_demo_user()
    worker.start()
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# --- pages ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def page_index():
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
def page_login():
    return FileResponse(STATIC / "login.html")


@app.get("/batch/{batch_id}", include_in_schema=False)
def page_batch(batch_id: int):
    return FileResponse(STATIC / "batch.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# --- auth ----------------------------------------------------------------------------
class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    user = auth.authenticate(body.username.strip(), body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    response.set_cookie(auth.COOKIE, auth.make_token(user["id"]), httponly=True,
                        samesite="lax", max_age=config.SESSION_HOURS * 3600)
    return {"username": user["username"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(auth.require_user)):
    return {"username": user["username"], "app_name": config.APP_NAME,
            "tagline": config.APP_TAGLINE, "brand_color": config.BRAND_COLOR}


@app.get("/api/branding")
def branding():
    """Public: the login page needs the name and colour before anyone is signed in."""
    return {"app_name": config.APP_NAME, "tagline": config.APP_TAGLINE,
            "brand_color": config.BRAND_COLOR}


# --- batches -------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    """Display name only. Files are stored under a uuid, never under this."""
    name = Path(name.replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f]", "", name)[:200] or "drawing.pdf"


@app.post("/api/batches")
async def create_batch(files: List[UploadFile] = File(...), user=Depends(auth.require_user)):
    if len(files) > config.MAX_FILES_PER_BATCH:
        raise HTTPException(400, "Too many files (max %d per batch)" % config.MAX_FILES_PER_BATCH)

    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    accepted, rejected = [], []
    for f in files:
        name = _safe_name(f.filename or "")
        data = await f.read(limit + 1)
        if len(data) > limit:
            rejected.append({"filename": name, "reason": "larger than %d MB" % config.MAX_UPLOAD_MB})
        elif not data.startswith(b"%PDF-"):
            rejected.append({"filename": name, "reason": "not a PDF"})
        else:
            accepted.append((name, data))
    if not accepted:
        raise HTTPException(400, "No valid PDF files in the upload: %s"
                            % "; ".join("%s (%s)" % (r["filename"], r["reason"]) for r in rejected))

    batch_id = db.create_batch(user["id"], "Batch " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    folder = config.UPLOAD_DIR / str(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    ids = []
    for name, data in accepted:
        stored = folder / ("%s.pdf" % uuid.uuid4().hex)
        stored.write_bytes(data)
        ids.append(db.add_drawing(batch_id, name, str(stored)))
    for drawing_id in ids:            # enqueue only after every row exists
        worker.enqueue(drawing_id)
    return {"batch_id": batch_id, "accepted": len(accepted), "rejected": rejected}


@app.get("/api/batches")
def list_batches(user=Depends(auth.require_user)):
    return db.list_batches(user["id"])


def _owned_batch(batch_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    batch = db.get_batch(batch_id, user["id"])
    if batch is None:
        raise HTTPException(404, "Batch not found")      # 404, not 403: don't confirm it exists
    return batch


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, user=Depends(auth.require_user)):
    batch = _owned_batch(batch_id, user)
    rows = db.batch_drawings(batch_id)
    counts = {"DEFENSE": 0, "NON_DEFENSE": 0, "NEEDS_REVIEW": 0, "FAILED": 0}
    for r in rows:
        if r["state"] == "done" and r["status"] in counts:
            counts[r["status"]] += 1
    done = sum(1 for r in rows if r["state"] == "done")
    return {"batch": batch, "drawings": rows, "counts": counts,
            "total": len(rows), "done": done, "finished": done == len(rows)}


@app.get("/api/batches/{batch_id}/export.xlsx")
def export_batch(batch_id: int, user=Depends(auth.require_user)):
    _owned_batch(batch_id, user)
    rows = []
    for d in db.batch_drawings(batch_id):
        if d["state"] != "done":
            continue
        row = {"Filename": d["filename"], "Status": d["status"], "Reason": d["reason"],
               "Classification Reason": d["reason"], "Extraction Method": d["method"] or "",
               "Date Processed": d["processed_at"] or "",
               "Manually Edited": ", ".join(dc.FIELD_LABELS[k] for k in d["edited_fields"]
                                            if k in dc.FIELD_LABELS)}
        for key in dc.FIELD_ORDER:
            row[dc.FIELD_LABELS[key]] = d[key]
        rows.append(row)
    name = "Extraction_Summary_%s.xlsx" % datetime.now().strftime("%Y-%m-%d")
    return Response(build_workbook(rows),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})


# --- drawings ------------------------------------------------------------------------
def _owned_drawing(drawing_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    d = db.get_drawing_for_user(drawing_id, user["id"])
    if d is None:
        raise HTTPException(404, "Drawing not found")
    return d


@app.get("/api/drawings/{drawing_id}/pdf")
def drawing_pdf(drawing_id: int, user=Depends(auth.require_user)):
    d = _owned_drawing(drawing_id, user)
    path = Path(d["stored_path"])
    if not path.is_file():
        raise HTTPException(404, "File is no longer on disk")
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.get("/api/drawings/{drawing_id}/preview.png")
def drawing_preview(drawing_id: int, user=Depends(auth.require_user)):
    d = _owned_drawing(drawing_id, user)
    png = pipeline.preview_path(Path(d["stored_path"]))
    if not png.is_file():
        raise HTTPException(404, "No preview available")
    return FileResponse(png, media_type="image/png")


# Built from dc.FIELD_ORDER rather than listed by hand, so a field added to the
# extraction engine doesn't also need updating here to keep accepting edits to it.
FieldEdit = create_model("FieldEdit", **{f: (str, ...) for f in dc.FIELD_ORDER})


@app.patch("/api/drawings/{drawing_id}")
def edit_drawing(drawing_id: int, body: FieldEdit, user=Depends(auth.require_user)):
    d = _owned_drawing(drawing_id, user)
    if d["state"] != "done" or d["status"] == "FAILED":
        raise HTTPException(409, "This drawing has no extracted text to correct")

    new = {k: getattr(body, k).strip()[:dc.MAX_FIELD_LEN] for k in dc.FIELD_ORDER}
    edited = set(d["edited_fields"])
    edited.update(k for k in dc.FIELD_ORDER if new[k] != d[k])
    status, reason = pipeline.reroute(d["full_text"], new)
    if status == "FAILED":                       # cannot happen for ok=True, but never store it
        raise HTTPException(400, "Could not classify with these values")
    db.save_edit(drawing_id, new, status, reason, sorted(edited))
    return {"status": status, "reason": reason}


# Static assets last, so they never shadow an /api route.
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
