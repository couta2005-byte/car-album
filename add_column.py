import sqlite3

conn = sqlite3.connect("app.db")
cur = conn.cursor()

cur.execute("ALTER TABLE posts ADD COLUMN image_path TEXT")

conn.commit()
conn.close()

print("image_path カラムを追加しました")
