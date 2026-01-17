from fastapi import FastAPI, Request, Form, UploadFile, File, Cookie
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import sqlite3, os, uuid
from datetime import datetime
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


def get_me(user_cookie: str | None):
    return unquote(user_cookie) if user_cookie else None


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
    me = get_me(user)
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
    me = get_me(user)
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
# 認証（画面）
# ======================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: str = Cookie(default=None)):
    me = get_me(user)
    # 既にログイン済みならトップへ
    if me:
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "user": None,
        "mode": "login"
    })


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: str = Cookie(default=None)):
    me = get_me(user)
    if me:
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse("register.html", {
        "request": request,
        "user": None,
        "mode": "register"
    })


# ======================
# 認証（処理）
# ======================
@app.post("/register")
def register(username: str = Form(...), password: str = Form(...)):
    db = get_db()

    exists = db.execute(
        "SELECT 1 FROM users WHERE username=?",
        (username,)
    ).fetchone()

    if exists:
        db.close()
        return RedirectResponse("/register", status_code=303)

    db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
    # プロフィール空で作成
    db.execute("INSERT OR IGNORE INTO profiles (username, maker, car, region, bio) VALUES (?, '', '', '', '')", (username,))
    db.commit()
    db.close()

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", quote(username))
    return res


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM users WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    db.close()

    if not row:
        return RedirectResponse("/login", status_code=303)

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", quote(username))
    return res


@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res


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
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    image_path = None
    if image and image.filename:
        os.makedirs("uploads", exist_ok=True)
        ext = image.filename.split(".")[-1]
        filename = f"{uuid.uuid4()}.{ext}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())
        image_path = f"/uploads/{filename}"

    db = get_db()
    db.execute("""
        INSERT INTO posts (username, maker, region, car, comment, image, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        me, maker, region, car, comment,
        image_path, datetime.now().strftime("%Y-%m-%d %H:%M")
    ))
    db.commit()
    db.close()

    return redirect_back(request)


# ======================
# プロフィール（表示/編集）
# ======================
@app.get("/user/{username}", response_class=HTMLResponse)
def user_page(request: Request, username: str, user: str = Cookie(default=None)):
    me = get_me(user)
    db = get_db()

    profile = db.execute(
        "SELECT maker, car, region, bio FROM profiles WHERE username=?",
        (username,)
    ).fetchone()

    posts = fetch_posts(db, "WHERE p.username=?", (username,))
    liked_posts = get_liked_posts(db, me)

    follow_count = db.execute("SELECT COUNT(*) FROM follows WHERE follower=?", (username,)).fetchone()[0]
    follower_count = db.execute("SELECT COUNT(*) FROM follows WHERE followee=?", (username,)).fetchone()[0]

    is_following = False
    if me and me != username:
        is_following = db.execute(
            "SELECT 1 FROM follows WHERE follower=? AND followee=?",
            (me, username)
        ).fetchone() is not None

    db.close()

    return templates.TemplateResponse("user.html", {
        "request": request,
        "username": username,
        "me": me,
        "user": me,  # nav用
        "profile": profile,
        "posts": posts,
        "liked_posts": liked_posts,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "is_following": is_following,
        "mode": "profile"
    })


@app.post("/profile/edit")
def profile_edit(
    request: Request,
    maker: str = Form(""),
    car: str = Form(""),
    region: str = Form(""),
    bio: str = Form(""),
    user: str = Cookie(default=None)
):
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    db.execute("""
        INSERT INTO profiles (username, maker, car, region, bio)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
          maker=excluded.maker,
          car=excluded.car,
          region=excluded.region,
          bio=excluded.bio
    """, (me, maker, car, region, bio))
    db.commit()
    db.close()

    return redirect_back(request, f"/user/{me}")


# ======================
# フォロー
# ======================
@app.post("/follow/{username}")
def follow(request: Request, username: str, user: str = Cookie(default=None)):
    me = get_me(user)
    if not me or me == username:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    db.execute("INSERT OR IGNORE INTO follows (follower, followee) VALUES (?, ?)", (me, username))
    db.commit()
    db.close()
    return redirect_back(request, f"/user/{username}")


@app.post("/unfollow/{username}")
def unfollow(request: Request, username: str, user: str = Cookie(default=None)):
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    db.execute("DELETE FROM follows WHERE follower=? AND followee=?", (me, username))
    db.commit()
    db.close()
    return redirect_back(request, f"/user/{username}")


# ======================
# いいね
# ======================
@app.post("/like/{post_id}")
def like(request: Request, post_id: int, user: str = Cookie(default=None)):
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    db.execute("INSERT OR IGNORE INTO likes (username, post_id) VALUES (?, ?)", (me, post_id))
    db.commit()
    db.close()
    return redirect_back(request)


@app.post("/unlike/{post_id}")
def unlike(request: Request, post_id: int, user: str = Cookie(default=None)):
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    db.execute("DELETE FROM likes WHERE username=? AND post_id=?", (me, post_id))
    db.commit()
    db.close()
    return redirect_back(request)


# ======================
# 削除
# ======================
@app.post("/delete/{post_id}")
def delete_post(request: Request, post_id: int, user: str = Cookie(default=None)):
    me = get_me(user)
    if not me:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    row = db.execute("SELECT username, image FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        db.close()
        return redirect_back(request)

    owner, img = row
    if owner != me:
        db.close()
        return redirect_back(request)

    db.execute("DELETE FROM posts WHERE id=?", (post_id,))
    db.execute("DELETE FROM likes WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM comments WHERE post_id=?", (post_id,))
    db.commit()
    db.close()

    # ローカルのuploads削除（存在すれば）
    if img and img.startswith("/uploads/"):
        path = img.replace("/uploads/", "uploads/")
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass

    return redirect_back(request)
