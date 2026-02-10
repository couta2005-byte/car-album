from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2, os, re, uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from typing import Optional, Dict, List, Any, Tuple

# ★ password hash（bcrypt不具合回避：pbkdf2_sha256のみ使用）
from passlib.context import CryptContext

# ★ Cloudinary
import cloudinary
import cloudinary.uploader

app = FastAPI()

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
# static / uploads
# ======================
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")

# Jinja2 filter: urlencode
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
        result = fn(db, cur)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            cur.close()
        except:
            pass
        try:
            db.close()
        except:
            pass

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
# helpers: https（Render対策）, handle validation（インスタ方式）
# ======================
def is_https_request(request: Request) -> bool:
    xf = request.headers.get("x-forwarded-proto", "")
    if xf:
        return xf.split(",")[0].strip() == "https"
    return request.url.scheme == "https"

# ✅ インスタ寄せ：ログインID(@ID)は「小文字 + 数字 + . _」のみ
# 3〜20文字、先頭末尾が"."はNG、連続"..”もNG
LOGIN_ID_RE = re.compile(r"^[a-z0-9._]{3,20}$")

def normalize_login_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    s = s.lower()

    if not LOGIN_ID_RE.match(s):
        return None
    if s.startswith(".") or s.endswith("."):
        return None
    if ".." in s:
        return None
    return s

def is_handle_available(db, handle: str, exclude_user_id: Optional[str] = None) -> bool:
    cur = db.cursor()
    try:
        if exclude_user_id:
            cur.execute("SELECT 1 FROM users WHERE handle=%s AND id<>%s LIMIT 1", (handle, exclude_user_id))
        else:
            cur.execute("SELECT 1 FROM users WHERE handle=%s LIMIT 1", (handle,))
        return cur.fetchone() is None
    finally:
        cur.close()

def suggest_handle_from_login(login_id: str) -> Optional[str]:
    return normalize_login_id(login_id)

# ======================
# ✅ auth: uid cookie（UUID）を優先して自分を特定する
# ======================
def get_me_from_cookies(db, user_cookie: Optional[str], uid_cookie: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    return (me_username, me_user_id)
    - uid(cookie) があれば最優先
    - 旧 user(cookie=username) は互換で残す
    """
    # 1) uid があれば users.id から username を引く
    if uid_cookie:
        uid = (uid_cookie or "").strip()
        if uid:
            cur = db.cursor()
            try:
                cur.execute("SELECT username, id FROM users WHERE id=%s", (uid,))
                row = cur.fetchone()
                if row:
                    return row[0], str(row[1])
            finally:
                cur.close()

    # 2) 旧 user cookie（username）
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
# ✅ DM helpers（ルーム作成/取得）
# ======================
def get_or_create_dm_room_id(db, me_user_id: str, other_user_id: str) -> str:
    """
    dm_rooms は (user1_id, user2_id) を昇順で固定して一意化
    """
    u1, u2 = sorted([me_user_id, other_user_id])
    cur = db.cursor()
    try:
        cur.execute("SELECT id FROM dm_rooms WHERE user1_id=%s AND user2_id=%s", (u1, u2))
        row = cur.fetchone()
        if row:
            return str(row[0])

        rid = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO dm_rooms (id, user1_id, user2_id, created_at)
            VALUES (%s, %s, %s, %s)
        """, (rid, u1, u2, utcnow_naive()))
        return rid
    finally:
        cur.close()
