from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from database import get_db
from models import Post

router = APIRouter()

@router.get("/ranking")
def get_ranking(
    period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
    db: Session = Depends(get_db)
):
    now = datetime.now()

    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)

    posts = (
        db.query(Post)
        .filter(Post.created_at >= start)
        .order_by(Post.likes.desc(), Post.id.desc())
        .limit(5)
        .all()
    )

    return [
        {
            "username": p.username,
            "car_name": p.car_name,
            "likes": p.likes
        }
        for p in posts
    ]
