import io
import uuid
import random
import shutil
import logging
import zipfile
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Cookie, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.exceptions import HTTPException

from config import (
    UPLOAD_DIR, MAX_IMAGES_PER_GROUP, MAX_FILE_SIZE,
    IMAGE_EXPIRY_HOURS, ALLOWED_MIME_TYPES, ALLOWED_EXTENSIONS,
    SHORT_CODE_CHARS, SHORT_CODE_LENGTH, SITE_URL, SITE_NAME,
)
from database import init_db, get_db
from scheduler import start_scheduler, cleanup_expired

logger = logging.getLogger(__name__)
MAX_GROUPS_PER_SESSION = 3


# ─── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_expired()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")
templates.env.globals["site_url"] = SITE_URL
templates.env.globals["site_name"] = SITE_NAME


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _unique_code(conn) -> str:
    for _ in range(20):
        code = "".join(random.choices(SHORT_CODE_CHARS, k=SHORT_CODE_LENGTH))
        if not conn.execute(
            "SELECT 1 FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone():
            return code
    raise RuntimeError("Could not generate unique short code")


async def _validate_files(files: list[UploadFile], max_allowed: int = 5):
    """Returns (validated_list, error_str). validated_list = list of (stored_name, original, mime, bytes)."""
    if not files or (len(files) == 1 and not files[0].filename):
        return None, "No images selected."
    if len(files) > max_allowed:
        return None, f"Maximum {max_allowed} image(s) allowed per upload."

    validated = []
    for f in files:
        if f.content_type not in ALLOWED_MIME_TYPES:
            return None, f"'{f.filename}' is not supported. Allowed: JPG, PNG, GIF, WebP."
        content = await f.read()
        if len(content) == 0:
            return None, f"'{f.filename}' is empty."
        if len(content) > MAX_FILE_SIZE:
            return None, f"'{f.filename}' exceeds the 10 MB limit."
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".jpg"
        stored = f"{uuid.uuid4().hex}{ext}"
        validated.append((stored, f.filename, f.content_type, content))
    return validated, None


def _active_images(conn, group_id: str) -> list[dict]:
    """Return active (non-expired) images for a group, with minutes_left."""
    now = datetime.utcnow()
    rows = conn.execute(
        "SELECT * FROM images WHERE group_id = ? AND expires_at > datetime('now') ORDER BY id",
        (group_id,),
    ).fetchall()
    result = []
    for r in rows:
        exp = datetime.fromisoformat(r["expires_at"])
        mins = max(0, int((exp - now).total_seconds() / 60))
        result.append({**dict(r), "minutes_left": mins})
    return result


def _is_owner(conn, session_id: str | None, group_id: str) -> bool:
    if not session_id:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM session_groups WHERE session_id = ? AND group_id = ?",
        (session_id, group_id),
    ).fetchone())


def _err_partial(request: Request, message: str, retarget: str | None = None):
    resp = templates.TemplateResponse(request, "partials/error.html", {"message": message})
    if retarget:
        resp.headers["HX-Retarget"] = retarget
        resp.headers["HX-Reswap"] = "innerHTML"
    return resp


def _active_group_count(conn, session_id: str) -> int:
    return conn.execute("""
        SELECT COUNT(*) AS cnt FROM session_groups sg
        JOIN image_groups g ON sg.group_id = g.id
        WHERE sg.session_id = ?
          AND EXISTS (
              SELECT 1 FROM images i
              WHERE i.group_id = g.id AND i.expires_at > datetime('now')
          )
    """, (session_id,)).fetchone()["cnt"]


# ─── Static files ──────────────────────────────────────────────────────────────

