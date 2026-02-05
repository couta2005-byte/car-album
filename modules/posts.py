# modules/posts.py
from fastapi import APIRouter, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse

from typing import Optional, List, Dict, Any
from urllib.parse import quote, unquote
from datetime import datetime, timedelta

import cloudinary.uploader

from modules.core import (
    get_db,
    run_db,
    utcnow_naive,
    fmt_jst,
    get_me_from_cookies,
    get_me_handle,
)

router = APIRouter()


# ======================
# likes helper
# ======================
def get_liked_posts(db, me_user_id: Optional[str], me_username: Optional[str]) -> set:
    if not me_user_id and not me_username:
        return set()
    cur = db.cursor()
    try:
        if me_user_id:
            cur.execute("SELECT post_id FROM likes WHERE user_id=%s", (me_user_id,))
        else:
            cur.execute("SELECT post_id FROM likes WHERE username=%s", (me_username,))
        return {r[0] for r in cur.fetchall()}
    finally:
        cur.close()


# ======================
# comments fetch
# ======================
def fetch_comments_for_posts(
    db, post_ids: List[int], me_user_id: Optional[str]
) -> Dict[int, List[Dict[str, Any]]]:
    if not post_ids:
        return {}

    placeholders = ",".join(["%s"] * len(post_ids))
    cur = db.cursor()

    try:
        if me_user_id:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    u.username,
                    COALESCE(u.display_name, u.username),
                    u.handle,
                    u.id,
                    c.comment,
                    c.created_at,
                    p.icon,
                    COUNT(cl.comment_id),
                    CASE WHEN mycl.user_id IS NULL THEN 0 ELSE 1 END
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles p ON p.user_id = u.id
                LEFT JOIN comment_likes cl ON cl.comment_id = c.id
                LEFT JOIN comment_likes mycl
                  ON mycl.comment_id = c.id AND mycl.user_id = %s
                WHERE c.post_id IN ({placeholders})
                GROUP BY c.post_id, c.id, u.username, u.display_name, u.handle, u.id, c.comment, c.created_at, p.icon, mycl.user_id
                ORDER BY c.id ASC
            """
            cur.execute(sql, (me_user_id, *post_ids))
        else:
            sql = f"""
                SELECT
                    c.post_id,
                    c.id,
                    u.username,
                    COALESCE(u.display_name, u.username),
                    u.handle,
                    u.id,
                    c.comment,
                    c.created_at,
                    p.icon,
                    COUNT(cl.comment_id),
                    0
                FROM comments c
                JOIN users u ON c.user_id = u.id
                LEFT JOIN profiles p ON p.user_id = u.id
                LEFT JOIN comment_likes cl ON cl.comment_id = c.id
                WHERE c.post_id IN ({placeholders})
                GROUP BY c.post_id, c.id, u.username, u.display_name, u.handle, u.id, c.comment, c.created_at, p.icon
                ORDER BY c.id ASC
            """
            cur.execute(sql, tuple(post_ids))

        rows = cur.fetchall()
    finally:
        cur.close()

    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        (
            post_id, cid, username, display_name,
            handle, user_id, comment, created_at,
            icon, likes, liked
        ) = r

        key = handle if handle else username

        out.setdefault(post_id, []).append({
            "id": cid,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "profile_key": key,
            "user_id": str(user_id),
            "comment": comment,
            "created_at": fmt_jst(created_at),
            "user_icon": icon,
            "likes": likes,
            "liked": bool(liked),
        })

    return out


# ======================
# posts fetch
# ======================
def fetch_posts(
    db,
    me_user_id: Optional[str],
    where_sql: str = "",
    params=(),
    order_sql: str = "ORDER BY p.id DESC",
    limit_sql: str = "",
):
    cur = db.cursor()
    try:
        cur.execute(f"""
            SELECT
                p.id,
                u.username,
                COALESCE(u.display_name, u.username),
                u.handle,
                u.id,
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                COUNT(l.post_id),
                pr.icon
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN likes l ON l.post_id = p.id
            LEFT JOIN profiles pr ON pr.user_id = u.id
            {where_sql}
            GROUP BY
                p.id, u.username, u.display_name, u.handle, u.id,
                p.maker, p.region, p.car,
                p.comment, p.image, p.created_at,
                pr.icon
            {order_sql}
            {limit_sql}
        """, params)
        rows = cur.fetchall()
    finally:
        cur.close()

    post_ids = [r[0] for r in rows]
    comments_map = fetch_comments_for_posts(db, post_ids, me_user_id)

    posts = []
    for r in rows:
        (
            pid, username, display_name, handle,
            user_id, maker, region, car,
            comment, image, created_at,
            likes, icon
        ) = r

        key = handle if handle else username

        comments = comments_map.get(pid, [])
        posts.append({
            "id": pid,
            "username": username,
            "display_name": display_name,
            "handle": handle,
            "profile_key": key,
            "user_id": str(user_id),
            "maker": maker,
            "region": region,
            "car": car,
            "comment": comment,
            "image": image,
            "created_at": fmt_jst(created_at),
            "likes": likes,
            "user_icon": icon,
            "comments": comments,
            "comment_count": len(comments),
        })

    return posts


# ======================
# top
# ======================
@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        posts = fetch_posts(db, me_user_id)
        liked_posts = get_liked_posts(db, me_user_id, me_username)
    finally:
        db.close()

    return request.app.state.templates.TemplateResponse("index.html", {
        "request": request,
        "posts": posts,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "liked_posts": liked_posts,
        "mode": "home",
        "ranking_title": "",
        "period": "",
    })


# ======================
# post create
# ======================
@router.post("/post")
def create_post(
    request: Request,
    maker: str = Form(""),
    region: str = Form(""),
    car: str = Form(""),
    comment: str = Form(""),
    image: UploadFile = File(None),
    user: str = Cookie(None),
    uid: str = Cookie(None),
):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    image_url = None
    if image and image.filename:
        result = cloudinary.uploader.upload(
            image.file,
            folder="carbum/posts",
            transformation=[{"width": 1400, "crop": "limit"}],
        )
        image_url = result["secure_url"]

    def _do(db, cur):
        cur.execute("""
            INSERT INTO posts
              (username, user_id, maker, region, car, comment, image, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            me_username,
            me_user_id,
            maker,
            region,
            car,
            comment,
            image_url,
            utcnow_naive(),
        ))

    run_db(_do)
    return RedirectResponse("/", status_code=303)


# ======================
# like API
# ======================
@router.post("/api/like/{post_id}")
def api_like(post_id: int, request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return JSONResponse({"ok": False}, status_code=401)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("SELECT 1 FROM likes WHERE user_id=%s AND post_id=%s", (me_user_id, post_id))
        liked = cur.fetchone() is not None

        if liked:
            cur.execute("DELETE FROM likes WHERE user_id=%s AND post_id=%s", (me_user_id, post_id))
        else:
            cur.execute(
                "INSERT INTO likes (username, user_id, post_id) VALUES (%s,%s,%s)",
                (me_username, me_user_id, post_id),
            )

        cur.execute("SELECT COUNT(*) FROM likes WHERE post_id=%s", (post_id,))
        count = cur.fetchone()[0]
        return {"ok": True, "liked": not liked, "likes": count}

    return JSONResponse(run_db(_do))
