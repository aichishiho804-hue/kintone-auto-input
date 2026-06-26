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
BOX_CLIENT_ID = os.getenv("BOX_CLIENT_ID", "")
BOX_CLIENT_SECRET = os.getenv("BOX_CLIENT_SECRET", "")
BOX_REFRESH_TOKEN = os.getenv("BOX_REFRESH_TOKEN", "")

_box_access_token: str = BOX_TOKEN
_box_refresh_token: str = BOX_REFRESH_TOKEN
_box_token_expires_at: float = 0.0  # epoch seconds

RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")

import time

async def _update_render_env(new_refresh: str, new_access: str):
    """Render APIで全環境変数を取得して BOX_TOKEN/BOX_REFRESH_TOKEN だけ更新"""
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 現在の全環境変数を取得
            r = await client.get(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            )
            if r.status_code != 200:
                return
            env_list = r.json()
            # BOX_TOKEN と BOX_REFRESH_TOKEN を更新
            for item in env_list:
                if item.get("key") == "BOX_TOKEN":
                    item["value"] = new_access
                elif item.get("key") == "BOX_REFRESH_TOKEN":
                    item["value"] = new_refresh
            # 全体を PUT で更新
            await client.put(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
                json=env_list,
            )
    except Exception:
        pass


async def get_box_access_token() -> str:
    global _box_access_token, _box_refresh_token, _box_token_expires_at
    now = time.time()
    # 有効期限が残り5分以上あればそのまま使用
    if _box_access_token and now < _box_token_expires_at - 300:
        return _box_access_token
    if not BOX_CLIENT_ID or not BOX_CLIENT_SECRET or not _box_refresh_token:
        return _box_access_token
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.box.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": _box_refresh_token,
                "client_id": BOX_CLIENT_ID,
                "client_secret": BOX_CLIENT_SECRET,
            },
        )
    if r.status_code == 200:
        data = r.json()
        _box_access_token = data.get("access_token", _box_access_token)
        _box_refresh_token = data.get("refresh_token", _box_refresh_token)
        expires_in = data.get("expires_in", 3600)
        _box_token_expires_at = now + expires_in
        # Render環境変数を非同期で更新（失敗しても続行）
        import asyncio
        asyncio.create_task(_update_render_env(_box_refresh_token, _box_access_token))
    return _box_access_token


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
    token = await get_box_access_token()
    if not token:
        return ""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.box.com/2.0/files/{file_id}",
            headers={
                "Authorization": f"Bearer {token}",
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
        # status が "none" の場合はBOXにOCR生成をリクエストし少し待つ
        status = text_rep.get("status", {}).get("state", "")
        if status == "none":
            info_url = text_rep.get("info", {}).get("url", "")
            if info_url:
                await client.get(info_url, headers={"Authorization": f"Bearer {token}"})
            import asyncio
            await asyncio.sleep(3)
            # 再取得
            r3 = await client.get(
                f"https://api.box.com/2.0/files/{file_id}",
                headers={"Authorization": f"Bearer {token}", "X-Rep-Hints": "[extracted_text]"},
                params={"fields": "representations"},
            )
            if r3.status_code == 200:
                entries = r3.json().get("representations", {}).get("entries", [])
                text_rep = next((e for e in entries if e.get("representation") == "extracted_text"), None)
                if not text_rep:
                    return ""
        url = text_rep.get("content", {}).get("url_template", "").replace("{+asset_path}", "")
        if not url:
            return ""
        r2 = await client.get(url, headers={"Authorization": f"Bearer {token}"})
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


async def lookup_prefecture_from_zip(zipcode: str) -> str:
    """郵便番号から都道府県をzipcloudで検索"""
    code = zipcode.replace("-", "").replace("ー", "")
    if len(code) != 7:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "https://zipcloud.ibsnet.co.jp/api/search",
                params={"zipcode": code},
            )
        data = r.json()
        results = data.get("results") or []
        if results:
            return results[0].get("address1", "")
    except Exception:
        pass
    return ""


