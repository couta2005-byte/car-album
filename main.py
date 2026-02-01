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
# helpers: https（Render対策）, login_id validation（インスタ方式）
# ======================
def is_https_request(request: Request) -> bool:
    xf = request.headers.get("x-forwarded-proto", "")
    if xf:
        return xf.split(",")[0].strip() == "https"
    return request.url.scheme == "https"

# ✅ ログインID/handle：小文字 + 数字 + . _
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

# ======================
# ✅ auth: uid cookie（UUID）だけで自分を特定する（強い版）
# ======================
def get_me(db, uid_cookie: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    return (me_username, me_user_id)
    """
    if not uid_cookie:
        return None, None
    uid = (uid_cookie or "").strip()
    if not uid:
        return None, None

    cur = db.cursor()
    try:
        cur.execute("SELECT username, id FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if row:
            return row[0], str(row[1])
        return None, None
    finally:
        cur.close()

# ======================
# DB init（強い版：user_id(UUID)が唯一の正）
#  - RESET_DB=1 のときだけ全テーブルDROPして作り直す（試験用ならこれが最強）
# ======================
def init_db():
    RESET_DB = os.environ.get("RESET_DB", "0") == "1"

    def _do(db, cur):
        # UUID生成関数（pgcrypto）
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

        if RESET_DB:
            # ⚠️ 全消し（試験用）: 既存データ/アカウント全部消える
            cur.execute("""
                DROP TABLE IF EXISTS comment_likes CASCADE;
                DROP TABLE IF EXISTS comments CASCADE;
                DROP TABLE IF EXISTS likes CASCADE;
                DROP TABLE IF EXISTS follows CASCADE;
                DROP TABLE IF EXISTS posts CASCADE;
                DROP TABLE IF EXISTS profiles CASCADE;
                DROP TABLE IF EXISTS users CASCADE;
            """)

        # users（id UUIDが主キー）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                display_name TEXT NOT NULL,
                handle TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        # handle unique（NULL複数OK）
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_handle_unique
            ON users(handle)
            WHERE handle IS NOT NULL;
        """)

        # profiles（user_idが主キー）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                maker TEXT,
                car TEXT,
                region TEXT,
                bio TEXT,
                icon TEXT
            );
        """)

        # posts（username列は持たない。user_idのみ）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                maker TEXT,
                region TEXT,
                car TEXT,
                comment TEXT,
                image TEXT,
                created_at TIMESTAMP NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS posts_user_id_idx ON posts(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS posts_created_at_idx ON posts(created_at);")

        # follows（user_idのみ）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS follows (
                follower_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                followee_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (follower_id, followee_id)
            );
        """)

        # likes（user_idのみ）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, post_id)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS likes_post_id_idx ON likes(post_id);")

        # comments（user_idのみ）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS comments_post_id_idx ON comments(post_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS comments_user_id_idx ON comments(user_id);")

        # comment_likes（user_idのみ）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comment_likes (
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, comment_id)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS comment_likes_comment_id_idx ON comment_likes(comment_id);")

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

def get_liked_posts(db, me_user_id: Optional[str]) -> set:
    if not me_user_id:
        return set()
    cur = db.cursor()
    try:
        cur.execute("SELECT post_id FROM likes WHERE user_id=%s", (me_user_id,))
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
                    u.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.user_id IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = u.id
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
                    u.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = u.id
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
                "liked": bool(liked),
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
                "liked": False,
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
                    u.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.user_id IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = u.id
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
                    u.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles pr ON pr.user_id = u.id
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
                "liked": bool(liked),
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
                "liked": False,
            })
    return out

# ======================
# posts fetch（user_idが唯一の正）
# ======================
def fetch_posts(db, me_user_id: Optional[str], where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id,
                u.username,
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                COUNT(l.post_id) AS like_count,
                pr.icon AS user_icon
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN likes l ON p.id = l.post_id
            LEFT JOIN profiles pr ON pr.user_id = u.id
            {where_sql}
            GROUP BY
                p.id, u.username,
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
            "likes": int(r[8] or 0),
            "user_icon": r[9],
            "comments": post_comments,
            "comment_count": len(post_comments)
        })
    return posts

