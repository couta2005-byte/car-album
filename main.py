from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from modules.core import init_app
from modules.posts import router as posts_router
from modules.users import router as users_router
from modules.dm import router as dm_router

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")

# ★★★ これを必ず追加 ★★★
app.state.templates = templates

# 初期化
init_app(app, templates)

# routers
app.include_router(posts_router)
app.include_router(users_router)
app.include_router(dm_router)
