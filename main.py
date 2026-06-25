from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import httpx
import os
import re
from datetime import datetime, date, timedelta, timezone

app = FastAPI(title="kintone自動入力システム")

KINTONE_DOMAIN = os.getenv("KINTONE_DOMAIN", "aichishiho.cybozu.com")
KINTONE_APP_ID = os.getenv("KINTONE_APP_ID", "57")
KINTONE_GUEST_ID = os.getenv("KINTONE_GUEST_ID", "4")
KINTONE_TOKEN = os.getenv("KINTONE_TOKEN", "")
CHATWORK_TOKEN = os.getenv("CHATWORK_TOKEN", "")
CHATWORK_ROOM_ID = os.getenv("CHATWORK_ROOM_ID", "236606241")
BOX_TOKEN = os.getenv("BOX_TOKEN", "")


# ── ユーティリティ ──────────────────────────────────────────

def seireki_to_wareki(year: int, month: int, day: int):
    d = date(year, month, day)
    for name, start in [
        ("令和", date(2019, 5, 1)),
        ("平成", date(1989, 1, 8)),
        ("昭和", date(1926, 12, 25)),
        ("大正", date(1912, 7, 30)),
    ]:
        if d >= start:
            return name, year - start.year + 1
    return "明治", year - 1868 + 1


def calc_age(birthdate_str: str) -> int:
    b = date.fromisoformat(birthdate_str)
    t = date.today()
    age = t.year - b.year
    if (t.month, t.day) < (b.month, b.day):
        age -= 1
    return age


def extract_address_parts(address: str):
    pref = re.match(r"^(.+?[都道府県])", address)
    pref = pref.group(1) if pref else ""
    rest = address[len(pref):]
    city = re.match(r"^(.+?[市区町村郡])", rest)
    city = city.group(1) if city else ""
    town = rest[len(city):]
    town = re.sub(r"[\d０-９]+番地.*$", "", town).strip()
    town = re.sub(r"[\d０-９]+丁目.*$", "", town).strip()
    return pref, city, town


def detect_product(text: str) -> Optional[str]:
    if "相続一式" in text:
        return "相続一式"
    if "遺産承継" in text:
        return "遺産承継"
    if "相続登記" in text:
        return "相続登記放棄"
    return None


def extract_junin_date(body: str, send_time: int) -> str:
    jst = timezone(timedelta(hours=9))
    send_date = datetime.fromtimestamp(send_time, tz=jst).date()
    if "昨日" in body:
        return (send_date - timedelta(days=1)).isoformat()
    return send_date.isoformat()


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ── kintone ────────────────────────────────────────────────

async def get_kintone_record(record_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/record.json",
            headers={"X-Cybozu-API-Token": KINTONE_TOKEN},
            params={"app": KINTONE_APP_ID, "id": record_id},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail=f"kintoneレコードが見つかりません: {r.text}")
    return r.json().get("record", {})


# ── BOX ────────────────────────────────────────────────────

async def get_box_text(file_id: str) -> str:
    if not BOX_TOKEN:
        return ""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.box.com/2.0/files/{file_id}",
            headers={
                "Authorization": f"Bearer {BOX_TOKEN}",
                "X-Rep-Hints": "[extracted_text]",
            },
            params={"fields": "representations"},
        )
        if r.status_code != 200:
            return ""
        entries = r.json().get("representations", {}).get("entries", [])
        text_rep = next((e for e in entries if e.get("representation") == "extracted_text"), None)
        if not text_rep:
            return ""
        url = text_rep.get("content", {}).get("url_template", "").replace("{+asset_path}", "")
        if not url:
            return ""
        r2 = await client.get(url, headers={"Authorization": f"Bearer {BOX_TOKEN}"})
        return r2.text if r2.status_code == 200 else ""


def extract_kana_from_box(text: str, name: str) -> Optional[str]:
    # 「小田　直史 (オダ　ナオフミ )」のようなパターン
    pattern = r'[（(]([ァ-ヶー\s　]+)[）)]'
    matches = re.findall(pattern, text)
    for m in matches:
        kana = m.strip()
        if len(kana) >= 3:
            hiragana = "".join(
                chr(ord(c) - 0x60) if "ァ" <= c <= "ン" else c
                for c in kana
            ).replace("　", "").replace(" ", "")
            return hiragana
    return None


def extract_address_from_box(text: str) -> dict:
    """BOX連絡票から住所・郵便番号を抽出"""
    result = {}

    # 「住所 (〒444-0948) 岡崎市西本郷町字和志山２４１番地１」パターン
    addr_match = re.search(r'住所\s*[（(〒]?\s*〒?([\d-]{7,8})[）)]?\s*(.+)', text)
    if addr_match:
        result["文字列__1行_"] = addr_match.group(1).replace("-", "-")  # 郵便番号
        addr_text = addr_match.group(2).strip()

        # 全角数字→半角に正規化
        addr_text = addr_text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

        # 都道府県が含まれる場合
        if re.match(r'.+?[都道府県]', addr_text):
            pref, city, town = extract_address_parts(addr_text)
            result["都道府県"] = pref
            result["市町村名"] = city
            result["町名"] = town
            result["住所"] = addr_text
        else:
            # 都道府県なし（市区町村から）
            city_match = re.match(r'^(.+?[市区町村郡])', addr_text)
            if city_match:
                city = city_match.group(1)
                rest = addr_text[len(city):]
                town = re.sub(r'字', '', rest)
                town = re.sub(r'[\d０-９]+番地.*$', '', town).strip()
                result["市町村名"] = city
                result["町名"] = town
                result["住所"] = addr_text  # 都道府県なしでも住所欄に入力

    return result


