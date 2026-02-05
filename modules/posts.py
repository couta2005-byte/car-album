# modules/posts.py
from fastapi import APIRouter, Request, Form, UploadFile, File, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from modules.core import *

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)

        cur = db.cursor()
        cur.execute("""
            SELECT p.id, u.display_name, u.handle, p.comment, p.image, p.created_at
            FROM posts p
            JOIN users u ON p.user_id = u.id
            ORDER BY p.id DESC
        """)
        posts = cur.fetchall()
        cur.close()
    finally:
        db.close()

    return request.app.state.templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "posts": posts,
            "user": me_username,
            "me_user_id": me_user_id,
            "me_handle": me_handle,
            "mode": "home"
        }
    )