@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return FileResponse("robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    return FileResponse("sitemap.xml", media_type="application/xml")


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/s/{code}", response_class=HTMLResponse)
async def share_view(request: Request, code: str, img_session: str = Cookie(default=None)):
    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()

        if not group:
            return templates.TemplateResponse(request, "404.html", {}, status_code=404)

        images = _active_images(conn, group["id"])

        if not images:
            # Group exists but all images expired
            return templates.TemplateResponse(request, "404.html", {}, status_code=404)

        owner = _is_owner(conn, img_session, group["id"])
        can_add = owner and len(images) < MAX_IMAGES_PER_GROUP

        min_exp = min(i["expires_at"] for i in images)
        first_img_url = f"{SITE_URL}/uploads/{group['id']}/{images[0]['filename']}"

    return templates.TemplateResponse(request, "share.html", {
        "group": group,
        "images": images,
        "group_id": group["id"],
        "code": code,
        "is_owner": owner,
        "can_add": can_add,
        "min_expires_at": min_exp,
        "first_img_url": first_img_url,
    })


# ─── Downloads ────────────────────────────────────────────────────────────────

@app.get("/s/{code}/download")
async def download_all(code: str):
    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()
        if not group:
            raise HTTPException(404)

        images = conn.execute(
            "SELECT * FROM images WHERE group_id = ? AND expires_at > datetime('now') ORDER BY id",
            (group["id"],),
        ).fetchall()

    if not images:
        raise HTTPException(404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for img in images:
            fp = UPLOAD_DIR / group["id"] / img["filename"]
            if fp.exists():
                zf.write(str(fp), img["original_name"])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="imageshare-{code}.zip"'},
    )


# ─── HTMX: session check ──────────────────────────────────────────────────────

@app.get("/session-check", response_class=HTMLResponse)
async def session_check(request: Request, img_session: str = Cookie(default=None)):
    if not img_session:
        return HTMLResponse("")

    with get_db() as conn:
        groups = conn.execute("""
            SELECT g.id, g.short_code
            FROM session_groups sg
            JOIN image_groups g ON sg.group_id = g.id
            WHERE sg.session_id = ?
              AND EXISTS (
                  SELECT 1 FROM images i
                  WHERE i.group_id = g.id AND i.expires_at > datetime('now')
              )
            ORDER BY sg.added_at DESC
            LIMIT 3
        """, (img_session,)).fetchall()

        if not groups:
            return HTMLResponse("")

        groups_data = []
        for g in groups:
            min_exp = conn.execute(
                "SELECT MIN(expires_at) AS v FROM images WHERE group_id = ? AND expires_at > datetime('now')",
                (g["id"],),
            ).fetchone()["v"]

            thumbs = conn.execute(
                "SELECT filename FROM images WHERE group_id = ? AND expires_at > datetime('now') ORDER BY id LIMIT 4",
                (g["id"],),
            ).fetchall()

            img_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM images WHERE group_id = ? AND expires_at > datetime('now')",
                (g["id"],),
            ).fetchone()["cnt"]

            groups_data.append({
                "id": g["id"],
                "short_code": g["short_code"],
                "min_expires_at": min_exp,
                "thumbs": [t["filename"] for t in thumbs],
                "img_count": img_count,
            })

    return templates.TemplateResponse(request, "partials/session_banner.html", {
        "groups": groups_data,
        "total": len(groups_data),
    })


# ─── HTMX: new group upload ───────────────────────────────────────────────────

@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    img_session: str = Cookie(default=None),
):
    session_id = img_session or str(uuid.uuid4())

    validated, err = await _validate_files(files, max_allowed=MAX_IMAGES_PER_GROUP)
    if err:
        return _err_partial(request, err, retarget="#error-area")

    with get_db() as conn:
        active = _active_group_count(conn, session_id)
        if active >= MAX_GROUPS_PER_SESSION:
            return _err_partial(
                request,
                f"You already have {MAX_GROUPS_PER_SESSION} active groups (maximum). "
                "Delete one or wait for it to expire.",
                retarget="#error-area",
            )

        group_id = str(uuid.uuid4())
        now = datetime.utcnow()
        expires_at = now + timedelta(hours=IMAGE_EXPIRY_HOURS)
        exp_str = expires_at.strftime("%Y-%m-%dT%H:%M:%S")

        group_dir = UPLOAD_DIR / group_id
        group_dir.mkdir(parents=True, exist_ok=True)

        try:
            saved = []
            for stored, original, mime, content in validated:
                (group_dir / stored).write_bytes(content)
                saved.append((stored, original, mime, len(content)))
        except Exception:
            shutil.rmtree(group_dir, ignore_errors=True)
            return _err_partial(request, "Upload failed. Please try again.", retarget="#error-area")

        short_code = _unique_code(conn)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

        conn.execute(
            "INSERT INTO image_groups (id, short_code, created_at, expires_at, image_count) VALUES (?,?,?,?,?)",
            (group_id, short_code, now_str, exp_str, len(saved)),
        )
        conn.executemany(
            "INSERT INTO images (group_id, filename, original_name, mime_type, file_size, uploaded_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(group_id, s[0], s[1], s[2], s[3], now_str, exp_str) for s in saved],
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_sessions (session_id, last_seen) VALUES (?,?)",
            (session_id, now_str),
        )
        conn.execute(
            "INSERT OR IGNORE INTO session_groups (session_id, group_id, added_at) VALUES (?,?,?)",
            (session_id, group_id, now_str),
        )

    resp = templates.TemplateResponse(request, "partials/upload_result.html", {
        "short_code": short_code,
        "expires_at": exp_str,
        "images": [{"filename": s[0], "original_name": s[1]} for s in saved],
        "group_id": group_id,
        "image_count": len(saved),
    })
    resp.set_cookie(
        "img_session", session_id,
        max_age=86400 * 30, httponly=True, samesite="lax", secure=True,
    )
    return resp


