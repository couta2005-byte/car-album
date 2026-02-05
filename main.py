# main.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os

from modules.core import templates
from modules import posts, users, dm

app = FastAPI()

# static
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ★★★★★ これが無いと100%落ちる ★★★★★
app.state.templates = templates

# routers
app.include_router(posts.router)
app.include_router(users.router)
app.include_router(dm.router)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
