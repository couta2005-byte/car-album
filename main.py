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


# ======================
# DB
# ======================
def get_db():
    return sqlite3.connect(DB)


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
def current_user(user: str | None):
    return unquote(user) if user else None


def redirect_back(request: Request, fallback="/"):
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def get_liked_posts(db, me):
    if not me:
        return set()
    return {r[0] for r in db.execute(
        "SELECT post_id FROM likes WHERE username=?", (me,)
    ).fetchall()}


def get_comments_map(db):
    m = {}
    for c in db.execute(
        "SELECT post_id, username, comment, created_at FROM comments ORDER BY id"
    ):
        m.setdefault(c[0], []).append(c)
    return m


def fetch_posts(db, where="", params=(), order="ORDER BY p.id DESC", limit=""):
    rows = db.execute(f"""
        SELECT p.id, p.username, p.maker, p.region, p.car,
               p.comment, p.image, p.created_at,
               COUNT(l.post_id)
        FROM posts p
        LEFT JOIN likes l ON p.id = l.post_id
        {where}
        GROUP BY p.id
        {order}
        {limit}
    """, params).fetchall()

    comments = get_comments_map(db)

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
        "comments": comments.get(r[0], [])
    } for r in rows]


# ======================
# HOME
# ======================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(default=None)):
    me = current_user(user)
    db = get_db()
    posts = fetch_posts(db)
    liked = get_liked_posts(db, me)
    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked,
        "mode": "home",
        "ranking_title": "",
        "period": ""
    })


# ======================
# SEARCH
# ======================
@app.get("/search", response_class=HTMLResponse)
def search(request: Request, maker="", car="", region="", user: str = Cookie(default=None)):
    me = current_user(user)
    db = get_db()
    posts = fetch_posts(
        db,
        "WHERE p.maker LIKE ? AND p.car LIKE ? AND p.region LIKE ?",
        (f"%{maker}%", f"%{car}%", f"%{region}%")
    )
    liked = get_liked_posts(db, me)
    db.close()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked,
        "maker": maker,
        "car": car,
        "region": region,
        "mode": "search"
    })


# ======================
# FOLLOWING
# ======================
@app.get("/following", response_class=HTMLResponse)
def following(request: Request, user: str = Cookie(default=None)):
    me = current_user(user)
    if not me:
        return RedirectResponse("/", 303)

    db = get_db()
    posts = fetch_posts(
        db,
        "JOIN follows f ON p.username=f.followee WHERE f.follower=?",
        (me,)
    )
    liked = get_liked_posts(db, me)
    db.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked,
        "mode": "following",
        "ranking_title": "",
        "period": ""
    })


# ======================
# RANKING
# ======================
@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, period="day", user: str = Cookie(default=None)):
    me = current_user(user)
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
    posts = fetch_posts(
        db,
        "WHERE p.created_at >= ?",
        (since.strftime("%Y-%m-%d %H:%M"),),
        "ORDER BY likes DESC, p.id DESC",
        "LIMIT 10"
    )
    liked = get_liked_posts(db, me)
    db.close()

    return templates.TemplateResponse("ranking.html", {
        "request": request,
        "posts": posts,
        "user": me,
        "liked_posts": liked,
        "mode": f"ranking_{period}",
        "ranking_title": title,
        "period": period
    })


# ======================
# POST
# ======================
@app.post("/post")
def post(request: Request, maker: str = Form(""), region: str = Form(""),
         car: str = Form(""), comment: str = Form(""),
         image: UploadFile = File(None), user: str = Cookie(default=None)):
    me = current_user(user)
    if not me:
        return RedirectResponse("/", 303)

    path = None
    if image and image.filename:
        os.makedirs("uploads", exist_ok=True)
        name = f"{uuid.uuid4()}.{image.filename.split('.')[-1]}"
        with open(f"uploads/{name}", "wb") as f:
            f.write(image.file.read())
        path = f"/uploads/{name}"

    db = get_db()
    db.execute("""
        INSERT INTO posts (username, maker, region, car, comment, image, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (me, maker, region, car, comment, path,
          datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    db.close()

    return redirect_back(request)


# ======================
# AUTH
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
        return RedirectResponse("/", 303)

    res = RedirectResponse("/", 303)
    res.set_cookie("user", quote(username))
    return res


@app.post("/register")
def register(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (username, password))
    db.commit()
    db.close()

    res = RedirectResponse("/", 303)
    res.set_cookie("user", quote(username))
    return res


@app.post("/logout")
def logout():
    res = RedirectResponse("/", 303)
    res.delete_cookie("user")
    return res