# ======================
# top
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        posts = fetch_posts(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id)
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
    uid: str = Cookie(default=None),
):
    q = (q or "").strip()
    maker = (maker or "").strip()
    car = (car or "").strip()
    region = (region or "").strip()

    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        liked_posts = get_liked_posts(db, me_user_id)
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
                    OR u.username ILIKE %s
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
def following(request: Request, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        posts = fetch_posts(
            db, me_user_id,
            "JOIN follows f ON p.user_id = f.followee_id WHERE f.follower_id=%s",
            (me_user_id,)
        )
        liked_posts = get_liked_posts(db, me_user_id)
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
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)

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
        liked_posts = get_liked_posts(db, me_user_id)
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
def post_detail(request: Request, post_id: int, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        liked_posts = get_liked_posts(db, me_user_id)
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
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
            INSERT INTO comments (post_id, user_id, comment, created_at)
            VALUES (%s, %s, %s, %s)
        """, (post_id, me_user_id, comment, utcnow_naive()))

    run_db(_do)
    return redirect_back(request, fallback=f"/post/{post_id}")

# ======================
# comment delete（自分のだけ）
# ======================
@app.post("/comment_delete/{comment_id}")
def delete_comment(request: Request, comment_id: int, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
def api_comment_like(comment_id: int, request: Request, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
                "INSERT INTO comment_likes (user_id, comment_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (me_user_id, comment_id)
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
def profile(request: Request, username: str, uid: str = Cookie(default=None)):
    username = unquote(username)

    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me(db, uid)

        cur.execute("SELECT id, display_name, handle FROM users WHERE username=%s", (username,))
        urow = cur.fetchone()
        if not urow:
            return RedirectResponse("/", status_code=303)

        target_user_id = str(urow[0])
        display_name = urow[1]
        handle = urow[2]

        cur.execute(
            "SELECT maker, car, region, bio, icon FROM profiles WHERE user_id=%s",
            (target_user_id,)
        )
        prof = cur.fetchone()

        posts = fetch_posts(db, me_user_id, "WHERE p.user_id=%s", (target_user_id,))

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

        liked_posts = get_liked_posts(db, me_user_id)
    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "profile": prof,
        "posts": posts,
        "me": me_username,
        "user": me_username,
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
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    display_name = (display_name or "").strip()
    if not display_name:
        display_name = me_username
    if len(display_name) > 40:
        display_name = display_name[:40]

    handle_norm = normalize_login_id(handle)  # None allowed

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
                # 既に使われてるなら handleは未設定に戻す（安全）
                final_handle = None

        cur.execute("""
            UPDATE users
            SET display_name=%s, handle=%s
            WHERE id=%s
        """, (display_name, final_handle, me_user_id))

        if icon_url:
            cur.execute("""
                INSERT INTO profiles (user_id, maker, car, region, bio, icon)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio,
                    icon=EXCLUDED.icon
            """, (me_user_id, maker, car, region, bio, icon_url))
        else:
            cur.execute("""
                INSERT INTO profiles (user_id, maker, car, region, bio)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio
            """, (me_user_id, maker, car, region, bio))

    run_db(_do)
    return RedirectResponse(f"/user/{quote(me_username)}", status_code=303)

# ======================
# follow / unfollow（user_idベース）
# ======================
@app.post("/follow/{username}")
def follow(username: str, uid: str = Cookie(default=None)):
    target_username = unquote(username)

    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
            "INSERT INTO follows (follower_id, followee_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (me_user_id, target_user_id)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target_username)}", status_code=303)

@app.post("/unfollow/{username}")
def unfollow(username: str, uid: str = Cookie(default=None)):
    target_username = unquote(username)

    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
    uid: str = Cookie(default=None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
        image_path = result.get("secure_url")

    def _do(db, cur):
        cur.execute("""
            INSERT INTO posts (user_id, maker, region, car, comment, image, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            me_user_id, maker, region, car, comment,
            image_path,
            utcnow_naive()
        ))

    run_db(_do)
    return redirect_back(request, "/")

