from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3, os, uuid
from datetime import datetime

# ====================
# App
# ====================
app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")

DB = "app.db"

# ====================
# DB
# ====================
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# ====================
# Login User
# ====================
def get_login_user(request: Request):
    return request.cookies.get("user")

# ====================
# Posts
# ====================
def row_to_post(r):
    return {
        "id": r["id"],
        "username": r["username"],
        "maker": r["maker"],
        "car": r["car"],
        "region": r["region"],
        "comment": r["comment"],
        "image": r["image"],
        "created_at": r["created_at"],
        "likes": r["likes"],
    }

def get_posts():
    db = get_db()
    rows = db.execute("""
        SELECT * FROM posts
        ORDER BY id DESC
    """).fetchall()
    db.close()
    return [row_to_post(r) for r in rows]

def search_posts(maker, car, region):
    db = get_db()
    query = "SELECT * FROM posts WHERE 1=1"
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
    rows = db.execute(query, params).fetchall()
    db.close()
    return [row_to_post(r) for r in rows]

def get_user_posts(username):
    db = get_db()
    rows = db.execute("""
        SELECT * FROM posts
        WHERE username=?
        ORDER BY id DESC
    """, (username,)).fetchall()
    db.close()
    return [row_to_post(r) for r in rows]

def get_following_posts(user):
    db = get_db()
    rows = db.execute("""
        SELECT p.*
        FROM posts p
        JOIN follows f ON p.username = f.following
        WHERE f.follower = ?
        ORDER BY p.id DESC
    """, (user,)).fetchall()
    db.close()
    return [row_to_post(r) for r in rows]

def get_ranking_posts():
    db = get_db()
    rows = db.execute("""
        SELECT * FROM posts
        ORDER BY likes DESC, id DESC
        LIMIT 10
    """).fetchall()
    db.close()
    return [row_to_post(r) for r in rows]

def get_liked_posts(user):
    if not user:
        return []
    db = get_db()
    rows = db.execute(
        "SELECT post_id FROM likes WHERE username=?",
        (user,)
    ).fetchall()
    db.close()
    return [r["post_id"] for r in rows]

# ====================
# Pages
# ====================
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
def ranking(request: Request):
    user = get_login_user(request)
    return templates.TemplateResponse(
        "ranking.html",
        {
            "request": request,
            "user": user,
            "posts": get_ranking_posts(),
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

# ====================
# Auth
# ====================
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    db.close()

    res = RedirectResponse("/", status_code=303)
    if row:
        res.set_cookie("user", username)
    return res

@app.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    return res

# ====================
# Post
# ====================
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
        os.makedirs("uploads", exist_ok=True)
        ext = os.path.splitext(image.filename)[1]
        filename = f"{uuid.uuid4()}{ext}"
        image_path = f"/uploads/{filename}"
        with open(f"uploads/{filename}", "wb") as f:
            f.write(image.file.read())

    db = get_db()
    db.execute("""
        INSERT INTO posts (username, maker, car, region, comment, image, created_at, likes)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        user,
        maker,
        car,
        region,
        comment,
        image_path,
        datetime.now().strftime("%Y-%m-%d %H:%M")
    ))
    db.commit()
    db.close()

    return RedirectResponse("/", status_code=303)