async def extract_address_from_box(text: str) -> dict:
    """BOX連絡票から住所・郵便番号を抽出"""
    result = {}

    # 「住所 (〒444-0948) 岡崎市西本郷町字和志山２４１番地１」パターン
    addr_match = re.search(r'住所\s*[（(〒]?\s*〒?([\d-]{7,8})[）)]?\s*(.+)', text)
    if addr_match:
        zipcode = addr_match.group(1).replace("-", "-")
        result["文字列__1行_"] = zipcode  # 郵便番号
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
            # 都道府県なし → 郵便番号APIで補完
            pref = await lookup_prefecture_from_zip(zipcode)
            if pref:
                result["都道府県"] = pref
            city_match = re.match(r'^(.+?[市区町村郡])', addr_text)
            if city_match:
                city = city_match.group(1)
                rest = addr_text[len(city):]
                town = re.sub(r'字', '', rest)
                town = re.sub(r'[\d０-９]+番地.*$', '', town).strip()
                result["市町村名"] = city
                result["町名"] = town
                result["住所"] = (pref + addr_text) if pref else addr_text

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
    zipcode = record.get("文字列__1行_", {}).get("value", "")
    if address:
        pref, city, town = extract_address_parts(address)
        auto["都道府県"] = pref
        auto["市町村名"] = city
        auto["町名"] = town
        sources["住所系"] = "kintone既存住所から分割"
        # 都道府県が取れなかった場合は郵便番号で補完
        if not pref and zipcode:
            pref = await lookup_prefecture_from_zip(zipcode)
            if pref:
                auto["都道府県"] = pref
                sources["住所系"] = "kintone既存住所から分割（都道府県は郵便番号から補完）"

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
                box_addr = await extract_address_from_box(box_text)
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
            sources["ドロップダウン_6"] = 'Chatwork「ご依頼をいただきました」から'
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


JYUNIN_APP_ID = os.getenv("JYUNIN_APP_ID", "56")
JYUNIN_TOKEN = os.getenv("JYUNIN_TOKEN", "")
BOX_JYUNIN_PARENT_FOLDER = os.getenv("BOX_JYUNIN_PARENT_FOLDER", "98466904273")

BOX_SUBFOLDERS = [
    "①戸籍・印鑑証明書・免許証",
    "②登記情報・評価証明・名寄",
    "③通帳・証券会社資料",
    "④作成書類",
    "⑤チェック済",
    "⑥完了その他",
    "⑦不動産仲介",
]


def extract_deceased_from_box(text: str) -> dict:
    result = {}
    # 故人氏名とふりがな: 「故人氏名 小田　初伸 (オダ　ハツノブ) 様」
    m = re.search(r'故人氏名\s+(.+?)\s*[（(]([ァ-ヶー\s　]+)[）)]', text)
    if m:
        name = re.sub(r'[\s　]+', '', m.group(1)).replace('様', '')
        result["被相続人"] = name
        kana = m.group(2).strip()
        hiragana = "".join(
            chr(ord(c) - 0x60) if "ァ" <= c <= "ン" else c for c in kana
        ).replace("　", "").replace(" ", "")
        result["被相続人ふりがな"] = hiragana

    # 故人続柄: 「故人続柄 父」
    m2 = re.search(r'故人続柄\s+(\S+)', text)
    if m2:
        result["被相続人から見た依頼者の続柄"] = m2.group(1)

    # 葬儀施行日時: 「葬儀施行 日時 2026.06.07」
    m3 = re.search(r'葬儀施行\s*日時\s+(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', text)
    if m3:
        result["西暦2"] = f"{m3.group(1)}-{int(m3.group(2)):02d}-{int(m3.group(3)):02d}"

    return result


