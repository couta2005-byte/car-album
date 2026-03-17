import psycopg2
import csv
import os

DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

# ======================
# makers（category対応）
# ======================
def import_makers():
    conn = get_conn()
    cur = conn.cursor()

    with open("data/makers.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            cur.execute("""
                INSERT INTO makers (id, name, category)
                VALUES (%s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category
            """, (
                int(row["id"]),
                row["name"],
                row["category"]
            ))

    conn.commit()
    conn.close()
    print("makers投入OK")

# ======================
# car_models（ここ重要）
# ======================
def import_cars():
    conn = get_conn()
    cur = conn.cursor()

    with open("data/cars.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            cur.execute("""
                INSERT INTO car_models (id, maker_id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET
                    maker_id = EXCLUDED.maker_id,
                    name = EXCLUDED.name
            """, (
                int(row["id"]),
                int(row["maker_id"]),
                row["name"]
            ))

    conn.commit()
    conn.close()
    print("car_models投入OK")

# ======================
# 実行
# ======================
if __name__ == "__main__":
    print("=== START IMPORT ===")

    import_makers()
    import_cars()

    print("🔥 完了（makers + car_models）")