# ─── HTMX: add images to existing group ───────────────────────────────────────

@app.post("/s/{code}/add-images", response_class=HTMLResponse)
async def add_images(
    request: Request,
    code: str,
    files: list[UploadFile] = File(...),
    img_session: str = Cookie(default=None),
):
    if not img_session:
        raise HTTPException(403)

    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()
        if not group or not _is_owner(conn, img_session, group["id"]):
            raise HTTPException(403)

        current_images = _active_images(conn, group["id"])
        current_count = len(current_images)

        if current_count >= MAX_IMAGES_PER_GROUP:
            return templates.TemplateResponse(request, "partials/gallery_partial.html", {
                "images": current_images,
                "group_id": group["id"],
                "code": code,
                "is_owner": True,
                "can_add": False,
                "error": f"This group already has {MAX_IMAGES_PER_GROUP} images (maximum).",
            })

        max_new = MAX_IMAGES_PER_GROUP - current_count
        validated, err = await _validate_files(files, max_allowed=max_new)
        if err:
            return templates.TemplateResponse(request, "partials/gallery_partial.html", {
                "images": current_images,
                "group_id": group["id"],
                "code": code,
                "is_owner": True,
                "can_add": True,
                "error": err,
            })

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=IMAGE_EXPIRY_HOURS)
        exp_str = expires_at.strftime("%Y-%m-%dT%H:%M:%S")
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

        group_dir = UPLOAD_DIR / group["id"]
        group_dir.mkdir(parents=True, exist_ok=True)

        try:
            for stored, original, mime, content in validated:
                (group_dir / stored).write_bytes(content)
                conn.execute(
                    "INSERT INTO images (group_id, filename, original_name, mime_type, file_size, uploaded_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (group["id"], stored, original, mime, len(content), now_str, exp_str),
                )
        except Exception:
            return templates.TemplateResponse(request, "partials/gallery_partial.html", {
                "images": current_images,
                "group_id": group["id"],
                "code": code,
                "is_owner": True,
                "can_add": True,
                "error": "Failed to save images. Please try again.",
            })

        # Update group metadata
        conn.execute("""
            UPDATE image_groups SET
                image_count = (SELECT COUNT(*) FROM images WHERE group_id = ?),
                expires_at  = (SELECT MAX(expires_at) FROM images WHERE group_id = ?)
        """, (group["id"], group["id"]))

        updated_images = _active_images(conn, group["id"])
        can_add = len(updated_images) < MAX_IMAGES_PER_GROUP

    return templates.TemplateResponse(request, "partials/gallery_partial.html", {
        "images": updated_images,
        "group_id": group["id"],
        "code": code,
        "is_owner": True,
        "can_add": can_add,
    })


# ─── HTMX: delete group from session banner (returns refreshed banner) ────────

