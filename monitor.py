"""
爆サイ監視スクリプト
- Google Sheetsから監視リストを読み込み
- 爆サイの最新スレッドを自動検出
- キーワード検知でDiscord通知（AND/OR条件対応）
"""

import os
import json
import time
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── 定数 ──────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NOTIFIED_IDS_FILE = "notified_ids.json"

BAKUSAI_BASE = "https://bakusai.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Google Sheets 読み込み ────────────────────────────
def load_targets_from_sheet():
    """
    スプシの構成（1行目はヘッダー）:
    A: thread_title_keyword  （スレ検索キーワード）
    B: acode                 （爆サイのacode）
    C: ctgid                 （爆サイのカテゴリID）
    D: bid                   （爆サイの掲示板ID）
    E: detect_keyword        （検知ワード、複数はカンマ区切り）
    F: detect_condition      （AND または OR）
    G: active                （TRUE/FALSEで監視ON/OFF）
    """
    import json as _json
    creds_info = _json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:G100"
    ).execute()

    rows = result.get("values", [])
    targets = []
    for row in rows:
        if len(row) < 7:
            continue
        thread_title_keyword, acode, ctgid, bid, detect_keyword, detect_condition, active = row[:7]
        if active.strip().upper() != "TRUE":
            continue
        kws = [kw.strip() for kw in detect_keyword.split(",") if kw.strip()]
        targets.append({
            "thread_title_keyword": thread_title_keyword.strip(),
            "acode": acode.strip(),
            "ctgid": ctgid.strip(),
            "bid": bid.strip(),
            "detect_keywords": kws,
            "detect_condition": detect_condition.strip().upper() or "OR",
        })
    return targets

# ── キーワードマッチ判定 ──────────────────────────────
def is_match(text, keywords, condition):
    # キーワード未設定 or "*" → 全件マッチ
    if not keywords or keywords == ["*"]:
        return True
    if condition == "AND":
        return all(kw in text for kw in keywords)
    else:
        return any(kw in text for kw in keywords)

# ── 最新スレッドURL取得 ───────────────────────────────
def get_latest_thread_url(acode, ctgid, bid, title_keyword):
    encoded_word = requests.utils.quote(title_keyword, safe="")
    search_url = (
        f"{BAKUSAI_BASE}/sch_thr_thread/acode={acode}/ctgid={ctgid}/bid={bid}/p=1/"
        f"sch=thr_sch/sch_range=board/word={encoded_word}/"
    )
    print(f"検索URL: {search_url}")

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] スレッド検索失敗: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    for a in soup.select("a[href*='/thr_res/']"):
        href = a.get("href", "")
        if not href:
            continue
        # bidが一致するリンクのみ取得
        if f"bid={bid}" not in href:
            continue
        full_url = href if href.startswith("http") else BAKUSAI_BASE + href
        if "/p=" not in full_url:
            full_url = full_url.rstrip("/") + "/p=1/"
        print(f"スレッド発見: {full_url}")
        return full_url

# ── スレッド書き込み取得（全ページ） ─────────────────
def get_all_posts(thread_url):
    all_posts = []
    page = 1

    while True:
        paged_url = thread_url.replace("/p=1/", f"/p={page}/") \
            if "/p=" in thread_url \
            else thread_url.rstrip("/") + f"/p={page}/"

        print(f"取得中: {paged_url}")

        try:
            resp = requests.get(paged_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"[ERROR] ページ取得失敗: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        posts = parse_posts(soup, paged_url)

        if not posts:
            break

        all_posts.extend(posts)

        next_link = soup.find("a", string=lambda t: t and "次" in t)
        if not next_link:
            break

        page += 1
        time.sleep(1)

    return all_posts

def parse_posts(soup, base_url):
    posts = []
    for item in soup.select("div.article[id^='res']"):
        post_id = item.get("id", "").replace("res", "").strip()
        text_el = item.select_one(".resbody")
        if not text_el:
            continue
        for overlay in text_el.select(".resOverlay"):
            overlay.decompose()
        text = text_el.get_text(separator=" ", strip=True)

        # 投稿日時を取得
        date_el = item.select_one("span[itemprop='commentTime']")
        post_date = date_el.get_text(strip=True) if date_el else ""

        if post_id and text:
            base = base_url.split("/p=")[0].rstrip("/")
            post_url = f"{base}/#{item.get('id')}"
            posts.append({
                "id": post_id,
                "text": text,
                "url": post_url,
                "date": post_date,
            })
    return posts

# ── 通知済みID管理 ────────────────────────────────────
def load_notified_ids():
    if os.path.exists(NOTIFIED_IDS_FILE):
        with open(NOTIFIED_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_notified_ids(data):
    with open(NOTIFIED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Discord通知 ───────────────────────────────────────
def notify_discord(keywords, condition, post, thread_url):
    if keywords and keywords != ["*"]:
        header = f"🔔 キーワード「{'、'.join(keywords)}」を検知しました"
    else:
        header = "🔔 新着書き込みを検知しました"
    message = (
        f"{header}\n\n"
        f"📅 {post.get('date', '')}\n"
        f"📝 {post['text'][:200]}\n\n"
        f"🔗 {post['url']}"
    )
    payload = {"content": message}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[OK] Discord通知送信: post_id={post['id']}")
    except Exception as e:
        print(f"[ERROR] Discord通知失敗: {e}")

# ── メイン処理 ────────────────────────────────────────
def main():
    print("=== 爆サイ監視スタート ===")

    notified_ids = load_notified_ids()
    updated = False

    try:
        targets = load_targets_from_sheet()
    except Exception as e:
        print(f"[ERROR] スプシ読み込み失敗: {e}")
        return

    print(f"監視対象: {len(targets)}件")

    for target in targets:
        keyword_title = target["thread_title_keyword"]
        acode = target["acode"]
        ctgid = target["ctgid"]
        bid = target["bid"]
        detect_keywords = target["detect_keywords"]
        detect_condition = target["detect_condition"]

        print(f"\n--- [{keyword_title}] 処理中 ---")
        print(f"検知キーワード: {detect_keywords} ({detect_condition}条件)")

        thread_url = get_latest_thread_url(acode, ctgid, bid, keyword_title)
        if not thread_url:
            continue

        print(f"最新スレッド: {thread_url}")

        posts = get_all_posts(thread_url)
        print(f"取得した書き込み数: {len(posts)}")

        notified_key = f"{keyword_title}_{ctgid}_{bid}"
        if notified_key not in notified_ids:
            notified_ids[notified_key] = []

        for post in posts:
            post_id = post["id"]

            if post_id in notified_ids[notified_key]:
                continue

            if is_match(post["text"], detect_keywords, detect_condition):
                notify_discord(detect_keywords, detect_condition, post, thread_url)
                notified_ids[notified_key].append(post_id)
                updated = True

        time.sleep(2)

    if updated:
        save_notified_ids(notified_ids)
        print("\n通知済みIDを更新しました")

    print("\n=== 処理完了 ===")

if __name__ == "__main__":
    main()
