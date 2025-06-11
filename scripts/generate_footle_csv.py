# scripts/generate_footle_csv.py

import csv
from transliterate import translit
from pathlib import Path

# 1) Точный список из 100 уникальных фамилий
players_en = [
    "messi","ronaldo","neymar","mbappe","lewandowski",
    "salah","kane","hazard","modric","debruyne",
    "suarez","benzema","pogba","griezmann","mane",
    "coutinho","ramos","rodriguez","iniesta","xavi",
    "busquets","terstegen","alisson","neuer","buffon",
    "vidal","kroos","milinkovic","lloris","eriksen",
    "gomez","bale","rooney","beckham","maradona",
    "pele","zidane","puyol","beckenbauer","ronaldinho",
    "ibrahimovic","baggio","vieira","xhaka","ozil",
    "foden","sterling","dybala","hummels","rakitic",
    "mandzukic","nainggolan","higuain","cavani","muller",
    "alli","cantona","gerrard","lampard","scholes",
    "verratti","terry","pique","hamsik","milner",
    "totti","pirlo","gattuso","seedorf","henry",
    "aguero","werner","reus","kimmich","navas",
    "silva","ramsey","asensio","depay","fabregas",
    "kompany","vidic","lahm","alves","essien",
    "zanetti","cafu","puskas","klinsmann","yashin",
    "schweinsteiger","carragher","gullit","neville","cannavaro",
    "rivaldo","trezeguet","valderrama","hagi","maicon"
]

# Проверяем, что действительно ровно 100 уникальных
players_en = sorted(set(players_en))
assert len(players_en) == 100, f"Ожидалось 100, а получилось {len(players_en)}"

def rusify(name: str) -> str:
    # pip install transliterate
    return translit(name, 'ru').lower()

# 2) Пути
SCRIPT_DIR = Path(__file__).resolve().parent    # .../scripts
PROJECT_ROOT = SCRIPT_DIR.parent               # корень проекта
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "footle_list.csv"

# 3) Создаём папку data/, если её нет
DATA_DIR.mkdir(exist_ok=True)

# 4) Пишем CSV
with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["en", "ru"])
    for en in players_en:
        writer.writerow([en, rusify(en)])

print(f"✅ Сгенерировано {len(players_en)} записей в {CSV_PATH}")