# ======================
# DB init（壊さない段階移行：UUID追加＋既存データ埋め） + ✅DM追加
# ======================
def init_db():
    def _do(db, cur):
        # ✅ UUID生成関数（pgcrypto）
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT
        );

        CREATE TABLE IF NOT EXISTS profiles (
            username TEXT PRIMARY KEY,
            maker TEXT,
            car TEXT,
            region TEXT,
            bio TEXT
        );

        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            username TEXT,
            maker TEXT,
            region TEXT,
            car TEXT,
            comment TEXT,
            image TEXT,
            created_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS follows (
            follower TEXT,
            followee TEXT,
            PRIMARY KEY (follower, followee)
        );

        CREATE TABLE IF NOT EXISTS likes (
            username TEXT,
            post_id INTEGER,
            PRIMARY KEY (username, post_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER,
            username TEXT,
            comment TEXT,
            created_at TIMESTAMP
        );
        """)

        # ---- profiles icon ----
        cur.execute("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS icon TEXT;")

        # ---- users 拡張（display_name / handle / created_at）----
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS handle TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")

        # ---- ✅ users.id(UUID) 追加（固定IDの本体）----
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id UUID;")
        cur.execute("UPDATE users SET id = gen_random_uuid() WHERE id IS NULL;")
        cur.execute("ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid();")

        # ✅ 既存handleを小文字に正規化（今後の衝突を減らす）
        cur.execute("UPDATE users SET handle = LOWER(handle) WHERE handle IS NOT NULL;")

        # handle unique（NULL複数OK）
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_handle_unique
            ON users(handle)
            WHERE handle IS NOT NULL;
        """)
        # id unique
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_id_unique
            ON users(id)
            WHERE id IS NOT NULL;
        """)

        # 既存ユーザーの埋め
        cur.execute("UPDATE users SET display_name = username WHERE display_name IS NULL;")
        cur.execute("UPDATE users SET created_at = NOW() WHERE created_at IS NULL;")

        # ---- comment likes ----
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comment_likes (
            username TEXT,
            comment_id INTEGER,
            PRIMARY KEY (username, comment_id)
        );
        """)

        # ======================
        # ✅ 段階移行：各テーブルに user_id を追加して埋める
        # ======================

        # posts.user_id
        cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS user_id UUID;")
        cur.execute("""
            UPDATE posts p
            SET user_id = u.id
            FROM users u
            WHERE p.user_id IS NULL AND p.username = u.username;
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS posts_user_id_idx ON posts(user_id);")

        # comments.user_id
        cur.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS user_id UUID;")
        cur.execute("""
            UPDATE comments c
            SET user_id = u.id
            FROM users u
            WHERE c.user_id IS NULL AND c.username = u.username;
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS comments_user_id_idx ON comments(user_id);")

        # likes.user_id
        cur.execute("ALTER TABLE likes ADD COLUMN IF NOT EXISTS user_id UUID;")
        cur.execute("""
            UPDATE likes l
            SET user_id = u.id
            FROM users u
            WHERE l.user_id IS NULL AND l.username = u.username;
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS likes_user_id_post_id_unique
            ON likes(user_id, post_id)
            WHERE user_id IS NOT NULL;
        """)

        # follows.follower_id / followee_id
        cur.execute("ALTER TABLE follows ADD COLUMN IF NOT EXISTS follower_id UUID;")
        cur.execute("ALTER TABLE follows ADD COLUMN IF NOT EXISTS followee_id UUID;")
        cur.execute("""
            UPDATE follows f
            SET follower_id = uf.id
            FROM users uf
            WHERE f.follower_id IS NULL AND f.follower = uf.username;
        """)
        cur.execute("""
            UPDATE follows f
            SET followee_id = ut.id
            FROM users ut
            WHERE f.followee_id IS NULL AND f.followee = ut.username;
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS follows_ids_unique
            ON follows(follower_id, followee_id)
            WHERE follower_id IS NOT NULL AND followee_id IS NOT NULL;
        """)

        # comment_likes.user_id
        cur.execute("ALTER TABLE comment_likes ADD COLUMN IF NOT EXISTS user_id UUID;")
        cur.execute("""
            UPDATE comment_likes cl
            SET user_id = u.id
            FROM users u
            WHERE cl.user_id IS NULL AND cl.username = u.username;
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS comment_likes_ids_unique
            ON comment_likes(user_id, comment_id)
            WHERE user_id IS NOT NULL;
        """)

        # profiles.user_id
        cur.execute("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS user_id UUID;")
        cur.execute("""
            UPDATE profiles pr
            SET user_id = u.id
            FROM users u
            WHERE pr.user_id IS NULL AND pr.username = u.username;
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS profiles_user_id_unique
            ON profiles(user_id)
            WHERE user_id IS NOT NULL;
        """)

        # ======================
        # ✅ DM tables（追加）
        # ======================
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
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY (room_id) REFERENCES dm_rooms(id)
        );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS dm_rooms_user1_idx ON dm_rooms(user1_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS dm_rooms_user2_idx ON dm_rooms(user2_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS dm_messages_room_time_idx ON dm_messages(room_id, created_at);")

    run_db(_do)

@app.on_event("startup")
def startup():
    init_db()

# ======================
# common
# ======================
def redirect_back(request: Request, fallback: str = "/"):
    next_url = request.query_params.get("next")
    if next_url and next_url.startswith("/"):
        return RedirectResponse(next_url, status_code=303)

    referer = request.headers.get("referer")
    return RedirectResponse(referer or fallback, status_code=303)

def get_liked_posts(db, me_user_id: Optional[str], me_username: Optional[str]) -> set:
    if not me_user_id and not me_username:
        return set()
    cur = db.cursor()
    try:
        if me_user_id:
            cur.execute("SELECT post_id FROM likes WHERE user_id=%s", (me_user_id,))
        else:
            cur.execute("SELECT post_id FROM likes WHERE username=%s", (me_username,))
        return {r[0] for r in cur.fetchall()}
    finally:
        cur.close()

# ======================
# ✅ users search（検索ページ用）
# ======================
def search_users(db, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    if not q:
        return []

    like = f"%{q}%"
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT
                u.id,
                u.username,
                COALESCE(u.display_name, u.username) AS display_name,
                u.handle,
                pr.icon AS icon
            FROM users u
            LEFT JOIN profiles pr ON pr.user_id = u.id
            WHERE
                u.username ILIKE %s
                OR COALESCE(u.display_name, '') ILIKE %s
                OR COALESCE(u.handle, '') ILIKE %s
            ORDER BY
                CASE WHEN u.handle = %s THEN 0 ELSE 1 END,
                CASE WHEN u.username = %s THEN 0 ELSE 1 END,
                CASE WHEN u.username ILIKE %s THEN 0 ELSE 1 END,
                CASE WHEN COALESCE(u.handle,'') ILIKE %s THEN 0 ELSE 1 END,
                u.username ASC
            LIMIT %s
        """, (
            like, like, like,
            q.lower(), q,
            f"{q}%", f"{q}%",
            int(limit),
        ))
        rows = cur.fetchall()
    finally:
        cur.close()

    out: List[Dict[str, Any]] = []
    for (uid, username, display_name, handle, icon) in rows:
        profile_key = handle if handle else username
        out.append({
            "id": str(uid) if uid is not None else None,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "icon": icon,
            "profile_key": profile_key,
        })
    return out
# ======================
# comments fetch（一覧・ランキング用）
# ======================
def fetch_comments_for_posts(db, post_ids: List[int], me_user_id: Optional[str]) -> Dict[int, List[Dict[str, Any]]]:
    if not post_ids:
        return {}

    placeholders = ",".join(["%s"] * len(post_ids))

    cur = db.cursor()
    try:
        if me_user_id:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    COALESCE(u.username, c.username) AS username,
                    COALESCE(u.display_name, COALESCE(u.username, c.username)) AS display_name,
                    u.handle AS handle,
                    COALESCE(c.user_id, u.id) AS user_id,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.user_id IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                LEFT JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = COALESCE(u.id, c.user_id)
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                LEFT JOIN comment_likes mycl
                    ON mycl.comment_id = c.id AND mycl.user_id = %s
                WHERE c.post_id IN ({placeholders})
                ORDER BY c.id ASC
            """
            cur.execute(sql, (me_user_id, *post_ids))
        else:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    COALESCE(u.username, c.username) AS username,
                    COALESCE(u.display_name, COALESCE(u.username, c.username)) AS display_name,
                    u.handle AS handle,
                    COALESCE(c.user_id, u.id) AS user_id,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                LEFT JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = COALESCE(u.id, c.user_id)
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                WHERE c.post_id IN ({placeholders})
                ORDER BY c.id ASC
            """
            cur.execute(sql, tuple(post_ids))

        rows = cur.fetchall()
    finally:
        cur.close()

    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        if me_user_id:
            post_id, cid, username, display_name, handle, c_user_id, comment, created_at, user_icon, likes, liked = r
        else:
            post_id, cid, username, display_name, handle, c_user_id, comment, created_at, user_icon, likes = r
            liked = 0

        profile_key = handle if handle else username

        out.setdefault(post_id, []).append({
            "id": cid,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "profile_key": profile_key,
            "user_id": str(c_user_id) if c_user_id is not None else None,
            "comment": comment,
            "created_at": fmt_jst(created_at),
            "user_icon": user_icon,
            "likes": int(likes or 0),
            "liked": bool(liked)
        })
    return out

# ======================
# post_detail用（単体）
# ======================
def fetch_comments_for_post_detail(db, post_id: int, me_user_id: Optional[str]) -> List[Dict[str, Any]]:
    cur = db.cursor()
    try:
        if me_user_id:
            cur.execute("""
                SELECT
                    c.id,
                    COALESCE(u.username, c.username) AS username,
                    COALESCE(u.display_name, COALESCE(u.username, c.username)) AS display_name,
                    u.handle AS handle,
                    COALESCE(c.user_id, u.id) AS user_id,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.user_id IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                LEFT JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = COALESCE(u.id, c.user_id)
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                LEFT JOIN comment_likes mycl
                    ON mycl.comment_id = c.id AND mycl.user_id = %s
                WHERE c.post_id = %s
                ORDER BY c.id ASC
            """, (me_user_id, post_id))
        else:
            cur.execute("""
                SELECT
                    c.id,
                    COALESCE(u.username, c.username) AS username,
                    COALESCE(u.display_name, COALESCE(u.username, c.username)) AS display_name,
                    u.handle AS handle,
                    COALESCE(c.user_id, u.id) AS user_id,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                LEFT JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = COALESCE(u.id, c.user_id)
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                WHERE c.post_id = %s
                ORDER BY c.id ASC
            """, (post_id,))
        rows = cur.fetchall()
    finally:
        cur.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        if me_user_id:
            cid, username, display_name, handle, c_user_id, comment, created_at, user_icon, likes, liked = r
        else:
            cid, username, display_name, handle, c_user_id, comment, created_at, user_icon, likes = r
            liked = 0

        profile_key = handle if handle else username

        out.append({
            "id": cid,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "profile_key": profile_key,
            "user_id": str(c_user_id) if c_user_id is not None else None,
            "comment": comment,
            "created_at": fmt_jst(created_at),
            "user_icon": user_icon,
            "likes": int(likes or 0),
            "liked": bool(liked)
        })
    return out

# ======================
# posts fetch（display_name/handle も返す）
# ======================
def fetch_posts(db, me_user_id: Optional[str], where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id,
                COALESCE(u.username, p.username) AS username,
                COALESCE(u.display_name, COALESCE(u.username, p.username)) AS display_name,
                u.handle AS handle,
                COALESCE(p.user_id, u.id) AS user_id,
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                COUNT(l.post_id) AS like_count,
                pr.icon AS user_icon
            FROM posts p
            LEFT JOIN users u
                ON (p.user_id IS NOT NULL AND p.user_id = u.id)
                OR (p.user_id IS NULL AND p.username = u.username)
            LEFT JOIN likes l ON p.id = l.post_id
            LEFT JOIN profiles pr ON pr.user_id = COALESCE(u.id, p.user_id)
            {where_sql}
            GROUP BY
                p.id,
                COALESCE(u.username, p.username),
                COALESCE(u.display_name, COALESCE(u.username, p.username)),
                u.handle,
                COALESCE(p.user_id, u.id),
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                pr.icon
            {order_sql}
            {limit_sql}
        """, params)
        rows = cur.fetchall()
    finally:
        cur.close()

    post_ids = [r[0] for r in rows]
    comments_map = fetch_comments_for_posts(db, post_ids, me_user_id)

    posts = []
    for r in rows:
        pid = r[0]
        username = r[1]
        display_name = r[2]
        handle = r[3]
        user_id = str(r[4]) if r[4] is not None else None

        profile_key = handle if handle else username

        post_comments = comments_map.get(pid, [])
        posts.append({
            "id": pid,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "profile_key": profile_key,
            "user_id": user_id,
            "maker": r[5],
            "region": r[6],
            "car": r[7],
            "comment": r[8],
            "image": r[9],
            "created_at": fmt_jst(r[10]),
            "likes": r[11],
            "user_icon": r[12],
            "comments": post_comments,
            "comment_count": len(post_comments)
        })
    return posts
# ======================
# top
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        posts = fetch_posts(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "mode": "home",
        "ranking_title": "",
        "period": ""
    })

# ======================
# auth pages（errorをテンプレに渡す）
# ======================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
    finally:
        db.close()

    error = request.query_params.get("error", "")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "error": error
    })

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
    finally:
        db.close()

    error = request.query_params.get("error", "")
    return templates.TemplateResponse("register.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "error": error
    })