# ======================
# ✅ auth（pbkdf2） + ✅ handleでもログインOK
# ======================
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    login_id_raw = (username or "").strip()

    db = get_db()
    cur = db.cursor()
    try:
        # 1) handleとして正規化できるなら handle で探す
        h = normalize_login_id(login_id_raw)
        if h:
            cur.execute("SELECT password, id, username FROM users WHERE handle=%s", (h,))
            row = cur.fetchone()
            if row and pwd_context.verify(password, row[0]):
                user_id = str(row[1])
                real_username = row[2]
                res = RedirectResponse("/", status_code=303)
                res.set_cookie(
                    key="uid", value=user_id,
                    httponly=True, secure=is_https_request(request), samesite="lax"
                )
                # テンプレ表示用に username cookie 欲しいなら残してもOK（任意）
                res.set_cookie(
                    key="user", value=quote(real_username),
                    httponly=True, secure=is_https_request(request), samesite="lax"
                )
                return res

        # 2) usernameでログイン（usernameはUNIQUE）
        cur.execute("SELECT password, id, username FROM users WHERE username=%s", (login_id_raw,))
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if not row or not pwd_context.verify(password, row[0]):
        return RedirectResponse("/login", status_code=303)

    user_id = str(row[1])
    real_username = row[2]

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(
        key="uid", value=user_id,
        httponly=True, secure=is_https_request(request), samesite="lax"
    )
    res.set_cookie(
        key="user", value=quote(real_username),
        httponly=True, secure=is_https_request(request), samesite="lax"
    )
    return res

@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    # ✅ 新規登録の「ログインID」はインスタルール強制
    login_id = normalize_login_id(username)
    if not login_id:
        return RedirectResponse("/register", status_code=303)

    if len(password) < 4 or len(password) > 256:
        return RedirectResponse("/register", status_code=303)

    hashed = pwd_context.hash(password)

    def _do(db, cur):
        # username / handle を login_id で揃える（最初は同じでOK）
        cur.execute("SELECT 1 FROM users WHERE username=%s LIMIT 1", (login_id,))
        if cur.fetchone() is not None:
            raise RuntimeError("username_taken")

        cur.execute("SELECT 1 FROM users WHERE handle=%s LIMIT 1", (login_id,))
        if cur.fetchone() is not None:
            raise RuntimeError("handle_taken")

        cur.execute("""
            INSERT INTO users (username, password, display_name, handle, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (login_id, hashed, login_id, login_id, utcnow_naive()))
        new_id = cur.fetchone()[0]
        return str(new_id)

    try:
        new_user_id = run_db(_do)
    except Exception:
        return RedirectResponse("/register", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(
        key="uid", value=new_user_id,
        httponly=True, secure=is_https_request(request), samesite="lax"
    )
    res.set_cookie(
        key="user", value=quote(login_id),
        httponly=True, secure=is_https_request(request), samesite="lax"
    )
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("uid")
    res.delete_cookie("user")
    return res

# ======================
# likes API（リロード無し）: user_idベース
# ======================
@app.post("/api/like/{post_id}")
def api_like(post_id: int, request: Request, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
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
                "INSERT INTO likes (user_id, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (me_user_id, post_id)
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
def delete_post(request: Request, post_id: int, uid: str = Cookie(default=None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me(db, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("SELECT 1 FROM posts WHERE id=%s AND user_id=%s", (post_id, me_user_id))
        if cur.fetchone() is None:
            return
        # CASCADEで comments/likes/comment_likes は基本消えるが、順序依存を避けるなら明示でもOK
        cur.execute("DELETE FROM posts WHERE id=%s AND user_id=%s", (post_id, me_user_id))

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
