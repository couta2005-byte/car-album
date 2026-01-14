import sqlite3
import os

DB_PATH = "app.db"

if not os.path.exists(DB_PATH):
    print("❌ app.db が見つかりません")
    exit()

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# profiles テーブルの現在の構造を確認
cur.execute("PRAGMA table_info(profiles)")
columns = [row[1] for row in cur.fetchall()]

print("現在のカラム:", columns)

# region が無ければ追加
if "region" not in columns:
    print("➕ region カラムを追加します")
    cur.execute("ALTER TABLE profiles ADD COLUMN region TEXT")
else:
    print("✅ region カラムは既に存在します")

conn.commit()
conn.close()

print("🎉 DB修正完了")
