from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3, os, uuid
from datetime import datetime

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

DB = "app.db"

# --------------------
# DB
# --------------------
def get_db():
    return sqlite3.connect(DB)

# --------------------
# 共通：ログインユーザー
# --------------------
def get_login_user(request: Request):
    return request.cookies.get("user")

# --------------------
# 投稿取得系
# --------------------
def get_posts():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, username, maker, car, region, comment, image, created_at, likes
        FROM posts
        ORDER BY id DESC
    """)
    rows = cur.fetchall()
    db.close()

    posts = []
    for r in rows:
        posts.append({
            "id": r[0],
            "username": r[1],
            "maker": r[2],
            "car": r[3],
            "region": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": r[7],
            "likes": r[8],
        })
    return posts

def search_posts(maker, car, region):
    db = get_db()
    cur = db.cursor()

    query = "SELECT id, username, maker, car, region, comment, image, created_at, likes FROM posts WHERE 1=1"
    params = []

    if maker:
        query += " AND maker LIKE ?"
        params.append(f"%{maker}%")
    if car:
        query += " AND car LIKE ?"
        params.append(f"%{car}%")
    if region:
        query += " AND region LIKE ?"
        params.append(f"%{region}%")

    query += " ORDER BY id DESC"

    cur.execute(query, params)
    rows = cur.fetchall()
    db.close()

    posts = []
    for r in rows:
        posts.append({
            "id": r[0],
            "username": r[1],
            "maker": r[2],
            "car": r[3],
            "region": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": r[7],
            "likes": r[8],
        })
    return posts

def get_user_posts(username):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, username, maker, car, region, comment, image, created_at, likes
        FROM posts
        WHERE username=?
        ORDER BY id DESC
    """, (username,))
    rows = cur.fetchall()
    db.close()

    posts = []
    for r in rows:
        posts.append({
            "id": r[0],
            "username": r[1],
            "maker": r[2],
            "car": r[3],
            "region": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": r[7],
            "likes": r[8],
        })
    return posts

def get_following_posts(user):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT p.id, p.username, p.maker, p.car, p.region, p.comment, p.image, p.created_at, p.likes
        FROM posts p
        JOIN follows f ON p.username = f.following
        WHERE f.follower = ?
        ORDER BY p.id DESC
    """, (user,))
    rows = cur.fetchall()
    db.close()

    posts = []
    for r in rows:
        posts.append({
            "id": r[0],
            "username": r[1],
            "maker": r[2],
            "car": r[3],
            "region": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": r[7],
            "likes": r[8],
        })
    return posts

def get_ranking_posts(period):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, username, maker, car, region, comment, image, created_at, likes
        FROM posts
        ORDER BY likes DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    db.close()

    posts = []
    for r in rows:
        posts.append({
            "id": r[0],
            "username": r[1],
            "maker": r[2],
            "car": r[3],
            "region": r[4],
            "comment": r[5],
            "image": r[6],
            "created_at": r[7],
            "likes": r[8],
        })
    return posts

def get_liked_posts(user):
    if not user:
        return []
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT post_id FROM likes WHERE username=?", (user,))
    rows = cur.fetchall()
    db.close()
    return [r[0] for r in rows]

# --------------------
# ページ
# --------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = get_login_user(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "posts": get_posts(),
            "liked_posts": get_liked_posts(user),
            "mode": "home"
        }
    )

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, maker: str = "", car: str = "", region: str = ""):
    user = get_login_user(request)
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "user": user,
            "posts": search_posts(maker, car, region),
            "maker": maker,
            "car": car,
            "region": region,
            "mode": "search"
        }
    )

@app.get("/following", response_class=HTMLResponse)
def following(request: Request):
    user = get_login_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        "follow_list.html",
        {
            "request": request,
            "user": user,
            "posts": get_following_posts(user),
            "mode": "following"
        }
    )

@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, period: str = "day"):
    user = get_login_user(request)
    return templates.TemplateResponse(
        "ranking.html",
        {
            "request": request,
            "user": user,
            "posts": get_ranking_posts(period),
            "period": period,
            "ranking_title": "ランキング",
            "mode": "ranking"
        }
    )

@app.get("/user/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str):
    user = get_login_user(request)
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "username": username,
            "posts": get_user_posts(username),
            "mode": "profile"
        }
    )

# --------------------
# 認証
# --------------------
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
    row = cur.fetchone()
    db.close()

    if row:
        res = RedirectResponse("/", status_code=303)
        res.set_cookie("user", username)
        return res
    return RedirectResponse("/", status_code=303)

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res

# --------------------
# 投稿
# --------------------
@app.post("/post")
def post(
    request: Request,
    maker: str = Form(""),
    car: str = Form(""),
    region: str = Form(""),
    comment: str = Form(""),
    image: UploadFile = File(None)
):
    user = get_login_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)

    image_path = None
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1]
        filename = f"{uuid.uuid4()}{ext}"
        image_path = f"/uploads/{filename}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO posts (username, maker, car, region, comment, image, created_at, likes)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (user, maker, car, region, comment, image_path, datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    db.close()

    return RedirectResponse("/", status_code=303)
