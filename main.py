from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2, os, re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from typing import Optional, Dict, List, Any

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
    # username が handle として使えそうなら候補にする
    u = (username or "").strip()
    if HANDLE_RE.match(u):
        return u
    return None

# ======================
# DB init（既存DBでも壊れないように追加カラムも保証）
# ======================
def init_db():
    def _do(db, cur):
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

        # ★ profiles に icon カラム（無ければ追加）
        cur.execute("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS icon TEXT;")

        # ★ users に display_name / handle / created_at（無ければ追加）
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS handle TEXT;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")

        # ★ handle はユニーク（NULLは複数OK）
        # Postgres: 部分ユニークindex
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_handle_unique
            ON users(handle)
            WHERE handle IS NOT NULL;
        """)

        # ★ 既存ユーザーの埋め（NULLを埋めておく）
        cur.execute("UPDATE users SET display_name = username WHERE display_name IS NULL;")
        cur.execute("UPDATE users SET created_at = NOW() WHERE created_at IS NULL;")

        # ★ コメントいいね
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comment_likes (
            username TEXT,
            comment_id INTEGER,
            PRIMARY KEY (username, comment_id)
        );
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

def get_liked_posts(db, me):
    if not me:
        return set()
    cur = db.cursor()
    try:
        cur.execute("SELECT post_id FROM likes WHERE username=%s", (me,))
        return {r[0] for r in cur.fetchall()}
    finally:
        cur.close()

# ======================
# comments fetch（一覧・ランキング用：comment_id / likes / liked まで返す）
# ======================
def fetch_comments_for_posts(db, post_ids: List[int], me: Optional[str]) -> Dict[int, List[Dict[str, Any]]]:
    if not post_ids:
        return {}

    placeholders = ",".join(["%s"] * len(post_ids))

    cur = db.cursor()
    try:
        if me:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    c.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.username IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                LEFT JOIN profiles pr ON c.username = pr.username
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                LEFT JOIN comment_likes mycl
                    ON mycl.comment_id = c.id AND mycl.username = %s
                WHERE c.post_id IN ({placeholders})
                ORDER BY c.id ASC
            """
            cur.execute(sql, (me, *post_ids))
        else:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    c.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                LEFT JOIN profiles pr ON c.username = pr.username
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
        if me:
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
# post_detail用（単体で読む：likes/liked込み）
# ======================
def fetch_comments_for_post_detail(db, post_id: int, me: Optional[str]) -> List[Dict[str, Any]]:
    cur = db.cursor()
    try:
        if me:
            cur.execute("""
                SELECT
                    c.id,
                    c.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes,
                    CASE WHEN mycl.username IS NULL THEN 0 ELSE 1 END AS liked
                FROM comments c
                LEFT JOIN profiles pr ON c.username = pr.username
                LEFT JOIN (
                    SELECT comment_id, COUNT(*) AS like_count
                    FROM comment_likes
                    GROUP BY comment_id
                ) clc ON clc.comment_id = c.id
                LEFT JOIN comment_likes mycl
                    ON mycl.comment_id = c.id AND mycl.username = %s
                WHERE c.post_id = %s
                ORDER BY c.id ASC
            """, (me, post_id))
        else:
            cur.execute("""
                SELECT
                    c.id,
                    c.username,
                    c.comment,
                    c.created_at,
                    pr.icon AS user_icon,
                    COALESCE(clc.like_count, 0) AS likes
                FROM comments c
                LEFT JOIN profiles pr ON c.username = pr.username
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
        if me:
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
# posts fetch（★user_icon含む + comments に id/likes/liked を入れる）
# ======================
def fetch_posts(db, me: Optional[str], where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id, p.username, p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                COUNT(l.post_id) AS like_count,
                pr.icon AS user_icon
            FROM posts p
            LEFT JOIN likes l ON p.id = l.post_id
            LEFT JOIN profiles pr ON p.username = pr.username
            {where_sql}
            GROUP BY
                p.id, p.username, p.maker, p.region, p.car,
                p.comment, p.image, p.created_at, pr.icon
            {order_sql}
            {limit_sql}
        """, params)
        rows = cur.fetchall()
    finally:
        cur.close()

    post_ids = [r[0] for r in rows]
    comments_map = fetch_comments_for_posts(db, post_ids, me)

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
def index(request: Request, user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    try:
        posts = fetch_posts(db, me)
        liked_posts = get_liked_posts(db, me)
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
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
    user: str = Cookie(default=None)
):
    me = unquote(user) if user else None
    q = (q or "").strip()
    maker = (maker or "").strip()
    car = (car or "").strip()
    region = (region or "").strip()

    db = get_db()
    try:
        liked_posts = get_liked_posts(db, me)
        users = []
        posts = []

        if q:
            like = f"%{q}%"
            posts = fetch_posts(
                db, me,
                """
                WHERE
                    p.maker ILIKE %s
                    OR p.car ILIKE %s
                    OR p.region ILIKE %s
                    OR p.username ILIKE %s
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
                    db, me,
                    "WHERE p.maker ILIKE %s AND p.car ILIKE %s AND p.region ILIKE %s",
                    (f"%{maker}%", f"%{car}%", f"%{region}%"),
                    order_sql="ORDER BY p.id DESC"
                )

    finally:
        db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": me,
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
def following(request: Request, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    db = get_db()
    try:
        posts = fetch_posts(
            db, me,
            "JOIN follows f ON p.username = f.followee WHERE f.follower=%s",
            (me,)
        )
        liked_posts = get_liked_posts(db, me)
    finally:
        db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
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
    user: str = Cookie(default=None)
):
    me = unquote(user) if user else None
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

    db = get_db()
    try:
        posts = fetch_posts(
            db, me,
            "WHERE p.created_at >= %s",
            (since_utc_naive,),
            order_sql="ORDER BY like_count DESC, p.id DESC",
            limit_sql="LIMIT 10"
        )
        liked_posts = get_liked_posts(db, me)
    finally:
        db.close()

    return templates.TemplateResponse("ranking.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked_posts,
        "mode": f"ranking_{period}",
        "ranking_title": title,
        "period": period
    })

# ======================
# post detail
# ======================
@app.get("/post/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    try:
        liked_posts = get_liked_posts(db, me)
        posts = fetch_posts(db, me, "WHERE p.id=%s", (post_id,))
        if not posts:
            return RedirectResponse("/", status_code=303)

        post = posts[0]
        # detailは確実にlikes/liked付きで再取得
        post_comments = fetch_comments_for_post_detail(db, post_id, me)
        post["comments"] = post_comments
        post["comment_count"] = len(post_comments)
    finally:
        db.close()

    return templates.TemplateResponse("post_detail.html", {
        "request": request,
        "post": post,
        "user": me,
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
    user: str = Cookie(default=None)
):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    comment = (comment or "").strip()
    if not comment:
        return redirect_back(request, fallback=f"/post/{post_id}")

    def _do(db, cur):
        cur.execute("SELECT 1 FROM posts WHERE id=%s", (post_id,))
        if cur.fetchone() is None:
            return
        cur.execute("""
            INSERT INTO comments (post_id, username, comment, created_at)
            VALUES (%s, %s, %s, %s)
        """, (post_id, me, comment, utcnow_naive()))

    run_db(_do)
    return redirect_back(request, fallback=f"/post/{post_id}")

# ======================
# comment delete（自分のだけ）
# ======================
@app.post("/comment_delete/{comment_id}")
def delete_comment(request: Request, comment_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    def _do(db, cur):
        cur.execute("SELECT post_id FROM comments WHERE id=%s AND username=%s", (comment_id, me))
        row = cur.fetchone()
        if not row:
            return None
        post_id = row[0]

        cur.execute("DELETE FROM comment_likes WHERE comment_id=%s", (comment_id,))
        cur.execute("DELETE FROM comments WHERE id=%s AND username=%s", (comment_id, me))

        return post_id

    post_id = run_db(_do)
    fallback = f"/post/{post_id}" if post_id else "/"
    return redirect_back(request, fallback=fallback)

# ======================
# ✅ comment like API（リロード無し）
# ======================
@app.post("/api/comment_like/{comment_id}")
def api_comment_like(comment_id: int, request: Request, user: str = Cookie(default=None)):
    if not user:
        return JSONResponse({"ok": False, "error": "login_required"}, status_code=401)

    me = unquote(user)

    def _do(db, cur):
        cur.execute("SELECT 1 FROM comments WHERE id=%s", (comment_id,))
        if cur.fetchone() is None:
            return {"ok": False, "error": "not_found"}

        cur.execute("SELECT 1 FROM comment_likes WHERE username=%s AND comment_id=%s", (me, comment_id))
        liked = cur.fetchone() is not None

        if liked:
            cur.execute("DELETE FROM comment_likes WHERE username=%s AND comment_id=%s", (me, comment_id))
            liked = False
        else:
            cur.execute(
                "INSERT INTO comment_likes (username, comment_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (me, comment_id)
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
def profile(request: Request, username: str, user: str = Cookie(default=None)):
    username = unquote(username)
    me = unquote(user) if user else None

    db = get_db()
    cur = db.cursor()
    try:
        # profiles
        cur.execute(
            "SELECT maker, car, region, bio, icon FROM profiles WHERE username=%s",
            (username,)
        )
        prof = cur.fetchone()

        # posts
        posts = fetch_posts(db, me, "WHERE p.username=%s", (username,))

        # follow counts
        cur.execute("SELECT COUNT(*) FROM follows WHERE follower=%s", (username,))
        follow_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM follows WHERE followee=%s", (username,))
        follower_count = cur.fetchone()[0]

        is_following = False
        if me and me != username:
            cur.execute(
                "SELECT 1 FROM follows WHERE follower=%s AND followee=%s",
                (me, username)
            )
            is_following = cur.fetchone() is not None

        liked_posts = get_liked_posts(db, me)

        # ✅ display_name / handle
        cur.execute("SELECT display_name, handle FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        display_name = row[0] if row else None
        handle = row[1] if row else None
    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "profile": prof,  # (maker, car, region, bio, icon)
        "posts": posts,
        "me": me,
        "user": me,
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "liked_posts": liked_posts,
        "display_name": display_name,
        "handle": handle,
        "mode": "profile"
    })

# ======================
# profile edit（★icon upload + display_name/handle）
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
    user: str = Cookie(default=None)
):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    # 表示名
    display_name = (display_name or "").strip()
    if not display_name:
        display_name = me
    if len(display_name) > 40:
        display_name = display_name[:40]

    # handle（@ID）
    handle_norm = normalize_handle(handle)  # invalid => None
    # invalid を入力してたら「保存しない」挙動の方がストレス少ないので None に落とす
    # もし「無効ならエラー表示」したいならここで redirect してクエリに error 付ける

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
        # handle の重複チェック（自分以外が使ってたら保存しない）
        final_handle = handle_norm
        if final_handle is not None:
            cur.execute("SELECT 1 FROM users WHERE handle=%s AND username<>%s LIMIT 1", (final_handle, me))
            if cur.fetchone() is not None:
                final_handle = None  # 被ってたら諦める（後で変えられる）

        # users 更新（display_name / handle）
        cur.execute("""
            UPDATE users
            SET display_name=%s,
                handle=%s
            WHERE username=%s
        """, (display_name, final_handle, me))

        # profiles 更新（maker/car/region/bio/icon）
        if icon_url:
            cur.execute("""
                INSERT INTO profiles (username, maker, car, region, bio, icon)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio,
                    icon=EXCLUDED.icon
            """, (me, maker, car, region, bio, icon_url))
        else:
            cur.execute("""
                INSERT INTO profiles (username, maker, car, region, bio)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username)
                DO UPDATE SET
                    maker=EXCLUDED.maker,
                    car=EXCLUDED.car,
                    region=EXCLUDED.region,
                    bio=EXCLUDED.bio
            """, (me, maker, car, region, bio))

    run_db(_do)
    return RedirectResponse(f"/user/{quote(me)}", status_code=303)

# ======================
# follow / unfollow
# ======================
@app.post("/follow/{username}")
def follow(username: str, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    target = unquote(username)

    if me == target:
        return RedirectResponse(f"/user/{quote(target)}", status_code=303)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO follows (follower, followee) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (me, target)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target)}", status_code=303)

@app.post("/unfollow/{username}")
def unfollow(username: str, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    target = unquote(username)

    def _do(db, cur):
        cur.execute(
            "DELETE FROM follows WHERE follower=%s AND followee=%s",
            (me, target)
        )

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target)}", status_code=303)

# ======================
# post（UTC保存で統一）
# ======================
@app.post("/post")
def post(
    request: Request,
    maker: str = Form(""),
    region: str = Form(""),
    car: str = Form(""),
    comment: str = Form(""),
    image: UploadFile = File(None),
    user: str = Cookie(default=None)
):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
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
            INSERT INTO posts (username, maker, region, car, comment, image, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            me, maker, region, car, comment,
            image_path,
            utcnow_naive()
        ))

    run_db(_do)
    return redirect_back(request, "/")

# ======================
# auth（pbkdf2）
# ======================
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT password FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if not row or not pwd_context.verify(password, row[0]):
        return RedirectResponse("/login", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(
        key="user",
        value=quote(username),
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
        # display_name は初期 username
        display_name = username

        # handle は username が適合し、かつ空いてたら自動セット（被ったらNULL）
        h = suggest_handle(username)
        if h is not None and (not is_handle_available(db, h)):
            h = None

        cur.execute("""
            INSERT INTO users (username, password, display_name, handle, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (username, hashed, display_name, h, utcnow_naive()))

    try:
        run_db(_do)
    except Exception:
        # username重複など
        return RedirectResponse("/register", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie(
        key="user",
        value=quote(username),
        httponly=True,
        secure=is_https_request(request),
        samesite="lax"
    )
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res

# ======================
# likes（元ページへ戻る）
# ======================
@app.post("/like/{post_id}")
def like_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO likes (username, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (unquote(user), post_id)
        )

    run_db(_do)
    return redirect_back(request, fallback=f"/post/{post_id}")

@app.post("/unlike/{post_id}")
def unlike_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    def _do(db, cur):
        cur.execute(
            "DELETE FROM likes WHERE username=%s AND post_id=%s",
            (unquote(user), post_id)
        )

    run_db(_do)
    return redirect_back(request, fallback=f"/post/{post_id}")

# ======================
# likes API（リロード無し）
# ======================
@app.post("/api/like/{post_id}")
def api_like(post_id: int, request: Request, user: str = Cookie(default=None)):
    if not user:
        return JSONResponse({"ok": False, "error": "login_required"}, status_code=401)

    me = unquote(user)

    def _do(db, cur):
        cur.execute("SELECT 1 FROM likes WHERE username=%s AND post_id=%s", (me, post_id))
        liked = cur.fetchone() is not None

        if liked:
            cur.execute("DELETE FROM likes WHERE username=%s AND post_id=%s", (me, post_id))
            liked = False
        else:
            cur.execute(
                "INSERT INTO likes (username, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (me, post_id)
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
def delete_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    def _do(db, cur):
        cur.execute(
            "SELECT image FROM posts WHERE id=%s AND username=%s",
            (post_id, me)
        )
        row = cur.fetchone()
        if not row:
            return

        # ✅ 先に comment_likes を消す（comments を消した後だと subselect が空になる）
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
