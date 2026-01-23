from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2, os, uuid
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

# ★ bcrypt
from passlib.context import CryptContext

# ★ Cloudinary
import cloudinary
import cloudinary.uploader

app = FastAPI()

# ===== bcrypt 設定 =====
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ===== static / uploads =====
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")

# ===== PostgreSQL =====
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

# ★ Cloudinary 設定（環境変数）
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# ======================
# DB初期化（削除なし・IF NOT EXISTS）
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
# 共通
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
        rows = cur.fetchall()
        return {r[0] for r in rows}
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
        "created_at": r[7].strftime("%Y-%m-%d %H:%M") if r[7] else "",
        "likes": r[8],
        "comments": comments_map.get(r[0], [])
    } for r in rows]

# ======================
# トップ
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
# ログイン / 登録 画面
# ======================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

# ======================
# 検索
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    maker: str = Query(default=""),
    car: str = Query(default=""),
    region: str = Query(default=""),
    user: str = Cookie(default=None)
):
    me = unquote(user) if user else None
    db = get_db()
    try:
        posts = fetch_posts(
            db,
            "WHERE p.maker ILIKE %s AND p.car ILIKE %s AND p.region ILIKE %s",
            (f"%{maker}%", f"%{car}%", f"%{region}%")
        )
        liked_posts = get_liked_posts(db, me)
    finally:
        db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "maker": maker,
        "car": car,
        "region": region,
        "liked_posts": liked_posts,
        "mode": "search"
    })

# ======================
# フォロー中TL
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
# ランキング
# ======================
@app.get("/ranking", response_class=HTMLResponse)
def ranking(
    request: Request,
    period: str = Query(default="day"),
    user: str = Cookie(default=None)
):
    me = unquote(user) if user else None
    now = datetime.now()

    if period == "week":
        since = now - timedelta(days=7)
        title = "週間ランキング TOP10"
    elif period == "month":
        since = now - timedelta(days=30)
        title = "月間ランキング TOP10"
    else:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "日間ランキング TOP10"

    db = get_db()
    try:
        posts = fetch_posts(
            db,
            "WHERE p.created_at >= %s",
            (since,),
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
# プロフィール
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
# プロフィール編集
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
# フォロー / 解除
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
# 投稿（Cloudinary）
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
            image_path, datetime.now()
        ))

    run_db(_do)
    return redirect_back(request, "/")

# ======================
# 認証
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

    ok = False
    if row:
        ok = pwd_context.verify(password, row[0])

    if not ok:
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
    hashed = pwd_context.hash(password)

    def _do(db, cur):
        # ★ ここだけ修正：既存ユーザーを DO NOTHING で握りつぶさない
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s)",
            (username, hashed)
        )

    try:
        run_db(_do)
    except psycopg2.errors.UniqueViolation:
        # 既存username → 登録失敗（cookieを出さない）
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
# いいね
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
# 投稿削除
# ======================
@app.post("/delete/{post_id}")
def delete_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    image_path_holder = {"path": None}

    def _do(db, cur):
        cur.execute(
            "SELECT image FROM posts WHERE id=%s AND username=%s",
            (post_id, me)
        )
        row = cur.fetchone()
        if not row:
            return
        image_path_holder["path"] = row[0]

        cur.execute("DELETE FROM posts WHERE id=%s", (post_id,))
        cur.execute("DELETE FROM likes WHERE post_id=%s", (post_id,))
        cur.execute("DELETE FROM comments WHERE post_id=%s", (post_id,))

    run_db(_do)
    return RedirectResponse("/", status_code=303)
