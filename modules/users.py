# modules/users.py
from fastapi import APIRouter, Request, Form, UploadFile, File, Cookie, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from typing import Optional, List, Dict, Any
from urllib.parse import unquote
import uuid

import cloudinary.uploader

from modules.core import (
    get_db,
    run_db,
    pwd_context,
    normalize_login_id,
    get_me_from_cookies,
    get_me_handle,
    utcnow_naive,
)

router = APIRouter()


# ======================
# helpers
# ======================
def redirect_back(request: Request, fallback: str = "/"):
    next_url = request.query_params.get("next")
    if next_url and next_url.startswith("/"):
        return RedirectResponse(next_url, status_code=303)
    referer = request.headers.get("referer")
    return RedirectResponse(referer or fallback, status_code=303)


def resolve_user_by_key(db, key: str):
    cur = db.cursor()
    try:
        cur.execute("SELECT id, username, display_name, handle FROM users WHERE handle=%s", (key,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute("SELECT id, username, display_name, handle FROM users WHERE username=%s", (key,))
        return cur.fetchone()
    finally:
        cur.close()


# ======================
# auth pages
# ======================
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
    finally:
        db.close()

    error = request.query_params.get("error", "")
    return request.app.state.templates.TemplateResponse("login.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "error": error,
    })


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
    finally:
        db.close()

    error = request.query_params.get("error", "")
    return request.app.state.templates.TemplateResponse("register.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "error": error,
    })


# ======================
# auth actions
# ======================
@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    username = (username or "").strip()
    password = password or ""

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT password, id FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close()

        if not row or not pwd_context.verify(password, row[0]):
            return RedirectResponse("/login?error=invalid", status_code=303)

        uid = str(row[1])
    finally:
        db.close()

    res = RedirectResponse("/", status_code=303)
    res.set_cookie("user", username, httponly=True, samesite="lax")
    res.set_cookie("uid", uid, httponly=True, samesite="lax")
    return res


@router.post("/register")
def register(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    username = (username or "").strip()
    password = password or ""

    if not username or not password:
        return RedirectResponse("/register?error=empty", status_code=303)

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
        if cur.fetchone():
            cur.close()
            return RedirectResponse("/register?error=exists", status_code=303)

        cur.execute("""
            INSERT INTO users (username, password, display_name, created_at, id)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            username,
            pwd_context.hash(password),
            username,
            utcnow_naive(),
            uuid.uuid4(),
        ))
        cur.close()
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/login", status_code=303)


@router.post("/logout")
def logout():
    res = RedirectResponse("/", status_code=303)
    res.delete_cookie("user")
    res.delete_cookie("uid")
    return res


# ======================
# profile
# ======================
@router.get("/user/{key}", response_class=HTMLResponse)
def profile(
    request: Request,
    key: str,
    user: str = Cookie(None),
    uid: str = Cookie(None),
):
    key = unquote(key)

    db = get_db()
    cur = db.cursor()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)

        urow = resolve_user_by_key(db, key)
        if not urow:
            return RedirectResponse("/", status_code=303)

        target_user_id, username, display_name, handle = urow

        cur.execute("SELECT maker, car, region, bio, icon FROM profiles WHERE user_id=%s", (target_user_id,))
        profile = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM follows WHERE follower_id=%s", (target_user_id,))
        follow_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM follows WHERE followee_id=%s", (target_user_id,))
        follower_count = cur.fetchone()[0]

        is_following = False
        if me_user_id and me_user_id != str(target_user_id):
            cur.execute(
                "SELECT 1 FROM follows WHERE follower_id=%s AND followee_id=%s",
                (me_user_id, target_user_id),
            )
            is_following = cur.fetchone() is not None
    finally:
        cur.close()
        db.close()

    return request.app.state.templates.TemplateResponse("profile.html", {
        "request": request,
        "username": username,
        "display_name": display_name,
        "handle": handle,
        "profile": profile,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "target_user_id": str(target_user_id),
        "is_following": is_following,
        "follow_count": follow_count,
        "follower_count": follower_count,
        "mode": "profile",
    })


# ======================
# profile edit
# ======================
@router.post("/profile/edit")
def profile_edit(
    request: Request,
    display_name: str = Form(""),
    handle: str = Form(""),
    maker: str = Form(""),
    car: str = Form(""),
    region: str = Form(""),
    bio: str = Form(""),
    icon: UploadFile = File(None),
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

    display_name = (display_name or "").strip()
    handle = normalize_login_id(handle)

    icon_url = None
    if icon and icon.filename:
        result = cloudinary.uploader.upload(
            icon.file,
            folder="carbum/icons",
            transformation=[{"width": 400, "crop": "limit"}],
        )
        icon_url = result["secure_url"]

    def _do(db, cur):
        cur.execute("""
            UPDATE users
            SET display_name=%s, handle=%s
            WHERE id=%s
        """, (display_name or me_username, handle, me_user_id))

        cur.execute("""
            INSERT INTO profiles (username, user_id, maker, car, region, bio, icon)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (username)
            DO UPDATE SET
                maker=EXCLUDED.maker,
                car=EXCLUDED.car,
                region=EXCLUDED.region,
                bio=EXCLUDED.bio,
                icon=COALESCE(EXCLUDED.icon, profiles.icon)
        """, (
            me_username,
            me_user_id,
            maker,
            car,
            region,
            bio,
            icon_url,
        ))

    run_db(_do)
    return RedirectResponse(f"/user/{handle or me_username}", status_code=303)


# ======================
# follow / unfollow
# ======================
@router.post("/follow/{key}")
def follow(key: str, user: str = Cookie(None), uid: str = Cookie(None)):
    key = unquote(key)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        urow = resolve_user_by_key(db, key)
        if not urow:
            return RedirectResponse("/", status_code=303)

        target_user_id = str(urow[0])
        if target_user_id == me_user_id:
            return RedirectResponse("/", status_code=303)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute("""
            INSERT INTO follows (follower_id, followee_id)
            VALUES (%s,%s)
            ON CONFLICT DO NOTHING
        """, (me_user_id, target_user_id))

    run_db(_do)
    return RedirectResponse(f"/user/{key}", status_code=303)


@router.post("/unfollow/{key}")
def unfollow(key: str, user: str = Cookie(None), uid: str = Cookie(None)):
    key = unquote(key)

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        urow = resolve_user_by_key(db, key)
        if not urow:
            return RedirectResponse("/", status_code=303)

        target_user_id = str(urow[0])
    finally:
        db.close()

    def _do(db, cur):
        cur.execute(
            "DELETE FROM follows WHERE follower_id=%s AND followee_id=%s",
            (me_user_id, target_user_id),
        )

    run_db(_do)
    return RedirectResponse(f"/user/{key}", status_code=303)