# ======================
# search
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = Query(default=""),
    user_q: str = Query(default=""),
    maker: str = Query(default=""),
    car: str = Query(default=""),
    region: str = Query(default=""),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    q = (q or "").strip()
    user_q = (user_q or "").strip()
    maker = (maker or "").strip()
    car = (car or "").strip()
    region = (region or "").strip()

    if q and not user_q:
        user_q = q

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id, me_username)

        users: List[Dict[str, Any]] = []
        posts: List[Dict[str, Any]] = []

        if user_q:
            users = search_users(db, user_q, limit=20)

        if maker or car or region:
            posts = fetch_posts(
                db, me_user_id,
                "WHERE p.maker ILIKE %s AND p.car ILIKE %s AND p.region ILIKE %s",
                (f"%{maker}%", f"%{car}%", f"%{region}%"),
                order_sql="ORDER BY p.id DESC"
            )
        else:
            posts = []
    finally:
        db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "q": q,
        "user_q": user_q,
        "users": users,
        "maker": maker,
        "car": car,
        "region": region,
        "mode": "search"
    })

# ======================
# following TL
# ======================
@app.get("/following", response_class=HTMLResponse)
def following(request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        posts = fetch_posts(
            db, me_user_id,
            "JOIN follows f ON p.user_id = f.followee_id WHERE f.follower_id=%s",
            (me_user_id,)
        )
        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "mode": "following",
        "ranking_title": "",
        "period": ""
    })

