# modules/dm.py
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from modules.core import *
import uuid

router = APIRouter(prefix="/dm")

@router.post("/start/{target_user_id}")
def dm_start(target_user_id: str, user: str = Cookie(None), uid: str = Cookie(None)):
    db = get_db()
    try:
        me_username, me_user_id = get_me_from_cookies(db, user, uid)
        if not me_user_id or me_user_id == target_user_id:
            return RedirectResponse("/", status_code=303)

        cur = db.cursor()
        cur.execute("""
            SELECT id FROM dm_rooms
            WHERE (user1_id=%s AND user2_id=%s)
               OR (user1_id=%s AND user2_id=%s)
        """, (me_user_id, target_user_id, target_user_id, me_user_id))
        r = cur.fetchone()
        if r:
            room_id = r[0]
        else:
            room_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO dm_rooms (id, user1_id, user2_id, created_at)
                VALUES (%s,%s,%s,%s)
            """, (room_id, me_user_id, target_user_id, utcnow_naive()))
            db.commit()
        cur.close()
    finally:
        db.close()

    return RedirectResponse(f"/dm/{room_id}", status_code=303)
