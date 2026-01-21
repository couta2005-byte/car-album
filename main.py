from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import psycopg2
import os, uuid
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

app = FastAPI()

# ======================
# static / uploads
# ======================
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")

DATABASE_URL = os.environ.get("DATABASE_URL")
DEBUG_DB = os.environ.get("DEBUG_DB") == "1"


# ======================
# DB 接続
# ======================
def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

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
# DB 初期化（PostgreSQL）
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


# Render 起動時：テーブル存在保証（削除は一切しない）
@app.on_event("startup")
def startup():
    try:
        init_db()
        print("✅ PostgreSQL tables ensured")
    except Exception as e:
        print("❌ DB init failed:", e)


# ======================
# 共通
# ======================
def redirect_back(request: Request, fallback="/"):
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


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
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT post_id, username, comment, created_at
            FROM comments
            ORDER BY id ASC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()

    comments = {}
    for r in rows:
        comments.setdefault(r[0], []).append(r)
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
            GROUP BY p.id
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
# 検索
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    maker: str = Query(""),
    car: str = Query(""),
    region: str = Query(""),
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
        "liked_posts": liked_posts,
        "maker": maker,
        "car": car,
        "region": region,
        "mode": "search"
    })


# ======================
# 以下：プロフィール / 投稿 / いいね / 認証
# ※ ここも一切削減していない
# ======================

# プロフィール
@app.get("/user/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, user: str = Cookie(default=None)):
    username = unquote(username)
    me = unquote(user) if user else None

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT maker, car, region, bio FROM profiles WHERE username=%s", (username,))
        profile = cur.fetchone()

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
        "profile": profile,
        "posts": posts,
        "me": me,
        "user": me,
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "liked_posts": liked_posts,
        "mode": "profile"
    })


# プロフィール編集
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


# フォロー
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
        cur.execute("DELETE FROM follows WHERE follower=%s AND followee=%s", (me, target))

    run_db(_do)
    return RedirectResponse(f"/user/{quote(target)}", status_code=303)


# 投稿
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
        ext = image.filename.split(".")[-1]
        filename = f"{uuid.uuid4()}.{ext}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())
        image_path = f"/uploads/{filename}"

    def _do(db, cur):
        cur.execute("""
            INSERT INTO posts (username, maker, region, car, comment, image, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (me, maker, region, car, comment, image_path, datetime.now()))

    run_db(_do)
    return redirect_back(request, "/")


# いいね
@app.post("/like/{post_id}")
def like_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO likes (username, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (me, post_id)
        )

    run_db(_do)
    return redirect_back(request, "/")


@app.post("/unlike/{post_id}")
def unlike_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)

    def _do(db, cur):
        cur.execute(
            "DELETE FROM likes WHERE username=%s AND post_id=%s",
            (me, post_id)
        )

    run_db(_do)
    return redirect_back(request, "/")


# 削除
@app.post("/delete/{post_id}")
def delete_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    image_path = None

    def _do(db, cur):
        nonlocal image_path
        cur.execute("SELECT image FROM posts WHERE id=%s AND username=%s", (post_id, me))
        row = cur.fetchone()
        if not row:
            return
        image_path = row[0]
        cur.execute("DELETE FROM posts WHERE id=%s", (post_id,))
        cur.execute("DELETE FROM likes WHERE post_id=%s", (post_id,))
        cur.execute("DELETE FROM comments WHERE post_id=%s", (post_id,))

    run_db(_do)

    if image_path:
        try:
            os.remove(image_path.lstrip("/"))
        except:
            pass

    return redirect_back(request, "/")


# ======================
# 認証
# ======================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM users WHERE username=%s AND password=%s",
            (username, password)
        )
        ok = cur.fetchone() is not None
    finally:
        cur.close()
        db.close()

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
    def _do(db, cur):
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (username, password)
        )

    run_db(_do)

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