@app.post("/s/{code}/delete-from-banner", response_class=HTMLResponse)
async def delete_group_from_banner(request: Request, code: str, img_session: str = Cookie(default=None)):
    if not img_session:
        raise HTTPException(403)

    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()
        if not group or not _is_owner(conn, img_session, group["id"]):
            raise HTTPException(403)

        group_dir = UPLOAD_DIR / group["id"]
        if group_dir.exists():
            shutil.rmtree(group_dir, ignore_errors=True)
        conn.execute("DELETE FROM image_groups WHERE id = ?", (group["id"],))

        # Return refreshed session banner
        groups = conn.execute("""
            SELECT g.id, g.short_code
            FROM session_groups sg
            JOIN image_groups g ON sg.group_id = g.id
            WHERE sg.session_id = ?
              AND EXISTS (
                  SELECT 1 FROM images i
                  WHERE i.group_id = g.id AND i.expires_at > datetime('now')
              )
            ORDER BY sg.added_at DESC
            LIMIT 3
        """, (img_session,)).fetchall()

        if not groups:
            return HTMLResponse("")

        groups_data = []
        for g in groups:
            min_exp = conn.execute(
                "SELECT MIN(expires_at) AS v FROM images WHERE group_id = ? AND expires_at > datetime('now')",
                (g["id"],),
            ).fetchone()["v"]
            thumbs = conn.execute(
                "SELECT filename FROM images WHERE group_id = ? AND expires_at > datetime('now') ORDER BY id LIMIT 4",
                (g["id"],),
            ).fetchall()
            img_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM images WHERE group_id = ? AND expires_at > datetime('now')",
                (g["id"],),
            ).fetchone()["cnt"]
            groups_data.append({
                "id": g["id"],
                "short_code": g["short_code"],
                "min_expires_at": min_exp,
                "thumbs": [t["filename"] for t in thumbs],
                "img_count": img_count,
            })

    return templates.TemplateResponse(request, "partials/session_banner.html", {
        "groups": groups_data,
        "total": len(groups_data),
    })


# ─── HTMX: delete whole group ─────────────────────────────────────────────────

@app.post("/s/{code}/delete", response_class=HTMLResponse)
async def delete_group(request: Request, code: str, img_session: str = Cookie(default=None)):
    if not img_session:
        raise HTTPException(403)

    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()
        if not group or not _is_owner(conn, img_session, group["id"]):
            raise HTTPException(403)

        group_dir = UPLOAD_DIR / group["id"]
        if group_dir.exists():
            shutil.rmtree(group_dir, ignore_errors=True)

        conn.execute("DELETE FROM image_groups WHERE id = ?", (group["id"],))

    resp = Response("", status_code=200)
    resp.headers["HX-Redirect"] = "/"
    return resp


# ─── HTMX: delete single image ────────────────────────────────────────────────

@app.post("/s/{code}/image/{image_id}/delete", response_class=HTMLResponse)
async def delete_image(
    request: Request,
    code: str,
    image_id: int,
    img_session: str = Cookie(default=None),
):
    if not img_session:
        raise HTTPException(403)

    redirect_home = False
    gallery_ctx: dict | None = None

    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone()
        if not group or not _is_owner(conn, img_session, group["id"]):
            raise HTTPException(403)

        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND group_id = ?",
            (image_id, group["id"]),
        ).fetchone()
        if not img:
            raise HTTPException(404)

        fp = UPLOAD_DIR / group["id"] / img["filename"]
        try:
            if fp.exists():
                fp.unlink()
        except OSError:
            pass
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))

        remaining = _active_images(conn, group["id"])

        if not remaining:
            group_dir = UPLOAD_DIR / group["id"]
            if group_dir.exists():
                shutil.rmtree(group_dir, ignore_errors=True)
            conn.execute("DELETE FROM image_groups WHERE id = ?", (group["id"],))
            redirect_home = True
        else:
            conn.execute("""
                UPDATE image_groups SET
                    image_count = ?,
                    expires_at  = (SELECT MAX(expires_at) FROM images WHERE group_id = ?)
            """, (len(remaining), group["id"]))
            gallery_ctx = {
                "images": remaining,
                "group_id": group["id"],
                "code": code,
                "is_owner": True,
                "can_add": len(remaining) < MAX_IMAGES_PER_GROUP,
            }

    if redirect_home:
        resp = Response("", status_code=200)
        resp.headers["HX-Redirect"] = "/"
        return resp

    return templates.TemplateResponse(request, "partials/gallery_partial.html", gallery_ctx)


# ─── Error handler ────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