async def find_box_folder_by_name(parent_id: str, name: str) -> Optional[str]:
    # テスト用プレフィックスを除去して実際のフォルダ名に合わせる
    name = re.sub(r'^テスト用', '', name).strip()
    token = await get_box_access_token()
    if not token:
        return None
    # BOX Search APIで親フォルダ内を名前検索（件数上限の回避）
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.box.com/2.0/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "query": name,
                "type": "folder",
                "ancestor_folder_ids": parent_id,
                "fields": "id,name,parent",
                "limit": 50,
            },
        )
    if r.status_code == 200:
        for item in r.json().get("entries", []):
            if item.get("name") == name and item.get("parent", {}).get("id") == parent_id:
                return item["id"]
    # フォールバック：ページネーション付きフォルダリスト
    async with httpx.AsyncClient(timeout=15) as client:
        offset = 0
        while True:
            r = await client.get(
                f"https://api.box.com/2.0/folders/{parent_id}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "id,name,type", "limit": 1000, "offset": offset},
            )
            if r.status_code != 200:
                break
            data = r.json()
            for item in data.get("entries", []):
                if item.get("type") == "folder" and item.get("name") == name:
                    return item["id"]
            total = data.get("total_count", 0)
            offset += len(data.get("entries", []))
            if offset >= total:
                break
    return None


async def create_box_subfolder(parent_id: str, name: str) -> Optional[str]:
    token = await get_box_access_token()
    if not token:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.box.com/2.0/folders",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"name": name, "parent": {"id": parent_id}},
        )
    if r.status_code in (200, 201):
        return r.json().get("id")
    if r.status_code == 409:  # already exists
        return "exists"
    return None


class JyuninSearchRequest(BaseModel):
    customer_name: str
    hibikyo_record_id: str  # app57のレコードID
    box_file_id: Optional[str] = None


class JyuninUpdateRequest(BaseModel):
    jyunin_record_id: str
    fields: dict


@app.post("/api/jyunin/search")
async def api_jyunin_search(req: JyuninSearchRequest):
    # 1. app56を反響レコード番号で検索
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/records.json",
            headers={"X-Cybozu-API-Token": JYUNIN_TOKEN},
            params={
                "app": JYUNIN_APP_ID,
                "query": f'反響レコード番号 = {req.hibikyo_record_id}',
                "fields[0]": "$id",
            },
        )

    if r.status_code != 200 or not r.json().get("records"):
        raise HTTPException(
            status_code=404,
            detail="受任管理表レコードが見つかりません。kintoneの反響管理表画面で「受注したら案件管理へ行く」を先にクリックしてください。"
        )

    jyunin_record_id = r.json()["records"][0]["$id"]["value"]
    created = False

    # 2. BOX連絡票から故人情報を抽出
    auto = {}
    sources = {}
    box_file_id = req.box_file_id
    if not box_file_id:
        # box_file_idがない場合はapp57の問合せ内容から取得
        hibikyo = await get_kintone_record(req.hibikyo_record_id)
        inquiry = hibikyo.get("問合せ内容", {}).get("value", "")
        m = re.search(r"連絡票[：:]\s*https://app\.box\.com/file/(\d+)", inquiry)
        if m:
            box_file_id = m.group(1)

    if box_file_id:
        box_text = await get_box_text(box_file_id)
        if box_text:
            deceased = extract_deceased_from_box(box_text)
            for k, v in deceased.items():
                if v:
                    auto[k] = v
            if deceased:
                sources["故人情報"] = "BOX連絡票から抽出"

    return {
        "jyunin_record_id": jyunin_record_id,
        "created": created,
        "auto_fields": auto,
        "sources": sources,
    }


@app.post("/api/jyunin/update")
async def api_jyunin_update(req: JyuninUpdateRequest):
    record_body = {}
    date_fields = {"西暦2", "西暦3"}
    for key, val in req.fields.items():
        if val == "" or val is None:
            continue
        record_body[key] = {"value": val}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/record.json",
            headers={
                "X-Cybozu-API-Token": JYUNIN_TOKEN,
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"app": JYUNIN_APP_ID, "id": req.jyunin_record_id, "record": record_body},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"success": True}


