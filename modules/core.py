# modules/core.py
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

import psycopg2, os, re, uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from typing import Optional, Tuple

from passlib.context import CryptContext

import cloudinary
import cloudinary.uploader


# ======================
# timezone
# ======================
JST = timezone(timedelta(hours=9))


def utcnow_naive() -> datetime:
    # DB保存/比較はUTC naiveで統一
    return datetime.utcnow()


def fmt_jst(dt: Optional[datetime]) -> str:
    return ((dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") if dt else "")


# ======================
# password hash
# ======================
pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto"
)


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
        result = fn(db, cur)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass


# ======================
# Cloudinary
# ======================
def init_cloudinary():
    cloudinary.config(
        cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
        api_key=os.environ.get("CLOUDINARY_API_KEY"),
        api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
        secure=True
    )


# ======================
# helpers
# ======================
def is_https_request(request: Request) -> bool:
    xf = request.headers.get("x-forwarded-proto", "")
    if xf:
        return xf.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


# ======================
# login / handle rules（インスタ方式）
# ======================
LOGIN_ID_RE = re.compile(r"^[a-z0-9._]{3,20}$")


def normalize_login_id(s: str) -> Optional[str]:
    s = (s or "").strip().lower()
    if not s:
        return None
    if not LOGIN_ID_RE.match(s):
        return None
    if s.startswith(".") or s.endswith("."):
        return None
    if ".." in s:
        return None
    return s


# ======================
# auth helpers
# ======================
def get_me_from_cookies(
    db,
    user_cookie: Optional[str],
    uid_cookie: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    # uid(cookie) を最優先
    if uid_cookie:
        uid = uid_cookie.strip()
        if uid:
            cur = db.cursor()
            try:
                cur.execute("SELECT username, id FROM users WHERE id=%s", (uid,))
                row = cur.fetchone()
                if row:
                    return row[0], str(row[1])
            finally:
                cur.close()

    # 旧 user cookie
    if user_cookie:
        u = unquote(user_cookie)
        cur = db.cursor()
        try:
            cur.execute("SELECT username, id FROM users WHERE username=%s", (u,))
            row = cur.fetchone()
            if row:
                return row[0], str(row[1])
        finally:
            cur.close()

    return None, None


def get_me_handle(db, me_user_id: Optional[str]) -> Optional[str]:
    if not me_user_id:
        return None
    cur = db.cursor()
    try:
        cur.execute("SELECT handle FROM users WHERE id=%s", (me_user_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


# ======================
# DM helper
# ======================
def get_or_create_dm_room_id(db, me_user_id: str, other_user_id: str) -> str:
    u1, u2 = sorted([me_user_id, other_user_id])
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id FROM dm_rooms WHERE user1_id=%s AND user2_id=%s",
            (u1, u2)
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

        rid = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO dm_rooms (id, user1_id, user2_id, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (rid, u1, u2, utcnow_naive())
        )
        return rid
    finally:
        cur.close()


# ======================
# DB init
# ======================
def init_db():
    def _do(db, cur):
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            display_name TEXT,
            handle TEXT,
            created_at TIMESTAMP,
            id UUID DEFAULT gen_random_uuid()
        );
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS users_id_unique
        ON users(id);
        """)

        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS users_handle_unique
        ON users(handle)
        WHERE handle IS NOT NULL;
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            username TEXT PRIMARY KEY,
            user_id UUID,
            maker TEXT,
            car TEXT,
            region TEXT,
            bio TEXT,
            icon TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS dm_rooms (
            id UUID PRIMARY KEY,
            user1_id UUID NOT NULL,
            user2_id UUID NOT NULL,
            created_at TIMESTAMP NOT NULL,
            UNIQUE (user1_id, user2_id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS dm_messages (
            id UUID PRIMARY KEY,
            room_id UUID NOT NULL,
            sender_id UUID NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        );
        """)

    run_db(_do)


# ======================
# Jinja init
# ======================
def init_jinja(templates: Jinja2Templates):
    def jinja_urlencode(s: str) -> str:
        try:
            return quote(s)
        except Exception:
            return s

    templates.env.filters["urlencode"] = jinja_urlencode


# ======================
# app init
# ======================
def init_app(app: FastAPI, templates: Jinja2Templates):
    os.makedirs("uploads", exist_ok=True)
    init_cloudinary()
    init_jinja(templates)
    init_db()
