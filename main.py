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
# 共通
# ======================
def fetch_posts(db, where_sql="", params=(), order_sql="ORDER BY p.id DESC"):
    rows = db.execute(f"""
        SELECT p.id, p.username, p.maker, p.region, p.car,
               p.comment, p.image, p.created_at,
               COUNT(l.post_id)
        FROM posts p
        LEFT JOIN likes l ON p.id = l.post_id
        {where_sql}
        GROUP BY p.id
        {order_sql}
    """, params).fetchall()

    return [{
        "id": r[0],
        "username": r[1],
        "maker": r[2],
        "region": r[3],
        "car": r[4],
        "comment": r[5],
        "image": r[6],
        "created_at": r[7],
        "likes": r[8]
    } for r in rows]

# ======================
# おすすめ（投稿＋TL）
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(default=None)):
    me = unquote(user) if user else None
    db = get_db()
    posts = fetch_posts(db)
    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "mode": "home",
        "ranking_title": ""
    })

# ======================
# 投稿
# ======================
@app.post("/post")
def post(
    maker: str = Form(""),
    region: str = Form(""),
    car: str = Form(""),
    comment: str = Form(""),
    image: UploadFile = File(None),
    user: str = Cookie(default=None)
):
    if not user:
        return RedirectResponse("/", status_code=303)

    filename = None
    if image and image.filename:
        os.makedirs("uploads", exist_ok=True)
        name = f"{uuid.uuid4()}.{image.filename.split('.')[-1]}"
        with open(f"uploads/{name}", "wb") as f:
            f.write(image.file.read())
        filename = f"/uploads/{name}"

    db = get_db()
    db.execute("""
        INSERT INTO posts (username, maker, region, car, comment, image, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (unquote(user), maker, region, car, comment, filename,
          datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    db.close()

    return RedirectResponse("/", status_code=303)

# ======================
# 🔍 検索専用
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    maker: str = Query(""),
    car: str = Query(""),
    region: str = Query(""),
    user: str = Cookie(default=None)
):
    db = get_db()
    posts = fetch_posts(
        db,
        "WHERE maker LIKE ? AND car LIKE ? AND region LIKE ?",
        (f"%{maker}%", f"%{car}%", f"%{region}%")
    )
    db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": unquote(user) if user else None,
        "maker": maker,
        "car": car,
        "region": region,
        "mode": "search"
    })

# ======================
# ランキング
# ======================
@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, period: str = "day"):
    db = get_db()

    if period == "week":
        since = datetime.now() - timedelta(days=7)
        title = "週間ランキング"
    elif period == "month":
        since = datetime.now() - timedelta(days=30)
        title = "月間ランキング"
    else:
        since = datetime.now().replace(hour=0, minute=0, second=0)
        title = "日間ランキング"

    posts = fetch_posts(
        db,
        "WHERE created_at >= ?",
        (since.strftime("%Y-%m-%d %H:%M"),),
        "ORDER BY COUNT(l.post_id) DESC LIMIT 10"
    )
    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": None,
        "mode": "ranking",
        "ranking_title": title
    })

# ======================
# 認証
# ======================
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    ok = db.execute(
        "SELECT 1 FROM users WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    db.close()

    if not ok:
        return RedirectResponse("/", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", quote(username))
    return res

@app.post("/register")
def register(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (username, password))
    db.commit()
    db.close()

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", quote(username))
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res
