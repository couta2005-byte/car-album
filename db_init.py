import sqlite3
import time

conn = sqlite3.connect("app.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    car TEXT NOT NULL,
    comment TEXT NOT NULL,
    image TEXT,
    likes INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
)
""")

conn.commit()
conn.close()

print("DB初期化完了: app.db")
