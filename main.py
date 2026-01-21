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


def get_db():
    if not DATABASE_URL:
        # ここで止めないと「別DB/空DB」に繋がって消えたように見える
        raise RuntimeError("DATABASE_URL is not set. Set it in Render Web Service Environment Variables.")
    return psycopg2.connect(DATABASE_URL)


# ======================
# DB 初期化（PostgreSQL）
# ======================
def init_db():
    db = get_db()
    cur = db.cursor()

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

    db.commit()
    cur.close()
    db.close()


# ✅ 重要：起動時に毎回init_db()を回さない（これが「消えた」原因の温床）
# 必要なときだけ、RenderのEnvironmentで INIT_DB=1 を一瞬だけ入れて起動する
if os.environ.get("INIT_DB") == "1":
    init_db()


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
    cur.execute("SELECT post_id FROM likes WHERE username=%s", (me,))
    rows = cur.fetchall()
    cur.close()
    return {r[0] for r in rows}


def get_comments_map(db):
    cur = db.cursor()
    cur.execute("""
        SELECT post_id, username, comment, created_at
        FROM comments
        ORDER BY id ASC
    """)
    rows = cur.fetchall()
    cur.close()

    comments = {}
    for r in rows:
        comments.setdefault(r[0], []).append(r)
    return comments


def fetch_posts(db, where_sql="", params=(), order_sql="ORDER BY p.id DESC", limit_sql=""):
    cur = db.cursor()
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
        "created_at": r[7].strftime("%Y-%m-%d %H:%M"),
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

    posts = fetch_posts(db)
    liked_posts = get_liked_posts(db, me)

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

    posts = fetch_posts(
        db,
        "WHERE p.maker ILIKE %s AND p.car ILIKE %s AND p.region ILIKE %s",
        (f"%{maker}%", f"%{car}%", f"%{region}%")
    )

    liked_posts = get_liked_posts(db, me)
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
# フォロー中
# ======================
@app.get("/following", response_class=HTMLResponse)
def following(request: Request, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    db = get_db()

    posts = fetch_posts(
        db,
        "JOIN follows f ON p.username = f.followee WHERE f.follower=%s",
        (me,)
    )

    liked_posts = get_liked_posts(db, me)
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
    period: str = Query("day"),
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
    posts = fetch_posts(
        db,
        "WHERE p.created_at >= %s",
        (since,),
        order_sql="ORDER BY like_count DESC, p.id DESC",
        limit_sql="LIMIT 10"
    )

    liked_posts = get_liked_posts(db, me)
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
    db = get_db()
    cur = db.cursor()

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

    db.commit()
    cur.close()
    db.close()

    return RedirectResponse(f"/user/{quote(me)}", status_code=303)


# ======================
# フォロー
# ======================
@app.post("/follow/{username}")
def follow(username: str, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    target = unquote(username)

    if me == target:
        return RedirectResponse(f"/user/{quote(target)}", status_code=303)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO follows (follower, followee) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (me, target)
    )
    db.commit()
    cur.close()
    db.close()

    return RedirectResponse(f"/user/{quote(target)}", status_code=303)


@app.post("/unfollow/{username}")
def unfollow(username: str, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    target = unquote(username)

    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM follows WHERE follower=%s AND followee=%s", (me, target))
    db.commit()
    cur.close()
    db.close()

    return RedirectResponse(f"/user/{quote(target)}", status_code=303)


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
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    image_path = None

    if image and image.filename:
        ext = image.filename.split(".")[-1]
        filename = f"{uuid.uuid4()}.{ext}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())
        image_path = f"/uploads/{filename}"

    db = get_db()
    cur = db.cursor()

    cur.execute("""
        INSERT INTO posts (username, maker, region, car, comment, image, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (me, maker, region, car, comment, image_path, datetime.now()))

    db.commit()
    cur.close()
    db.close()

    return redirect_back(request, "/")


# ======================
# いいね
# ======================
@app.post("/like/{post_id}")
def like_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO likes (username, post_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (unquote(user), post_id)
    )
    db.commit()
    cur.close()
    db.close()

    return RedirectResponse("/", status_code=303)


@app.post("/unlike/{post_id}")
def unlike_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "DELETE FROM likes WHERE username=%s AND post_id=%s",
        (unquote(user), post_id)
    )
    db.commit()
    cur.close()
    db.close()

    return RedirectResponse("/", status_code=303)


# ======================
# 投稿削除
# ======================
@app.post("/delete/{post_id}")
def delete_post(post_id: int, user: str = Cookie(default=None)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    me = unquote(user)
    db = get_db()
    cur = db.cursor()

    cur.execute(
        "SELECT image FROM posts WHERE id=%s AND username=%s",
        (post_id, me)
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        db.close()
        return RedirectResponse("/", status_code=303)

    image_path = row[0]

    cur.execute("DELETE FROM posts WHERE id=%s", (post_id,))
    cur.execute("DELETE FROM likes WHERE post_id=%s", (post_id,))
    cur.execute("DELETE FROM comments WHERE post_id=%s", (post_id,))
    db.commit()

    cur.close()
    db.close()

    if image_path:
        try:
            os.remove(image_path.lstrip("/"))
        except:
            pass

    return RedirectResponse("/", status_code=303)


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
    cur.execute(
        "SELECT 1 FROM users WHERE username=%s AND password=%s",
        (username, password)
    )
    ok = cur.fetchone()
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
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (username, password) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (username, password)
    )
    db.commit()
    cur.close()
    db.close()

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
