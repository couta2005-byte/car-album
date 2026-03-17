import requests
from bs4 import BeautifulSoup
import csv
import re
import time
from urllib.parse import quote

BASE_URL = "https://ja.wikipedia.org/wiki/"

# maker_id: (Wikipediaページ名, 表示名, category)
makers = {
    # 日本車
    1: ("トヨタ自動車の車種一覧", "トヨタ", "japan_car"),
    2: ("日産自動車の車種一覧", "日産", "japan_car"),
    3: ("本田技研工業の車種一覧", "ホンダ", "japan_car"),
    4: ("マツダの車種一覧", "マツダ", "japan_car"),
    5: ("SUBARUの車種一覧", "スバル", "japan_car"),
    6: ("三菱自動車工業の車種一覧", "三菱", "japan_car"),
    7: ("スズキの車種一覧", "スズキ", "japan_car"),
    8: ("ダイハツ工業の車種一覧", "ダイハツ", "japan_car"),
    9: ("レクサスの車種一覧", "レクサス", "japan_car"),

    # 外車
    20: ("BMWの車種一覧", "BMW", "foreign_car"),
    21: ("メルセデス・ベンツの車種一覧", "メルセデス・ベンツ", "foreign_car"),
    22: ("アウディの車種一覧", "アウディ", "foreign_car"),
    23: ("フォルクスワーゲンの車種一覧", "フォルクスワーゲン", "foreign_car"),
    24: ("ポルシェの車種一覧", "ポルシェ", "foreign_car"),
    25: ("フェラーリの車種一覧", "フェラーリ", "foreign_car"),
    26: ("ランボルギーニの車種一覧", "ランボルギーニ", "foreign_car"),
    27: ("マセラティの車種一覧", "マセラティ", "foreign_car"),
    28: ("アルファロメオの車種一覧", "アルファロメオ", "foreign_car"),
    29: ("フィアットの車種一覧", "フィアット", "foreign_car"),
    30: ("アバルトの車種一覧", "アバルト", "foreign_car"),
    31: ("MINIの車種一覧", "MINI", "foreign_car"),
    32: ("ジープの車種一覧", "ジープ", "foreign_car"),
    33: ("キャデラックの車種一覧", "キャデラック", "foreign_car"),
    34: ("シボレーの車種一覧", "シボレー", "foreign_car"),
    35: ("フォードの車種一覧", "フォード", "foreign_car"),
    36: ("テスラの車種一覧", "テスラ", "foreign_car"),

    # バイク
    100: ("ホンダのオートバイ一覧", "ホンダ", "bike"),
    101: ("ヤマハ発動機の製品一覧", "ヤマハ", "bike"),
    102: ("カワサキのオートバイ一覧", "カワサキ", "bike"),
    103: ("スズキのオートバイ一覧", "スズキ", "bike"),
    104: ("BMWのオートバイ", "BMW Motorrad", "bike"),
    105: ("ドゥカティ", "ドゥカティ", "bike"),
    106: ("トライアンフ・モーターサイクルズ", "トライアンフ", "bike"),
    107: ("KTM", "KTM", "bike"),
    108: ("アプリリア", "アプリリア", "bike"),
    109: ("ハーレーダビッドソン", "ハーレーダビッドソン", "bike"),
}

NG_WORDS = [
    "一覧", "歴代", "コンセプト", "試作", "車両", "二輪",
    "自動車", "メーカー", "関連", "脚注", "外部リンク",
    "テンプレート", "カテゴリ", "編集", "出典",
    "プロジェクト", "Portal", "モータースポーツ", "グループ",
    "会社", "企業", "ブランド", "生産終了", "現行車種",
]

def clean_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"（.*?）", "", text)
    text = re.sub(r"\(.*?\)", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = text.replace("\n", "").replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text

def is_valid_name(text: str) -> bool:
    if not text:
        return False

    if not (2 <= len(text) <= 30):
        return False

    if any(word in text for word in NG_WORDS):
        return False

    if re.fullmatch(r"[0-9\-./ ]+", text):
        return False

    if not re.search(r"[ぁ-んァ-ン一-龯A-Za-z0-9]", text):
        return False

    # 明らかに説明文っぽいものを除外
    if "。" in text or "、" in text or ":" in text or "：" in text:
        return False

    return True

def fetch_page(page_title: str) -> str:
    url = BASE_URL + quote(page_title)
    print(f"取得中: {page_title}")

    res = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    res.raise_for_status()
    res.encoding = res.apparent_encoding
    return res.text

def fetch_names(page_title: str) -> list[str]:
    try:
        html = fetch_page(page_title)
    except Exception as e:
        print(f"取得失敗: {page_title} / {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    names = set()

    # li から拾う
    for li in soup.select("li"):
        text = clean_text(li.get_text(" ", strip=True))
        if is_valid_name(text):
            names.add(text)

    # tableのリンク文字も拾う
    for a in soup.select("table a, .wikitable a, .navbox a"):
        text = clean_text(a.get_text(" ", strip=True))
        if is_valid_name(text):
            names.add(text)

    return sorted(names)

def write_makers_csv(path: str = "data/makers.csv") -> None:
    rows = []
    for maker_id, (_, name, category) in makers.items():
        rows.append([maker_id, name, category])

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "category"])
        writer.writerows(rows)

def write_cars_csv(path: str = "data/cars.csv") -> None:
    rows = []
    car_id = 1

    for maker_id, (page_title, maker_name, category) in makers.items():
        names = fetch_names(page_title)
        print(f"{maker_name} ({category}) : {len(names)}件")

        for name in names:
            rows.append([car_id, maker_id, name])
            car_id += 1

        time.sleep(1)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "maker_id", "name"])
        writer.writerows(rows)

def main():
    write_makers_csv()
    write_cars_csv()
    print("makers.csv / cars.csv 生成完了")

if __name__ == "__main__":
    main()