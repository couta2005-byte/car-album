# modules/dm.py
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import RedirectResponse, HTMLResponse
from typing import List, Dict, Any
from datetime import timedelta
import uuid

from modules.core import (
    get_db,
    run_db,
    get_me_from_cookies,
    get_me_handle,
    utcnow_naive,
)

router = APIRouter(prefix="/dm")


# ======================
# helpers
# ======================
def redirect_back(request: Request, fallback: str = "/"):
    referer = request.headers.get("referer")
    return RedirectResponse(referer or fallback, status_code=303)


def get_or_create_dm_room_id(db, me_user_id: str, other_user_id: str) -> str:
    u1, u2 = sorted([me_user_id, other_user_id])
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id FROM dm_rooms WHERE user1_id=%s AND user2_id=%s",
            (u1, u2),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

        rid = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO dm_rooms (id, user1_id, user2_id, created_at)
            VALUES (%s,%s,%s,%s)
            """,
            (rid, u1, u2, utcnow_naive()),
        )
        return rid
    finally:
        cur.close()


# ======================
# DM list
# ======================
@router.get("", response_class=HTMLResponse)
def dm_list(request: Request, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        cur = db.cursor()
        cur.execute(
            """
            SELECT
                r.id,
                CASE
                    WHEN r.user1_id = %s THEN r.user2_id
                    ELSE r.user1_id
                END AS other_user_id
            FROM dm_rooms r
            WHERE r.user1_id=%s OR r.user2_id=%s
            ORDER BY r.created_at DESC
            """,
            (me_user_id, me_user_id, me_user_id),
        )
        rooms = cur.fetchall()

        room_list: List[Dict[str, Any]] = []
        for rid, other_uid in rooms:
            cur.execute(
                """
                SELECT u.username, u.display_name, u.handle, p.icon
                FROM users u
                LEFT JOIN profiles p ON p.user_id = u.id
                WHERE u.id=%s
                """,
                (other_uid,),
            )
            u = cur.fetchone()
            if not u:
                continue

            room_list.append({
                "room_id": str(rid),
                "username": u[0],
                "display_name": u[1] or u[0],
                "handle": u[2],
                "icon": u[3],
                "profile_key": u[2] if u[2] else u[0],
            })
        cur.close()
    finally:
        db.close()

    return request.app.state.templates.TemplateResponse("dm_list.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "rooms": room_list,
        "mode": "dm",
    })


# ======================
# DM room
# ======================
@router.get("/{room_id}", response_class=HTMLResponse)
def dm_room(request: Request, room_id: str, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        me_handle = get_me_handle(db, me_user_id)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        cur = db.cursor()
        cur.execute(
            "SELECT user1_id, user2_id FROM dm_rooms WHERE id=%s",
            (room_id,),
        )
        r = cur.fetchone()
        if not r or me_user_id not in (str(r[0]), str(r[1])):
            cur.close()
            return RedirectResponse("/", status_code=303)

        other_user_id = str(r[1] if str(r[0]) == me_user_id else r[0])

        cur.execute(
            """
            SELECT u.username, u.display_name, u.handle, p.icon
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE u.id=%s
            """,
            (other_user_id,),
        )
        other = cur.fetchone()

        cur.execute(
            """
            SELECT sender_id, body, created_at
            FROM dm_messages
            WHERE room_id=%s
            ORDER BY created_at ASC
            """,
            (room_id,),
        )
        rows = cur.fetchall()
        cur.close()

        messages = []
        for sender_id, body, created_at in rows:
            messages.append({
                "is_me": str(sender_id) == me_user_id,
                "body": body,
                "created_at": (created_at + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M"),
            })
    finally:
        db.close()

    return request.app.state.templates.TemplateResponse("dm_room.html", {
        "request": request,
        "user": me_username,
        "me_user_id": me_user_id,
        "me_handle": me_handle,
        "room_id": room_id,
        "other": {
            "username": other[0],
            "display_name": other[1] or other[0],
            "handle": other[2],
            "icon": other[3],
            "profile_key": other[2] if other[2] else other[0],
        },
        "messages": messages,
        "mode": "dm",
    })


# ======================
# DM send
# ======================
@router.post("/{room_id}/send")
def dm_send(request: Request, room_id: str, body: str = Form(""), user: str = Cookie(None), uid: str = Cookie(None)):
    body = (body or "").strip()
    if not body:
        return redirect_back(request, f"/dm/{room_id}")

    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id:
            return RedirectResponse("/login", status_code=303)
    finally:
        db.close()

    def _do(db, cur):
        cur.execute(
            """
            INSERT INTO dm_messages (id, room_id, sender_id, body, created_at)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (str(uuid.uuid4()), room_id, me_user_id, body, utcnow_naive()),
        )

    run_db(_do)
    return RedirectResponse(f"/dm/{room_id}", status_code=303)


# ======================
# DM start（GET）
# ======================
@router.get("/start/{target_user_id}")
def dm_start(target_user_id: str, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)

        if not me_user_id:
            return RedirectResponse("/login", status_code=303)

        if me_user_id == target_user_id:
            return RedirectResponse("/", status_code=303)

        room_id = get_or_create_dm_room_id(db, me_user_id, target_user_id)
    finally:
        db.close()

    return RedirectResponse(f"/dm/{room_id}", status_code=303)