# ======================
# ranking
# ======================
@app.get("/ranking", response_class=HTMLResponse)
def ranking(
    request: Request,
    period: str = Query(default="day"),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)

        now_utc = datetime.utcnow()
        if period == "week":
            since_utc_naive = now_utc - timedelta(days=7)
            title = "週間ランキング TOP10"
        elif period == "month":
            since_utc_naive = now_utc - timedelta(days=30)
            title = "月間ランキング TOP10"
        else:
            since_utc_naive = now_utc - timedelta(hours=24)
            title = "日間ランキング TOP10"

        posts = fetch_posts(
            db, me_user_id,
            "WHERE p.created_at >= %s",
            (since_utc_naive,),
            order_sql="ORDER BY like_count DESC, p.id DESC",
            limit_sql="LIMIT 10"
        )
        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        db.close()

    return templates.TemplateResponse("ranking.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "mode": f"ranking_{period}",
        "ranking_title": title,
        "period": period
    })

# ======================
# post detail
# ======================
@app.get("/post/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id, me_username)
        posts = fetch_posts(db, me_user_id, "WHERE p.id=%s", (post_id,))
        if not posts:
            return RedirectResponse("/", status_code=303)

        post = posts[0]
        post_comments = fetch_comments_for_post_detail(db, post_id, me_user_id)
        post["comments"] = post_comments
        post["comment_count"] = len(post_comments)
    finally:
        db.close()

    return templates.TemplateResponse("post_detail.html", {
        "request": request,
        "post": post,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "mode": "post_detail"
    })

# ======================
# comment
# ======================
@app.post("/comment/{post_id}")
def add_comment(
    request: Request,
    post_id: int,
    comment: str = Form(""),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    comment = (comment or "").strip()
    if not comment:
        return redirect_back(request, fallback=f"/post/{post_id}")

    def _do(db, cur):
        cur.execute("SELECT 1 FROM posts WHERE id=%s", (post_id,))
        if cur.fetchone() is None:
            return
        cur.execute("""
            INSERT INTO comments (post_id, username, user_id, comment, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (post_id, me_username, me_user_id, comment, utcnow_naive()))

    run_db(_do)
    return redirect_back(request, fallback=f"/post/{post_id}")

# ======================
# comment delete（自分のだけ）
# ======================
@app.post("/comment_delete/{comment_id}")
def delete_comment(request: Request, comment_id: int, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("""
            SELECT post_id
            FROM comments
            WHERE id=%s
              AND (
                    user_id=%s
                    OR (user_id IS NULL AND username=%s)
                  )
        """, (comment_id, me_user_id, me_username))
        row = cur.fetchone()
        if not row:
            return None
        post_id = row[0]

        cur.execute("DELETE FROM comment_likes WHERE comment_id=%s", (comment_id,))
        cur.execute("""
            DELETE FROM comments
            WHERE id=%s
              AND (
                    user_id=%s
                    OR (user_id IS NULL AND username=%s)
                  )
        """, (comment_id, me_user_id, me_username))
        return post_id

    post_id = run_db(_do)
    fallback = f"/post/{post_id}" if post_id else "/"
    return redirect_back(request, fallback=fallback)

# ======================
# ✅ comment like API（リロード無し）
# ======================
@app.post("/api/comment_like/{comment_id}")
def api_comment_like(comment_id: int, request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return JSONResponse({"ok": False, "error": "login_required"}, status_code=401)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("SELECT 1 FROM comments WHERE id=%s", (comment_id,))
        if cur.fetchone() is None:
            return {"ok": False, "error": "not_found"}

        cur.execute("SELECT 1 FROM comment_likes WHERE user_id=%s AND comment_id=%s", (me_user_id, comment_id))
        liked = cur.fetchone() is not None

        if liked:
            cur.execute("DELETE FROM comment_likes WHERE user_id=%s AND comment_id=%s", (me_user_id, comment_id))
            liked = False
        else:
            cur.execute(
                "INSERT INTO comment_likes (username, user_id, comment_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (me_username, me_user_id, comment_id)
            )
            liked = True

        cur.execute("SELECT COUNT(*) FROM comment_likes WHERE comment_id=%s", (comment_id,))
        likes_count = cur.fetchone()[0]

        return {"ok": True, "liked": liked, "likes": likes_count}

    return JSONResponse(run_db(_do))
# ======================
# profile（/user/{key} は handle優先で解決）
# ======================
def resolve_user_by_key(db, key: str):
    cur = db.cursor()
    try:
        cur.execute("SELECT id, username, display_name, handle FROM users WHERE handle=%s", (key,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute("SELECT id, username, display_name, handle FROM users WHERE username=%s", (key,))
        row = cur.fetchone()
        return row
    finally:
        cur.close()

@app.get("/user/{key}", response_class=HTMLResponse)
def profile(request: Request, key: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    key = unquote(key)

    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)

        urow = resolve_user_by_key(db, key)
        if not urow:
            return RedirectResponse("/", status_code=303)

        target_user_id = str(urow[0])
        username = urow[1]
        display_name = urow[2]
        handle = urow[3]

        cur.execute("SELECT maker, car, region, bio, icon FROM profiles WHERE user_id=%s", (target_user_id,))
        prof = cur.fetchone()

        posts = fetch_posts(db, me_user_id, "WHERE p.user_id=%s", (target_user_id,))

        cur.execute("SELECT COUNT(*) FROM follows WHERE follower_id=%s", (target_user_id,))
        follow_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM follows WHERE followee_id=%s", (target_user_id,))
        follower_count = cur.fetchone()[0]

        is_following = False
        if me_user_id and me_user_id != target_user_id:
            cur.execute("SELECT 1 FROM follows WHERE follower_id=%s AND followee_id=%s", (me_user_id, target_user_id))
            is_following = cur.fetchone() is not None

        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "profile": prof,
        "me": me_username,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "target_user_id": target_user_id,
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "liked_posts": liked_posts,
        "display_name": display_name,
        "handle": handle,
        "mode": "profile",
        "posts": posts
    })

# ======================
# profile edit（icon + display_name/handle）
# ======================
@app.post("/profile/edit")
def profile_edit(
    request: Request,
    display_name: str = Form(""),
    handle: str = Form(""),
    maker: str = Form(""),
    car: str = Form(""),
    region: str = Form(""),
    bio: str = Form(""),
    icon: UploadFile = File(None),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    display_name = (display_name or "").strip()
    if not display_name:
        display_name = me_username
    if len(display_name) > 40:
        display_name = display_name[:40]

    handle_norm = normalize_login_id(handle)

    icon_url = None
    if icon and icon.filename:
        result = cloudinary.uploader.upload(
            icon.file,
            folder="carbum/icons",
            transformation=[
                {"width": 256, "height": 256, "crop": "fill", "gravity": "face"},
                {"quality": "auto", "fetch_format": "auto"}
            ]
        )
        icon_url = result.get("secure_url")

    def _do(db, cur):
        final_handle = handle_norm
        if final_handle is not None:
            cur.execute("SELECT 1 FROM users WHERE handle=%s AND id<>%s LIMIT 1", (final_handle, me_user_id))
            if cur.fetchone() is not None:
                final_handle = None

        cur.execute("""
            UPDATE users
            SET display_name=%s,
                handle=%s
            WHERE id=%s
        """, (display_name, final_handle, me_user_id))

        if icon_url:
            cur.execute("""
                INSERT INTO profiles (username, user_id, maker, car, region, bio, icon)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET
                    user_id=EXCLUDED.user_id,
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio,
                    icon=EXCLUDED.icon
            """, (me_username, me_user_id, maker, car, region, bio, icon_url))
        else:
            cur.execute("""
                INSERT INTO profiles (username, user_id, maker, car, region, bio)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET
                    user_id=EXCLUDED.user_id,
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio
            """, (me_username, me_user_id, maker, car, region, bio))

        cur.execute("SELECT handle FROM users WHERE id=%s", (me_user_id,))
        row = cur.fetchone()
        return row[0] if row else None

    new_handle = run_db(_do)
    key = new_handle if new_handle else me_username
    return RedirectResponse(f"/user/{quote(key)}", status_code=303)

# ======================
# follow / unfollow（handleでもOK）
# ======================
def resolve_target_user(db, key: str):
    row = resolve_user_by_key(db, key)
    if not row:
        return None
    target_user_id = str(row[0])
    target_username = row[1]
    target_handle = row[3]
    target_key = target_handle if target_handle else target_username
    return target_user_id, target_username, target_key

@app.post("/follow/{key}")
def follow(key: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    key = unquote(key)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        target = resolve_target_user(db, key)
        if not target:
            return RedirectResponse("/", status_code=303)
        target_user_id, target_username, target_key = target
    finally:
        db.close()

    if me_user_id == target_user_id:
        return RedirectResponse(f"/user/{quote(target_key)}", status_code=303)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO follows (follower, followee, follower_id, followee_id) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (me_username, target_username, me_user_id, target_user_id)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target_key)}", status_code=303)

@app.post("/unfollow/{key}")
def unfollow(key: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    key = unquote(key)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        target = resolve_target_user(db, key)
        if not target:
            return RedirectResponse("/", status_code=303)
        target_user_id, target_username, target_key = target
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("DELETE FROM follows WHERE follower_id=%s AND followee_id=%s", (me_user_id, target_user_id))

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target_key)}", status_code=303)

# ======================
# post（user_idで保存）
# ======================
@app.post("/post")
def post(
    request: Request,
    maker: str = Form(""),
    region: str = Form(""),
    car: str = Form(""),
    comment: str = Form(""),
    image: UploadFile = File(None),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    image_path = None
    if image and image.filename:
        result = cloudinary.uploader.upload(
            image.file,
            folder="carbum/posts",
            transformation=[
                {"width": 1400, "crop": "limit"},
                {"quality": "auto", "fetch_format": "auto"}
            ]
        )
        image_path = result["secure_url"]

    def _do(db, cur):
        cur.execute("""
            INSERT INTO posts (username, user_id, maker, region, car, comment, image, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (me_username, me_user_id, maker, region, car, comment, image_path, utcnow_naive()))

    run_db(_do)
    return redirect_back(request, "/")

# ======================
# ✅ auth（pbkdf2） + ✅ インスタ方式：handleでもログインOK
# ======================
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    login_id_raw = (username or "").strip()

    db = get_db()
    cur = db.cursor()
    try:
        h = normalize_login_id(login_id_raw)
        if h:
            cur.execute("SELECT password, id, username FROM users WHERE handle=%s", (h,))
            row = cur.fetchone()
            if row and pwd_context.verify(password, row[0]):
                user_id = str(row[1])
                real_username = row[2]
                res = RedirectResponse("/", status_code=303)
                res.set_cookie(key="user", value=quote(real_username), httponly=True, secure=is_https_request(request), samesite="lax")
                res.set_cookie(key="uid", value=user_id, httponly=True, secure=is_https_request(request), samesite="lax")
                return res

        cur.execute("SELECT password, id, username FROM users WHERE username=%s", (login_id_raw,))
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if not row or not pwd_context.verify(password, row[0]):
        return RedirectResponse("/login?error=invalid", status_code=303)

    user_id = str(row[1])
    real_username = row[2]

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(key="user", value=quote(real_username), httponly=True, secure=is_https_request(request), samesite="lax")
    res.set_cookie(key="uid", value=user_id, httponly=True, secure=is_https_request(request), samesite="lax")
    return res

@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    # ✅ 新規登録の「ログインID」はインスタルール強制（日本語NG）
    login_id = normalize_login_id(username)
    if not login_id:
        return RedirectResponse("/register?error=invalid_id", status_code=303)

    if len(password) < 4 or len(password) > 256:
        return RedirectResponse("/register?error=invalid_pw", status_code=303)

    hashed = pwd_context.hash(password)

    def _do(db, cur):
        display_name = login_id
        h = suggest_handle_from_login(login_id)

        if h is None:
            raise RuntimeError("bad_handle")
        if not is_handle_available(db, h):
            raise RuntimeError("handle_taken")

        cur.execute("""
            INSERT INTO users (username, password, display_name, handle, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (login_id, hashed, display_name, h, utcnow_naive()))
        new_id = cur.fetchone()[0]
        return str(new_id)

    try:
        new_user_id = run_db(_do)
    except Exception as e:
        msg = str(e)
        if "handle_taken" in msg:
            return RedirectResponse("/register?error=handle_taken", status_code=303)
        if "bad_handle" in msg:
            return RedirectResponse("/register?error=invalid_id", status_code=303)
        return RedirectResponse("/register?error=failed", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(key="user", value=quote(login_id), httponly=True, secure=is_https_request(request), samesite="lax")
    res.set_cookie(key="uid", value=new_user_id, httponly=True, secure=is_https_request(request), samesite="lax")
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    res.delete_cookie("uid")
    return res

# ======================
# likes API（リロード無し）: user_idベース
# ======================
@app.post("/api/like/{post_id}")
def api_like(post_id: int, request: Request, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return JSONResponse({"ok": False, "error": "login_required"}, status_code=401)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("SELECT 1 FROM likes WHERE user_id=%s AND post_id=%s", (me_user_id, post_id))
        liked = cur.fetchone() is not None

        if liked:
            cur.execute("DELETE FROM likes WHERE user_id=%s AND post_id=%s", (me_user_id, post_id))
            liked = False
        else:
            cur.execute(
                "INSERT INTO likes (username, user_id, post_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (me_username, me_user_id, post_id)
            )
            liked = True

        cur.execute("SELECT COUNT(*) FROM likes WHERE post_id=%s", (post_id,))
        likes_count = cur.fetchone()[0]

        return {"ok": True, "liked": liked, "likes": likes_count}

    return JSONResponse(run_db(_do))

# ======================
# delete post（自分のだけ）
# ======================
@app.post("/delete/{post_id}")
def delete_post(request: Request, post_id: int, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("SELECT 1 FROM posts WHERE id=%s AND user_id=%s", (post_id, me_user_id))
        row = cur.fetchone()
        if not row:
            return

        cur.execute("""
            DELETE FROM comment_likes
            WHERE comment_id IN (SELECT id FROM comments WHERE post_id=%s)
        """, (post_id,))
        cur.execute("DELETE FROM comments WHERE post_id=%s", (post_id,))
        cur.execute("DELETE FROM likes WHERE post_id=%s", (post_id,))
        cur.execute("DELETE FROM posts WHERE id=%s", (post_id,))

    run_db(_do)
    return redirect_back(request, fallback="/")

# ======================
# ✅ DM（HTMLで完全に動かす版）
# ======================

# ======================
# ✅ DM room（HTML）
# ======================
@app.get("/dm/{room_id}", response_class=HTMLResponse)
def dm_room(
    request: Request,
    room_id: str,
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    cur = db.cursor()
    try:
        # ログイン判定
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        me_handle = get_me_handle(db, me_user_id)

        # 自分が所属しているルームか確認
        cur.execute("""
            SELECT user1_id, user2_id
            FROM dm_rooms
            WHERE id=%s AND (user1_id=%s OR user2_id=%s)
        """, (room_id, me_user_id, me_user_id))
        row = cur.fetchone()
        if not row:
            return RedirectResponse("/", status_code=303)

        user1_id, user2_id = row
        other_user_id = user2_id if str(user1_id) == me_user_id else user1_id

        # 相手ユーザー情報
        cur.execute("""
            SELECT u.id, u.username, u.display_name, u.handle, p.icon
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE u.id=%s
        """, (other_user_id,))
        other = cur.fetchone()

        # メッセージ一覧
        cur.execute("""
            SELECT
                m.id,
                m.sender_id,
                m.body,
                m.created_at,
                u.username,
                u.display_name,
                u.handle
            FROM dm_messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.room_id=%s
            ORDER BY m.created_at ASC
        """, (room_id,))
        rows = cur.fetchall()

        messages = []
        for r in rows:
            messages.append({
                "id": str(r[0]),
                "sender_id": str(r[1]),
                "body": r[2],
                "created_at": fmt_jst(r[3]),
                "username": r[4],
                "display_name": r[5],
                "handle": r[6],
                "is_me": str(r[1]) == me_user_id,
            })

    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("dm_room.html", {
        "request": request,
        "room_id": room_id,
        "messages": messages,
        "other": {
            "id": str(other[0]),
            "username": other[1],
            "display_name": other[2],
            "handle": other[3],
            "icon": other[4],
        },
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "mode": "dm",
    })

@app.post("/dm/start/{target_user_id}")
def dm_start(
    target_user_id: str,
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        if me_user_id == target_user_id:
            return RedirectResponse("/", status_code=303)

        # 相互フォロー判定①
        cur.execute(
            "SELECT 1 FROM follows WHERE follower_id=%s AND followee_id=%s",
            (me_user_id, target_user_id),
        )
        if cur.fetchone() is None:
            return RedirectResponse("/", status_code=303)

        # 相互フォロー判定②
        cur.execute(
            "SELECT 1 FROM follows WHERE follower_id=%s AND followee_id=%s",
            (target_user_id, me_user_id),
        )
        if cur.fetchone() is None:
            return RedirectResponse("/", status_code=303)

        # ✅ DMルーム作成 or 取得
        room_id = get_or_create_dm_room_id(db, me_user_id, target_user_id)
        db.commit()

    finally:
        cur.close()
        db.close()

    return RedirectResponse(f"/dm/{room_id}", status_code=303)

@app.post("/dm/{room_id}/send")
def dm_send(
    room_id: str,
    request: Request,
    body: str = Form(""),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    body = (body or "").strip()
    if not body:
        return RedirectResponse(f"/dm/{room_id}", status_code=303)

    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        # 所属チェック
        cur.execute("""
            SELECT 1
            FROM dm_rooms
            WHERE id=%s AND (user1_id=%s OR user2_id=%s)
        """, (room_id, me_user_id, me_user_id))
        if not cur.fetchone():
            return RedirectResponse("/", status_code=303)

        cur.execute("""
            INSERT INTO dm_messages (id, room_id, sender_id, body, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            str(uuid.uuid4()),
            room_id,
            me_user_id,
            body,
            utcnow_naive(),
        ))

        db.commit()
    finally:
        cur.close()
        db.close()

    return RedirectResponse(f"/dm/{room_id}", status_code=303)
@app.get("/dm", response_class=HTMLResponse)
def dm_list(
    request: Request,
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    db = get_db()
    cur = db.cursor()
    try:
        # ログイン判定（既存共通ロジック）
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        me_handle = get_me_handle(db, me_user_id)

        # DM一覧（相手 + 最新メッセージ）
        cur.execute("""
            SELECT
                r.id AS room_id,
                u.id AS partner_id,
                u.username,
                u.display_name,
                u.handle,
                p.icon,
                m.body,
                m.created_at
            FROM dm_rooms r
            JOIN users u
              ON u.id = CASE
                WHEN r.user1_id = %s THEN r.user2_id
                ELSE r.user1_id
              END
            LEFT JOIN profiles p ON p.user_id = u.id
            LEFT JOIN dm_messages m
              ON m.id = (
                SELECT id
                FROM dm_messages
                WHERE room_id = r.id
                ORDER BY created_at DESC
                LIMIT 1
              )
            WHERE %s IN (r.user1_id, r.user2_id)
            ORDER BY m.created_at DESC NULLS LAST
        """, (me_user_id, me_user_id))

        rooms = cur.fetchall()

    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse(
    "dm_list.html",
    {
        "request": request,
        "rooms": rooms,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "mode": "dm",
        "timedelta": timedelta,  # ← ★これ
    }
)