@app.post("/api/jyunin/create_folders")
async def api_create_folders(req: JyuninSearchRequest):
    # kintone受任管理表から相談者名（依頼者名）を取得してBOXフォルダを検索
    async with httpx.AsyncClient(timeout=10) as client:
        r56 = await client.get(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/records.json",
            headers={"X-Cybozu-API-Token": JYUNIN_TOKEN},
            params={"app": JYUNIN_APP_ID, "query": f'反響レコード番号 = {req.hibikyo_record_id}', "fields[0]": "相談者名"},
        )
    folder_name = req.customer_name  # フォールバック
    if r56.status_code == 200 and r56.json().get("records"):
        folder_name = r56.json()["records"][0].get("相談者名", {}).get("value", "") or req.customer_name

    folder_id = await find_box_folder_by_name(BOX_JYUNIN_PARENT_FOLDER, folder_name)
    if not folder_id:
        raise HTTPException(status_code=404, detail=f"BOXに「{folder_name}」フォルダが見つかりません。kintoneによるフォルダ作成完了後に実行してください。")

    results = []
    for name in BOX_SUBFOLDERS:
        result = await create_box_subfolder(folder_id, name)
        results.append({"name": name, "status": "作成済" if result == "exists" else ("成功" if result else "失敗")})

    return {"folder_name": folder_name, "folder_id": folder_id, "subfolders": results}


# ── BOX書類スキャン ────────────────────────────────────────

WAREKI_OFFSETS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}

def parse_date_from_text(text: str) -> Optional[str]:
    m = re.search(r'(明治|大正|昭和|平成|令和)\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日', text)
    if m:
        year = WAREKI_OFFSETS[m.group(1)] + int(m.group(2))
        return f"{year}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
    m = re.search(r'(\d{4})\s*年\s*(\d+)\s*月\s*(\d+)\s*日', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


async def get_box_folder_files_text(folder_id: str) -> str:
    token = await get_box_access_token()
    if not token:
        return ""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.box.com/2.0/folders/{folder_id}/items",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,name,type", "limit": 100},
        )
    if r.status_code != 200:
        return ""
    texts = []
    for item in r.json().get("entries", []):
        if item["type"] == "file":
            t = await get_box_text(item["id"])
            if t.strip():
                texts.append(f"==[{item['name']}]==\n{t}")
    return "\n".join(texts)


def parse_docs_for_jyunin(text: str) -> tuple:
    fields: dict = {}
    sources: dict = {}

    # 被相続人出生日 (西暦3) — 戸籍謄本「出生 昭和XX年…」
    m = re.search(r'出\s*生\s+((?:明治|大正|昭和|平成|令和)\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)', text)
    if m:
        d = parse_date_from_text(m.group(1))
        if d:
            fields["西暦3"] = d
            sources["西暦3"] = "戸籍書類（被相続人出生日）"

    # 被相続人本籍 (address_0) — 都道府県で始まる本籍行
    m = re.search(
        r'本\s*籍\s+((?:北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|東京都|神奈川県|'
        r'新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|'
        r'和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|'
        r'大分県|宮崎県|鹿児島県|沖縄県)[^\n]{0,60})',
        text,
    )
    if m:
        fields["address_0"] = m.group(1).strip()
        sources["address_0"] = "戸籍書類（本籍）"

    # 戸主/筆頭者 (文字列__1行__4)
    m = re.search(r'(?:筆\s*頭\s*者|戸\s*主)\s+([^\n\d]{2,20})', text)
    if m:
        name = re.sub(r'\s+', '', m.group(1)).strip()
        if name:
            fields["文字列__1行__4"] = name
            sources["文字列__1行__4"] = "戸籍書類（戸主）"

    # 依頼者出生日 (西暦) — 免許証「生年月日 昭和XX年…」
    for m in re.finditer(r'生\s*年\s*月\s*日\s+((?:明治|大正|昭和|平成|令和)\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)', text):
        d = parse_date_from_text(m.group(1))
        if d and "西暦" not in fields:
            fields["西暦"] = d
            sources["西暦"] = "免許証（依頼者出生日）"
            break

    # 依頼者本籍 (address_2) — 免許証の本籍欄（address_0未取得時のみ）
    if "address_0" not in fields:
        m = re.search(r'本\s*籍\s+([^\n]{2,40})', text)
        if m:
            fields["address_2"] = m.group(1).strip()
            sources["address_2"] = "免許証（本籍）"

    # 被相続人から見た依頼者の続柄 — 戸籍「長男/長女/配偶者…」
    if "被相続人から見た依頼者の続柄" not in fields:
        m = re.search(r'(長男|長女|次男|次女|三男|三女|四男|四女|配偶者|妻|夫|養子|養女)', text)
        if m:
            fields["被相続人から見た依頼者の続柄"] = m.group(1)
            sources["被相続人から見た依頼者の続柄"] = "戸籍書類（続柄）"

    # 相続人 (相続人フィールド) — 続柄に続く氏名を複数抽出
    heirs = re.findall(r'(?:長男|長女|次男|次女|三男|三女|配偶者)\s+([^\n\s]{2,8})', text)
    if heirs:
        fields["相続人"] = "、".join(dict.fromkeys(heirs)[:5])
        sources["相続人"] = "戸籍書類（相続人）"

    return fields, sources