# ── Chatwork ───────────────────────────────────────────────

async def search_chatwork(name: str, box_url: str = "") -> dict:
    if not CHATWORK_TOKEN:
        return {}

    # 検索キー：テスト用プレフィックス除去 + 姓のみ
    clean_name = re.sub(r"^テスト用|様$", "", name).strip()
    surname = clean_name[:2]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages",
            headers={"X-ChatWorkToken": CHATWORK_TOKEN},
            params={"force": 1},
        )
    if r.status_code != 200:
        return {}

    messages = r.json()

    for msg in reversed(messages):
        body = msg.get("body", "")

        name_hit = clean_name in body or surname in body
        url_hit = box_url and box_url in body
        if not (name_hit or url_hit):
            continue

        is_junin = any(k in body for k in ["ご依頼をいただきました", "でご依頼", "ご依頼"])
        is_mendan = any(k in body for k in ["面談をさせていただきました", "面談させていただきました"])
        if not (is_junin or is_mendan):
            continue

        sender = msg["account"]["name"]
        # 姓のみ抽出：スペースで分割した最初の要素の先頭2文字
        sender_surname = re.split(r"[\s　]", sender)[0][:2]

        return {
            "面談担当者": sender_surname,
            "受任日": extract_junin_date(body, msg["send_time"]),
            "面談": "済",
            "受任": "有" if is_junin else None,
            "商品選択反": detect_product(body),
            "source_message": body[:300],
        }

    return {}


# ── API エンドポイント ──────────────────────────────────────

class SearchRequest(BaseModel):
    customer_name: str
    record_id: str


class UpdateRequest(BaseModel):
    record_id: str
    fields: dict


@app.post("/api/search")
async def api_search(req: SearchRequest):
    auto = {}
    sources = {}

    # 1. kintone既存データ
    record = await get_kintone_record(req.record_id)

    address = record.get("住所", {}).get("value", "")
    if address:
        pref, city, town = extract_address_parts(address)
        auto["都道府県"] = pref
        auto["市町村名"] = city
        auto["町名"] = town
        sources["住所系"] = "kintone既存住所から分割"

    birthdate = record.get("西暦", {}).get("value", "")
    if birthdate:
        y, m, d = [int(x) for x in birthdate.split("-")]
        _, wareki_year = seireki_to_wareki(y, m, d)
        auto["和暦年"] = wareki_year
        auto["和暦月"] = m
        auto["和暦日"] = d
        auto["依頼人年齢"] = calc_age(birthdate)
        sources["和暦・年齢"] = "生年月日から算出"

    # 連絡票BOX URL取得
    inquiry = record.get("問合せ内容", {}).get("value", "")
    box_url = ""
    box_file_id = None
    m2 = re.search(r"連絡票[：:]\s*(https://app\.box\.com/file/(\d+))", inquiry)
    if m2:
        box_url = m2.group(1)
        box_file_id = m2.group(2)

    # 2. BOX連絡票からふりがな・住所を抽出
    if box_file_id:
        box_text = await get_box_text(box_file_id)
        if box_text:
            # ふりがな
            kana = extract_kana_from_box(box_text, req.customer_name)
            if kana:
                auto["ふりがな"] = kana
                sources["ふりがな"] = "BOX連絡票から抽出"

            # 住所（kintoneが空の場合はBOXから取得）
            if not address:
                box_addr = extract_address_from_box(box_text)
                for key, val in box_addr.items():
                    if val:
                        auto[key] = val
                if box_addr:
                    sources["住所系"] = "BOX連絡票から抽出"

    # 3. Chatwork検索
    cw = await search_chatwork(req.customer_name, box_url)
    if cw:
        if cw.get("面談担当者"):
            auto["面談担当者"] = cw["面談担当者"]
            sources["面談担当者"] = "Chatwork受任報告の送信者"
        if cw.get("受任日"):
            junin = cw["受任日"]
            auto["受任日"] = junin
            sources["受任日"] = 'Chatwork「昨日/本日面談」から'
            kanryo = add_months(date.fromisoformat(junin), 3)
            auto["完了予定日"] = kanryo.isoformat()
            sources["完了予定日"] = "受任日の3か月後"
        if cw.get("面談"):
            auto["面談"] = "済"
            sources["面談"] = "Chatwork面談報告から"
        if cw.get("受任"):
            auto["ドロップダウン_6"] = "有"
            sources["受任"] = 'Chatwork「ご依頼をいただきました」から'
        if cw.get("商品選択反"):
            auto["商品選択反"] = cw["商品選択反"]
            sources["商品選択反"] = f"Chatworkキーワードから判定"

    return {
        "record_id": req.record_id,
        "customer_name": record.get("相談者名", {}).get("value", req.customer_name),
        "auto_fields": auto,
        "sources": sources,
        "chatwork_message": cw.get("source_message", "") if cw else "",
    }


@app.post("/api/update")
async def api_update(req: UpdateRequest):
    record_body = {}
    num_fields = {"和暦年", "和暦月", "和暦日", "依頼人年齢", "受注額"}
    for key, val in req.fields.items():
        if val == "" or val is None:
            continue
        if key in num_fields:
            try:
                record_body[key] = {"value": int(val)}
            except ValueError:
                record_body[key] = {"value": val}
        else:
            record_body[key] = {"value": val}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/record.json",
            headers={
                "X-Cybozu-API-Token": KINTONE_TOKEN,
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"app": KINTONE_APP_ID, "id": req.record_id, "record": record_body},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return {"success": True, "revision": r.json().get("revision")}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
