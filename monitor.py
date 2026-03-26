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
    B: category              （爆サイのctgid番号）
    C: detect_keyword        （検知ワード、複数はカンマ区切り）
    D: detect_condition      （AND または OR）
    E: active                （TRUE/FALSEで監視ON/OFF）
    """
    import json as _json
    creds_info = _json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:E100"
    ).execute()

    rows = result.get("values", [])
    targets = []
    for row in rows:
        if len(row) < 5:
            continue
        thread_title_keyword, category, detect_keyword, detect_condition, active = row[:5]
        if active.strip().upper() != "TRUE":
            continue
        targets.append({
            "thread_title_keyword": thread_title_keyword.strip(),
            "category": category.strip(),
            "detect_keywords": [kw.strip() for kw in detect_keyword.split(",")],
            "detect_condition": detect_condition.strip().upper() or "OR",
        })
    return targets

# ── キーワードマッチ判定 ──────────────────────────────
def is_match(text: str, keywords: list[str], condition: str) -> bool:
    if condition == "AND":
        return all(kw in text for kw in keywords)
    else:  # OR（デフォルト）
        return any(kw in text for kw in keywords)

# ── 最新スレッドURL取得 ───────────────────────────────
def get_latest_thread_url(category: str, title_keyword: str) -> str | None:
    """
    爆サイのカテゴリページを検索し、タイトルキーワードに一致する
    最新スレッドのURLを返す。
    """
    search_url = (
        f"{BAKUSAI_BASE}/talkshow/tp=1/ctgid={category}/"
        f"q={requests.utils.quote(title_keyword)}/"
    )
    print(f"検索URL: {search_url}")

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] スレッド検索失敗: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # スレッド一覧からタイトルキーワードに一致するものを取得
    for a in soup.select("a[href*='/thr_res/']"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        # キーワードの一部でも一致すれば最新スレとみなす
        for kw in title_keyword.split():
            if kw in text:
                full_url = href if href.startswith("http") else BAKUSAI_BASE + href
                # ページ1を明示的に指定
                if "/p=" not in full_url:
                    full_url = full_url.rstrip("/") + "/p=1/"
                return full_url

    print(f"[WARN] キーワード '{title_keyword}' に一致するスレッドが見つかりませんでした")
    return None

# ── スレッド書き込み取得（全ページ） ─────────────────
def get_all_posts(thread_url: str) -> list[dict]:
    """
    スレッドの全ページから書き込みを取得する。
    """
    all_posts = []
    page = 1

    while True:
        # ページURLを生成（p=1, p=2, ...）
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

        # 次ページの存在確認
        next_link = soup.find("a", string=lambda t: t and "次" in t)
        if not next_link:
            break

        page += 1
        time.sleep(1)

    return all_posts

def parse_posts(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    BeautifulSoupオブジェクトから投稿を抽出する。
    """
    posts = []

    # 爆サイの投稿要素を取得（実際のHTMLに合わせて調整済み）
    for item in soup.select(".resItem, .res-item, [id^='res']"):
        post_id = item.get("id", "").replace("res", "").strip()
        text_el = item.select_one(".resText, .res-text, .text")
        text = text_el.get_text(separator=" ", strip=True) if text_el else item.get_text(separator=" ", strip=True)

        if post_id and text:
            post_url = f"{base_url.split('/p=')[0]}/#res{post_id}"
            posts.append({
                "id": post_id,
                "text": text,
                "url": post_url,
            })

    return posts

# ── 通知済みID管理 ────────────────────────────────────
def load_notified_ids() -> dict:
    if os.path.exists(NOTIFIED_IDS_FILE):
        with open(NOTIFIED_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_notified_ids(data: dict):
    with open(NOTIFIED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Discord通知 ───────────────────────────────────────
def notify_discord(keywords: list[str], condition: str, post: dict, thread_url: str):
    keyword_str = f" {condition} ".join(keywords)
    message = (
        f"🔔 **キーワード検知: `{keyword_str}`**\n"
        f"🧵 スレッド: {thread_url}\n"
        f"🔗 投稿URL: {post['url']}\n"
        f"📝 内容（抜粋）: {post['text'][:200]}"
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
        category = target["category"]
        detect_keywords = target["detect_keywords"]
        detect_condition = target["detect_condition"]

        print(f"\n--- [{keyword_title}] 処理中 ---")
        print(f"検知キーワード: {detect_keywords} ({detect_condition}条件)")

        # 最新スレッドURLを取得
        thread_url = get_latest_thread_url(category, keyword_title)
        if not thread_url:
            continue

        print(f"最新スレッド: {thread_url}")

        # 全ページの書き込みを取得
        posts = get_all_posts(thread_url)
        print(f"取得した書き込み数: {len(posts)}")

        # 通知済みIDの管理キー
        notified_key = f"{keyword_title}_{category}"
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
