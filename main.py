from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2, os, uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

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
    # DB保存/比較は常にUTCのnaiveで統一（TIMESTAMPと相性が良い）
    return datetime.utcnow()

def jst_now_aware() -> datetime:
    return datetime.now(JST)

def jst_midnight_to_utc_naive(dt_jst_aware: datetime) -> datetime:
    # JSTの0:00をUTCに変換して、naive化（DB比較用）
    dt0 = dt_jst_aware.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt0.astimezone(timezone.utc).replace(tzinfo=None)

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
# DB init
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
    run_db(_do)

@app.on_event("startup")
def startup():
    init_db()

# ======================
# common
# ======================
def redirect_back(request: Request, fallback: str = "/"):
    referer = request.headers.get("referer")
    return RedirectResponse(referer or fallback, status_code=303)

def is_https_request(request: Request) -> bool:
    return request.url.scheme == "https"

def get_liked_posts(db, me):
    if not me:
        return set()
    cur = db.cursor()
    try:
        cur.execute("SELECT post_id FROM likes WHERE username=%s", (me,))
        return {r[0] for r in cur.fetchall()}
    finally:
        cur.close()

def get_comments_map(db):
    comments = {}
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT post_id, username, comment, created_at
            FROM comments
            ORDER BY id ASC
        """)
        for c in cur.fetchall():
            comments.setdefault(c[0], []).append(c)
    finally:
        cur.close()
    return comments

# ======================
# posts fetch (JST display)
# ======================
def fetch_posts(db, where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id, p.username, p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                COUNT(l.post_id) AS like_count
            FROM posts p
            LEFT JOIN likes l ON p.id = l.post_id
            {where_sql}
            GROUP BY
                p.id, p.username, p.maker, p.region, p.car,
                p.comment, p.image, p.created_at
            {order_sql}
            {limit_sql}
        """, params)
        rows = cur.fetchall()
    finally:
        cur.close()

    comments_map = get_comments_map(db)

    return [{
        "id": r[0],
        "username": r[1],
        "maker": r[2],
        "region": r[3],
        "car": r[4],
        "comment": r[5],
        "image": r[6],
        # ★ 表示は必ずJST（UTC+9）
        "created_at": ((r[7] + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") if r[7] else ""),
        "likes": r[8],
        "comments": comments_map.get(r[0], [])
    } for r in rows]

# ======================
# top
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    try:
        posts = fetch_posts(db)
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
# search（強化：q=1入力 OR検索 + 互換維持）
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    # 新仕様：1つの入力で4項目を横断検索
    q: str = Query(default=""),
    # 旧仕様（互換維持）：maker/car/region のAND検索も残す（既存機能カットしない）
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
            # 新仕様：OR検索（メーカー/車名/地域/アカウント名）
            like = f"%{q}%"
            posts = fetch_posts(
                db,
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

            # ユーザー候補（アカウント名）
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
            # 旧仕様：AND検索（maker/car/region）
            # 旧パラメータが全部空なら、検索ページは空表示（posts=[]）でOK
            if maker or car or region:
                posts = fetch_posts(
                    db,
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
        # 新仕様
        "q": q,
        "users": users,
        # 旧仕様（互換維持）
        "maker": maker,
        "car": car,
        "region": region,
        "mode": "search"
    })

# ======================
# following TL（復旧）
# ======================
@app.get("/following", response_class=HTMLResponse)
def following(request: Request, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    db = get_db()
    try:
        posts = fetch_posts(
            db,
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
# ranking（復旧・JST境界対応）
# ======================
@app.get("/ranking", response_class=HTMLResponse)
def ranking(
    request: Request,
    period: str = Query(default="day"),
    user: str = Cookie(default=None)
):
    me = unquote(user) if user else None

    now_jst = jst_now_aware()

    if period == "week":
        since_utc_naive = (now_jst - timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
        title = "週間ランキング TOP10"
    elif period == "month":
        since_utc_naive = (now_jst - timedelta(days=30)).astimezone(timezone.utc).replace(tzinfo=None)
        title = "月間ランキング TOP10"
    else:
        since_utc_naive = jst_midnight_to_utc_naive(now_jst)
        title = "日間ランキング TOP10"

    db = get_db()
    try:
        posts = fetch_posts(
            db,
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
# profile（復旧）
# ======================
@app.get("/user/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, user: str = Cookie(default=None)):
    username = unquote(username)
    me = unquote(user) if user else None

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT maker, car, region, bio FROM profiles WHERE username=%s",
            (username,)
        )
        prof = cur.fetchone()

        posts = fetch_posts(db, "WHERE p.username=%s", (username,))

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
    finally:
        cur.close()
        db.close()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "profile": prof,
        "posts": posts,
        "me": me,
        "user": me,
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "liked_posts": liked_posts,
        "mode": "profile"
    })

# ======================
# profile edit
# ======================
@app.post("/profile/edit")
def profile_edit(
    maker: str = Form(""),
    car: str = Form(""),
    region: str = Form(""),
    bio: str = Form(""),
    user: str = Cookie(default=None)
):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    def _do(db, cur):
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
# follow / unfollow（復旧）
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
            folder="carbum/posts"
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
    if len(password) < 4 or len(password) > 256:
        return RedirectResponse("/register", status_code=303)

    hashed = pwd_context.hash(password)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s)",
            (username, hashed)
        )

    try:
        run_db(_do)
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
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res

# ======================
# likes
# ======================
@app.post("/like/{post_id}")
def like_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO likes (username, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (unquote(user), post_id)
        )

    run_db(_do)
    return RedirectResponse("/", status_code=303)

@app.post("/unlike/{post_id}")
def unlike_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    def _do(db, cur):
        cur.execute(
            "DELETE FROM likes WHERE username=%s AND post_id=%s",
            (unquote(user), post_id)
        )

    run_db(_do)
    return RedirectResponse("/", status_code=303)

# ======================
# delete post
# ======================
@app.post("/delete/{post_id}")
def delete_post(post_id: int, user: str = Cookie(default=None)):
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

        cur.execute("DELETE FROM posts WHERE id=%s", (post_id,))
        cur.execute("DELETE FROM likes WHERE post_id=%s", (post_id,))
        cur.execute("DELETE FROM comments WHERE post_id=%s", (post_id,))

    run_db(_do)
    return RedirectResponse("/", status_code=303)
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


