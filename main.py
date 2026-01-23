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

# ===== Cloudinary =====
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# ======================
# DB初期化
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

# ======================
# 起動時処理
# ======================
@app.on_event("startup")
def startup():
    init_db()

    # ★★★ 一時対処（bcrypt移行用）★★★
    # 旧平文パスワードユーザーを全削除
    def _wipe_users(db, cur):
        cur.execute("DELETE FROM users;")
    run_db(_wipe_users)
    # ★★★ 動作確認後、この3行は必ず消す ★★★

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
# ログイン / 登録
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
    hashed = pwd_context.hash(password)

    def _do(db, cur):
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s)",
            (username, hashed)
        )

    try:
        run_db(_do)
    except psycopg2.errors.UniqueViolation:
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
