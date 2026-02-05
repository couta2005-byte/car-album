# modules/core.py
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
import psycopg2, os, re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from typing import Optional, Tuple

from passlib.context import CryptContext
import cloudinary

# ======================
# timezone
# ======================
JST = timezone(timedelta(hours=9))

def utcnow_naive():
    return datetime.utcnow()

def fmt_jst(dt):
    return ((dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") if dt else "")

# ======================
# password hash
# ======================
pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto"
)

# ======================
# templates
# ======================
templates = Jinja2Templates(directory="templates")

def jinja_urlencode(s: str) -> str:
    try:
        return quote(s)
    except Exception:
        return s

templates.env.filters["urlencode"] = jinja_urlencode

# ======================
# PostgreSQL
# ======================
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

def get_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.autocommit = False
    return conn

def run_db(fn):
    db = get_db()
    cur = db.cursor()
    try:
        r = fn(db, cur)
        db.commit()
        return r
    except Exception:
        db.rollback()
        raise
    finally:
        try: cur.close()
        except: pass
        try: db.close()
        except: pass

# ======================
# Cloudinary
# ======================
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# ======================
# auth helpers
# ======================
LOGIN_ID_RE = re.compile(r"^[a-z0-9._]{3,20}$")

def normalize_login_id(s: str):
    s = (s or "").strip().lower()
    if not LOGIN_ID_RE.match(s): return None
    if s.startswith(".") or s.endswith("."): return None
    if ".." in s: return None
    return s

def get_me_from_cookies(db, user_cookie, uid_cookie) -> Tuple[Optional[str], Optional[str]]:
    if uid_cookie:
        cur = db.cursor()
        try:
            cur.execute("SELECT username, id FROM users WHERE id=%s", (uid_cookie,))
            r = cur.fetchone()
            if r:
                return r[0], str(r[1])
        finally:
            cur.close()

    if user_cookie:
        u = unquote(user_cookie)
        cur = db.cursor()
        try:
            cur.execute("SELECT username, id FROM users WHERE username=%s", (u,))
            r = cur.fetchone()
            if r:
                return r[0], str(r[1])
        finally:
            cur.close()

    return None, None

def get_me_handle(db, me_user_id):
    if not me_user_id:
        return None
    cur = db.cursor()
    try:
        cur.execute("SELECT handle FROM users WHERE id=%s", (me_user_id,))
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        cur.close()

# ======================
# redirect helper
# ======================
def redirect_back(request: Request, fallback="/"):
    nxt = request.query_params.get("next")
    if nxt and nxt.startswith("/"):
        return RedirectResponse(nxt, status_code=303)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)
