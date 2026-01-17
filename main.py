from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import sqlite3, os, uuid
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

DB = "app.db"


def get_db():
    return sqlite3.connect(DB)


# ======================
# DB初期化
# ======================
def init_db():
    db = get_db()
    db.executescript("""
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        maker TEXT,
        region TEXT,
        car TEXT,
        comment TEXT,
        image TEXT,
        created_at TEXT
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER,
        username TEXT,
        comment TEXT,
        created_at TEXT
    );
    """)
    db.commit()
    db.close()


init_db()


# ======================
# 共通処理
# ======================
def redirect_back(request: Request, fallback="/"):
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def get_liked_posts(db, me):
    if not me:
        return set()
    return {r[0] for r in db.execute(
        "SELECT post_id FROM likes WHERE username=?",
        (me,)
    ).fetchall()}


def get_comments_map(db):
    comments = {}
    for c in db.execute("""
        SELECT post_id, username, comment, created_at
        FROM comments
        ORDER BY id ASC
    """):
        comments.setdefault(c[0], []).append(c)
    return comments


def fetch_posts(db, where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    rows = db.execute(f"""
        SELECT
            p.id, p.username, p.maker, p.region, p.car,
            p.comment, p.image, p.created_at,
            COUNT(l.post_id)
        FROM posts p
        LEFT JOIN likes l ON p.id = l.post_id
        {where_sql}
        GROUP BY p.id
        {order_sql}
        {limit_sql}
    """, params).fetchall()

    comments_map = get_comments_map(db)

    return [{
        "id": r[0],
        "username": r[1],
        "maker": r[2],
        "region": r[3],
        "car": r[4],
        "comment": r[5],
        "image": r[6],
        "created_at": r[7],
        "likes": r[8],
        "comments": comments_map.get(r[0], [])
    } for r in rows]


# ======================
# ページ
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    posts = fetch_posts(db)
    liked_posts = get_liked_posts(db, me)
    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked_posts,
        "mode": "home"
    })


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, maker="", car="", region="", user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    posts = fetch_posts(
        db,
        "WHERE p.maker LIKE ? AND p.car LIKE ? AND p.region LIKE ?",
        (f"%{maker}%", f"%{car}%", f"%{region}%")
    )
    liked_posts = get_liked_posts(db, me)
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
# 投稿
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
        return RedirectResponse("/", status_code=303)

    username = unquote(user)
    image_path = None

    if image and image.filename:
        os.makedirs("uploads", exist_ok=True)
        filename = f"{uuid.uuid4()}.{image.filename.split('.')[-1]}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())
        image_path = f"/uploads/{filename}"

    db = get_db()
    db.execute("""
        INSERT INTO posts (username, maker, region, car, comment, image, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        username, maker, region, car, comment,
        image_path, datetime.now().strftime("%Y-%m-%d %H:%M")
    ))
    db.commit()
    db.close()

    return redirect_back(request)


# ======================
# 認証
# ======================
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM users WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    db.close()

    if not row:
        return RedirectResponse("/", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", quote(username))
    return res


@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res