class ScanDocsRequest(BaseModel):
    hibikyo_record_id: str
    jyunin_record_id: str
    customer_name: str
    box_folder_id: Optional[str] = None  # create_foldersで取得したフォルダID


@app.post("/api/jyunin/scan_docs")
async def api_scan_docs(req: ScanDocsRequest):
    # フォルダIDが直接渡された場合は再検索不要
    folder_id = req.box_folder_id
    if not folder_id:
        folder_id = await find_box_folder_by_name(BOX_JYUNIN_PARENT_FOLDER, req.customer_name)
    if not folder_id:
        raise HTTPException(status_code=404, detail=f"BOXに「{req.customer_name}」フォルダが見つかりません。")

    # サブフォルダ一覧取得
    token = await get_box_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.box.com/2.0/folders/{folder_id}/items",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,name,type", "limit": 100},
        )
    subfolders = {item["name"]: item["id"] for item in r.json().get("entries", []) if item["type"] == "folder"}

    # ①③⑥を優先してスキャン
    scan_targets = ["⑥完了その他", "①戸籍・印鑑証明書・免許証", "③通帳・証券会社資料", "②登記情報・評価証明・名寄"]
    combined_text = ""
    scanned = []
    for sf_name in scan_targets:
        sf_id = subfolders.get(sf_name)
        if sf_id:
            t = await get_box_folder_files_text(sf_id)
            if t.strip():
                combined_text += "\n" + t
                scanned.append(sf_name)

    if not combined_text.strip():
        return {"message": "書類が見つかりませんでした。BOXに書類をアップロードしてから実行してください。", "fields": {}, "sources": {}, "scanned": []}

    extracted, sources = parse_docs_for_jyunin(combined_text)
    if not extracted:
        return {"message": "書類を読み取りましたが、対象フィールドのデータが抽出できませんでした。", "fields": {}, "sources": {}, "scanned": scanned}

    # 現在のapp56レコードを取得して未入力フィールドのみ更新
    async with httpx.AsyncClient(timeout=10) as client:
        r56 = await client.get(
            f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/record.json",
            headers={"X-Cybozu-API-Token": JYUNIN_TOKEN},
            params={"app": JYUNIN_APP_ID, "id": req.jyunin_record_id},
        )
    current = r56.json().get("record", {}) if r56.status_code == 200 else {}

    update_body = {}
    updated = {}
    for key, val in extracted.items():
        if not current.get(key, {}).get("value") and val:
            update_body[key] = {"value": val}
            updated[key] = val

    if update_body:
        async with httpx.AsyncClient(timeout=10) as client:
            ru = await client.put(
                f"https://{KINTONE_DOMAIN}/k/guest/{KINTONE_GUEST_ID}/v1/record.json",
                headers={"X-Cybozu-API-Token": JYUNIN_TOKEN, "Content-Type": "application/json; charset=utf-8"},
                json={"app": JYUNIN_APP_ID, "id": req.jyunin_record_id, "record": update_body},
            )
        if ru.status_code != 200:
            raise HTTPException(status_code=ru.status_code, detail=f"kintone更新エラー: {ru.text}")

    return {
        "message": f"{len(updated)}件のフィールドを自動入力しました。" if updated else "新たに入力できる項目はありませんでした（既入力済）。",
        "fields": updated,
        "sources": sources,
        "scanned": scanned,
    }


