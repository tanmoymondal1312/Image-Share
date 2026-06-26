import io
import uuid
import random
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


def _unique_code(conn) -> str:
    for _ in range(20):
        code = "".join(random.choices(SHORT_CODE_CHARS, k=SHORT_CODE_LENGTH))
        if not conn.execute(
            "SELECT 1 FROM image_groups WHERE short_code = ?", (code,)
        ).fetchone():
            return code
    raise RuntimeError("Could not generate unique short code")


# ─── Static files ──────────────────────────────────────────────────────────────

@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return FileResponse("robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    return FileResponse("sitemap.xml", media_type="application/xml")


# ─── Main pages ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/s/{code}", response_class=HTMLResponse)
async def share_view(request: Request, code: str):
    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ? AND expires_at > datetime('now')",
            (code,),
        ).fetchone()

        if not group:
            return templates.TemplateResponse(
                request, "404.html", {}, status_code=404
            )

        images = conn.execute(
            "SELECT * FROM images WHERE group_id = ? ORDER BY id",
            (group["id"],),
        ).fetchall()

    first_img_url = (
        f"{SITE_URL}/uploads/{group['id']}/{images[0]['filename']}" if images else ""
    )
    return templates.TemplateResponse(
        request,
        "share.html",
        {
            "group": group,
            "images": images,
            "group_id": group["id"],
            "first_img_url": first_img_url,
            "code": code,
        },
    )


# ─── Download endpoints ────────────────────────────────────────────────────────

@app.get("/s/{code}/download")
async def download_all(code: str):
    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM image_groups WHERE short_code = ? AND expires_at > datetime('now')",
            (code,),
        ).fetchone()

        if not group:
            raise HTTPException(status_code=404)

        images = conn.execute(
            "SELECT * FROM images WHERE group_id = ? ORDER BY id",
            (group["id"],),
        ).fetchall()

    if not images:
        raise HTTPException(status_code=404)

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
        headers={
            "Content-Disposition": f'attachment; filename="imageshare-{code}.zip"',
            "Content-Type": "application/zip",
        },
    )


# ─── HTMX endpoints ────────────────────────────────────────────────────────────

@app.get("/session-check", response_class=HTMLResponse)
async def session_check(request: Request, img_session: str = Cookie(default=None)):
    if not img_session:
        return HTMLResponse("")

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT g.short_code, g.expires_at, g.image_count, g.id AS group_id
            FROM user_sessions s
            JOIN image_groups g ON s.group_id = g.id
            WHERE s.session_id = ? AND g.expires_at > datetime('now')
            """,
            (img_session,),
        ).fetchone()

        if not row:
            return HTMLResponse("")

        images = conn.execute(
            "SELECT filename FROM images WHERE group_id = ? ORDER BY id",
            (row["group_id"],),
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "partials/session_banner.html",
        {
            "short_code": row["short_code"],
            "expires_at": row["expires_at"],
            "group_id": row["group_id"],
            "images": images,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    img_session: str = Cookie(default=None),
):
    def err(msg: str):
        return templates.TemplateResponse(request, "partials/error.html", {"message": msg})

    if not files or (len(files) == 1 and not files[0].filename):
        return err("কোনো ছবি নির্বাচন করা হয়নি।")

    if len(files) > MAX_IMAGES_PER_GROUP:
        return err(f"সর্বোচ্চ {MAX_IMAGES_PER_GROUP}টি ছবি একসাথে আপলোড করা যাবে।")

    validated: list[tuple[str, str, str, bytes]] = []  # (filename, original, mime, content)
    for f in files:
        if f.content_type not in ALLOWED_MIME_TYPES:
            return err(f"'{f.filename}' অনুমোদিত ফরম্যাট নয়। JPG, PNG, GIF, WebP গ্রহণযোগ্য।")

        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            return err(f"'{f.filename}' ফাইলের সাইজ ১০MB-এর বেশি।")
        if len(content) == 0:
            return err(f"'{f.filename}' ফাইলটি খালি।")

        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".jpg"
        stored = f"{uuid.uuid4().hex}{ext}"
        validated.append((stored, f.filename, f.content_type, content))

    group_id = str(uuid.uuid4())
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=IMAGE_EXPIRY_HOURS)

    group_dir = UPLOAD_DIR / group_id
    group_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    try:
        for stored, original, mime, content in validated:
            (group_dir / stored).write_bytes(content)
            saved.append(
                {"filename": stored, "original_name": original, "mime_type": mime, "file_size": len(content)}
            )
    except Exception:
        import shutil; shutil.rmtree(group_dir, ignore_errors=True)
        return err("আপলোড করার সময় একটি সমস্যা হয়েছে। আবার চেষ্টা করুন।")

    session_id = img_session or str(uuid.uuid4())

    with get_db() as conn:
        short_code = _unique_code(conn)
        conn.execute(
            "INSERT INTO image_groups (id, short_code, created_at, expires_at, image_count) VALUES (?,?,?,?,?)",
            (group_id, short_code, now.strftime("%Y-%m-%dT%H:%M:%S"),
             expires_at.strftime("%Y-%m-%dT%H:%M:%S"), len(saved)),
        )
        conn.executemany(
            "INSERT INTO images (group_id, filename, original_name, mime_type, file_size) VALUES (?,?,?,?,?)",
            [(group_id, s["filename"], s["original_name"], s["mime_type"], s["file_size"]) for s in saved],
        )
        conn.execute(
            "INSERT OR REPLACE INTO user_sessions (session_id, group_id, last_upload) VALUES (?,?,?)",
            (session_id, group_id, now.strftime("%Y-%m-%dT%H:%M:%S")),
        )

    resp = templates.TemplateResponse(
        request,
        "partials/upload_result.html",
        {
            "short_code": short_code,
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "images": saved,
            "group_id": group_id,
            "image_count": len(saved),
        },
    )
    resp.set_cookie(
        "img_session", session_id,
        max_age=86400 * 30, httponly=True, samesite="lax", secure=True,
    )
    return resp


@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
