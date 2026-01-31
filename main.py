from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2, os, re
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
# helpers: https, handle validation
# ======================
def is_https_request(request: Request) -> bool:
    return request.url.scheme == "https"

HANDLE_RE = re.compile(r"^[a-zA-Z0-9._]{3,20}$")

def normalize_handle(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if not HANDLE_RE.match(s):
        return None
    return s

def is_handle_available(db, handle: str, exclude_username: Optional[str] = None) -> bool:
    cur = db.cursor()
    try:
        if exclude_username:
            cur.execute("SELECT 1 FROM users WHERE handle=%s AND username<>%s LIMIT 1", (handle, exclude_username))
        else:
            cur.execute("SELECT 1 FROM users WHERE handle=%s LIMIT 1", (handle,))
        return cur.fetchone() is None
    finally:
        cur.close()

def suggest_handle(username: str) -> Optional[str]:
    u = (username or "").strip()
    if HANDLE_RE.match(u):
        return u
    return None

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

# ======================
# DB init（壊さない段階移行：UUID追加＋既存データ埋め）
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
        # 既存ユーザーにID埋め
        cur.execute("UPDATE users SET id = gen_random_uuid() WHERE id IS NULL;")
        # default
        cur.execute("ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid();")

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

        # likes.user_id（PKは温存、段階移行用にindex追加）
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

        # profiles.user_id（将来username変更に備える）
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
            post_id, cid, username, comment, created_at, user_icon, likes, liked = r
            out.setdefault(post_id, []).append({
                "id": cid,
                "username": username,
                "comment": comment,
                "created_at": fmt_jst(created_at),
                "user_icon": user_icon,
                "likes": int(likes or 0),
                "liked": bool(liked)
            })
        else:
            post_id, cid, username, comment, created_at, user_icon, likes = r
            out.setdefault(post_id, []).append({
                "id": cid,
                "username": username,
                "comment": comment,
                "created_at": fmt_jst(created_at),
                "user_icon": user_icon,
                "likes": int(likes or 0),
                "liked": False
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
            cid, username, comment, created_at, user_icon, likes, liked = r
            out.append({
                "id": cid,
                "username": username,
                "comment": comment,
                "created_at": fmt_jst(created_at),
                "user_icon": user_icon,
                "likes": int(likes or 0),
                "liked": bool(liked)
            })
        else:
            cid, username, comment, created_at, user_icon, likes = r
            out.append({
                "id": cid,
                "username": username,
                "comment": comment,
                "created_at": fmt_jst(created_at),
                "user_icon": user_icon,
                "likes": int(likes or 0),
                "liked": False
            })
    return out

# ======================
# posts fetch（user_id優先でユーザー情報を引く）
# ======================
def fetch_posts(db, me_user_id: Optional[str], where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id,
                COALESCE(u.username, p.username) AS username,
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
                p.id, COALESCE(u.username, p.username),
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at, pr.icon
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
        post_comments = comments_map.get(pid, [])
        posts.append({
            "id": pid,
            "username": r[1],
            "maker": r[2],
            "region": r[3],
            "car": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": fmt_jst(r[7]),
            "likes": r[8],
            "user_icon": r[9],
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
        posts = fetch_posts(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "liked_posts": liked_posts,
        "mode": "home",
        "ranking_title": "",
        "period": ""
    })

# ======================
# auth pages
# ======================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

# ======================
# search
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = Query(default=""),
    maker: str = Query(default=""),
    car: str = Query(default=""),
    region: str = Query(default=""),
    user: str = Cookie(default=None),
    uid: str = Cookie(default=None),
):
    q = (q or "").strip()
    maker = (maker or "").strip()
    car = (car or "").strip()
    region = (region or "").strip()

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        liked_posts = get_liked_posts(db, me_user_id, me_username)
        users = []
        posts = []

        if q:
            like = f"%{q}%"
            posts = fetch_posts(
                db, me_user_id,
                """
                WHERE
                    p.maker ILIKE %s
                    OR p.car ILIKE %s
                    OR p.region ILIKE %s
                    OR COALESCE(u.username, p.username) ILIKE %s
                """,
                (like, like, like, like),
                order_sql="ORDER BY p.id DESC"
            )

            cur = db.cursor()
            try:
                cur.execute("""
                    SELECT username
                    FROM users
                    WHERE username ILIKE %s
                    ORDER BY username
                    LIMIT 20
                """, (like,))
                users = [r[0] for r in cur.fetchall()]
            finally:
                cur.close()
        else:
            if maker or car or region:
                posts = fetch_posts(
                    db, me_user_id,
                    "WHERE p.maker ILIKE %s AND p.car ILIKE %s AND p.region ILIKE %s",
                    (f"%{maker}%", f"%{car}%", f"%{region}%"),
                    order_sql="ORDER BY p.id DESC"
                )
    finally:
        db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "liked_posts": liked_posts,
        "q": q,
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
        cur.execute("SELECT post_id FROM comments WHERE id=%s AND user_id=%s", (comment_id, me_user_id))
        row = cur.fetchone()
        if not row:
            return None
        post_id = row[0]

        cur.execute("DELETE FROM comment_likes WHERE comment_id=%s", (comment_id,))
        cur.execute("DELETE FROM comments WHERE id=%s AND user_id=%s", (comment_id, me_user_id))
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
# profile
# ======================
@app.get("/user/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    username = unquote(username)

    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)

        # target user (id + display)
        cur.execute("SELECT id, display_name, handle FROM users WHERE username=%s", (username,))
        urow = cur.fetchone()
        if not urow:
            return RedirectResponse("/", status_code=303)
        target_user_id = str(urow[0])
        display_name = urow[1]
        handle = urow[2]

        # profiles
        cur.execute(
            "SELECT maker, car, region, bio, icon FROM profiles WHERE user_id=%s",
            (target_user_id,)
        )
        prof = cur.fetchone()

        # posts (user_id で引く)
        posts = fetch_posts(db, me_user_id, "WHERE p.user_id=%s", (target_user_id,))

        # follow counts（idで数える）
        cur.execute("SELECT COUNT(*) FROM follows WHERE follower_id=%s", (target_user_id,))
        follow_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM follows WHERE followee_id=%s", (target_user_id,))
        follower_count = cur.fetchone()[0]

        is_following = False
        if me_user_id and me_user_id != target_user_id:
            cur.execute(
                "SELECT 1 FROM follows WHERE follower_id=%s AND followee_id=%s",
                (me_user_id, target_user_id)
            )
            is_following = cur.fetchone() is not None

        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "profile": prof,  # (maker, car, region, bio, icon)
        "posts": posts,
        "me": me_username,      # template互換（me==username）
        "user": me_username,    # nav互換
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "liked_posts": liked_posts,
        "display_name": display_name,
        "handle": handle,
        "mode": "profile"
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

    # 表示名
    display_name = (display_name or "").strip()
    if not display_name:
        display_name = me_username
    if len(display_name) > 40:
        display_name = display_name[:40]

    # handle（@ID）
    handle_norm = normalize_handle(handle)

    # icon upload（精度UP：正方形＋顔優先＋軽量）
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
        # handle 重複チェック（自分以外が使ってたら保存しない）
        final_handle = handle_norm
        if final_handle is not None:
            cur.execute("SELECT 1 FROM users WHERE handle=%s AND id<>%s LIMIT 1", (final_handle, me_user_id))
            if cur.fetchone() is not None:
                final_handle = None

        # users 更新（display_name / handle）
        cur.execute("""
            UPDATE users
            SET display_name=%s,
                handle=%s
            WHERE id=%s
        """, (display_name, final_handle, me_user_id))

        # profiles 更新（user_id をキーにする）
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

    run_db(_do)
    return RedirectResponse(f"/user/{quote(me_username)}", status_code=303)

# ======================
# follow / unfollow（user_idベース）
# ======================
@app.post("/follow/{username}")
def follow(username: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    target_username = unquote(username)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        cur = db.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE username=%s", (target_username,))
            row = cur.fetchone()
            if not row:
                return RedirectResponse("/", status_code=303)
            target_user_id = str(row[0])
        finally:
            cur.close()
    finally:
        db.close()

    if me_user_id == target_user_id:
        return RedirectResponse(f"/user/{quote(target_username)}", status_code=303)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO follows (follower, followee, follower_id, followee_id) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (me_username, target_username, me_user_id, target_user_id)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target_username)}", status_code=303)

@app.post("/unfollow/{username}")
def unfollow(username: str, user: str = Cookie(default=None), uid: str = Cookie(default=None)):
    target_username = unquote(username)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        cur = db.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE username=%s", (target_username,))
            row = cur.fetchone()
            if not row:
                return RedirectResponse("/", status_code=303)
            target_user_id = str(row[0])
        finally:
            cur.close()
    finally:
        db.close()

    def _do(db, cur):
        cur.execute(
            "DELETE FROM follows WHERE follower_id=%s AND followee_id=%s",
            (me_user_id, target_user_id)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target_username)}", status_code=303)

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
        """, (
            me_username, me_user_id, maker, region, car, comment,
            image_path,
            utcnow_naive()
        ))

    run_db(_do)
    return redirect_back(request, "/")

# ======================
# auth（pbkdf2） + ✅ uid cookie
# ======================
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = (username or "").strip()

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT password, id FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if not row or not pwd_context.verify(password, row[0]):
        return RedirectResponse("/login", status_code=303)

    user_id = str(row[1])

    res = RedirectResponse("/", status_code=303)
    # 互換: user(username) も残す
    res.set_cookie(
        key="user",
        value=quote(username),
        httponly=True,
        secure=is_https_request(request),
        samesite="lax"
    )
    # 本命: uid(UUID)
    res.set_cookie(
        key="uid",
        value=user_id,
        httponly=True,
        secure=is_https_request(request),
        samesite="lax"
    )
    return res

@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    username = (username or "").strip()
    if not username:
        return RedirectResponse("/register", status_code=303)

    if len(password) < 4 or len(password) > 256:
        return RedirectResponse("/register", status_code=303)

    hashed = pwd_context.hash(password)

    def _do(db, cur):
        # display_name 初期username
        display_name = username

        # handle は username が適合し、かつ空いてたら自動セット（被ったらNULL）
        h = suggest_handle(username)
        if h is not None and (not is_handle_available(db, h)):
            h = None

        cur.execute("""
            INSERT INTO users (username, password, display_name, handle, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (username, hashed, display_name, h, utcnow_naive()))
        new_id = cur.fetchone()[0]
        return str(new_id)

    try:
        new_user_id = run_db(_do)
    except Exception:
        return RedirectResponse("/register", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(
        key="user",
        value=quote(username),
        httponly=True,
        secure=is_https_request(request),
        samesite="lax"
    )
    res.set_cookie(
        key="uid",
        value=new_user_id,
        httponly=True,
        secure=is_https_request(request),
        samesite="lax"
    )
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
# delete post
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
        cur.execute(
            "SELECT 1 FROM posts WHERE id=%s AND user_id=%s",
            (post_id, me_user_id)
        )
        row = cur.fetchone()
        if not row:
            return

        # comment_likes → comments → likes → posts
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
# Render / uvicorn 起動
# ======================
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