@app.get("/api/debug/box_search")
async def debug_box_search(name: str):
    results = {"search_name": name, "parent_folder_id": BOX_JYUNIN_PARENT_FOLDER}
    try:
        token = await get_box_access_token()
        results["token_ok"] = bool(token)
        results["token_prefix"] = token[:10] if token else None

        async with httpx.AsyncClient(timeout=15) as client:
            r2 = await client.get(
                f"https://api.box.com/2.0/folders/{BOX_JYUNIN_PARENT_FOLDER}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "id,name,type", "limit": 10},
            )
        results["folder_list_status"] = r2.status_code
        results["folder_total_count"] = r2.json().get("total_count") if r2.status_code == 200 else None
        results["folder_list_sample"] = [i["name"] for i in r2.json().get("entries", [])] if r2.status_code == 200 else r2.text[:500]

        async with httpx.AsyncClient(timeout=15) as client:
            rs = await client.get(
                "https://api.box.com/2.0/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"query": name, "type": "folder", "ancestor_folder_ids": BOX_JYUNIN_PARENT_FOLDER,
                        "fields": "id,name,parent", "limit": 10},
            )
        results["search_status"] = rs.status_code
        results["search_entries"] = [{"id": e["id"], "name": e["name"], "parent_id": e.get("parent", {}).get("id")} for e in rs.json().get("entries", [])] if rs.status_code == 200 else rs.text[:500]
    except Exception as e:
        results["error"] = str(e)
    return results


@app.get("/api/debug/box_file_text")
async def debug_box_file_text(file_id: str):
    """BOXファイルのOCRテキストを確認するデバッグ用"""
    try:
        token = await get_box_access_token()
        result = {"token_ok": bool(token), "token_prefix": token[:10] if token else ""}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.box.com/2.0/files/{file_id}",
                headers={"Authorization": f"Bearer {token}", "X-Rep-Hints": "[extracted_text]"},
                params={"fields": "representations,name"},
            )
        result["api_status"] = r.status_code
        result["api_error"] = r.text[:500]
        if r.status_code != 200:
            # トークンが誰のものか確認
            async with httpx.AsyncClient(timeout=15) as client2:
                r_me = await client2.get(
                    "https://api.box.com/2.0/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
            result["me_status"] = r_me.status_code
            me = r_me.json() if r_me.status_code == 200 else {}
            result["me_login"] = me.get("login", "")
            result["me_name"] = me.get("name", "")
            result["me_type"] = me.get("type", "")
            result["me_raw"] = r_me.text[:300]
            return result
        data = r.json()
        result["file_name"] = data.get("name", "")
        entries = data.get("representations", {}).get("entries", [])
        result["rep_count"] = len(entries)
        result["rep_types"] = [e.get("representation") for e in entries]
        text_rep = next((e for e in entries if e.get("representation") == "extracted_text"), None)
        if text_rep:
            result["rep_status"] = text_rep.get("status", {}).get("state", "")
            result["rep_url"] = text_rep.get("content", {}).get("url_template", "")
        text = await get_box_text(file_id)
        addr = await extract_address_from_box(text) if text else {}
        result["text_length"] = len(text)
        result["text_preview"] = text[:500] if text else ""
        result["address_extracted"] = addr
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
