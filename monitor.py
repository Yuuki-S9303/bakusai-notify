"""
爆サイ監視スクリプト
- Google Sheetsから監視リストを読み込み
- 爆サイの最新スレッドを自動検出
- キーワード検知でDiscord通知
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
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]          # GitHub Secret
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # GitHub Secret（JSON文字列）
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
    B: category              （爆サイカテゴリ名、URL用）
    C: detect_keyword        （検知したいワード）
    D: discord_webhook_url   （通知先Webhook URL）
    E: active                （TRUE/FALSEで監視ON/OFF）
    """
    import json as _json
    creds_info = _json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:E100"   # 2行目以降を取得（1行目はヘッダー）
    ).execute()

    rows = result.get("values", [])
    targets = []
    for row in rows:
        if len(row) < 5:
            continue
        thread_title_keyword, category, detect_keyword, webhook_url, active = row[:5]
        if active.strip().upper() != "TRUE":
            continue
        targets.append({
            "thread_title_keyword": thread_title_keyword.strip(),
            "category": category.strip(),
            "detect_keyword": detect_keyword.strip(),
            "discord_webhook_url": webhook_url.strip(),
        })
    return targets

# ── 最新スレッドURL取得 ───────────────────────────────
def get_latest_thread_url(category: str, title_keyword: str) -> str | None:
    """
    爆サイのカテゴリページを検索し、タイトルキーワードに一致する
    最新（最初に見つかった）スレッドのURLを返す。
    """
    search_url = f"{BAKUSAI_BASE}/talkshow/tp=1/c={category}/q={requests.utils.quote(title_keyword)}/"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] スレッド検索失敗: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # スレッド一覧リンクを探す（実際のHTMLに合わせて要調整）
    for a in soup.select("a[href*='/talkshow/']"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if title_keyword in text:
            if href.startswith("http"):
                return href
            return BAKUSAI_BASE + href

    print(f"[WARN] キーワード '{title_keyword}' に一致するスレッドが見つかりませんでした")
    return None

# ── スレッド書き込み取得 ──────────────────────────────
def get_posts(thread_url: str) -> list[dict]:
    """
    スレッドページの書き込みを取得する。
    戻り値: [{"id": "xxx", "text": "...", "url": "..."}]
    """
    try:
        resp = requests.get(thread_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] スレッド取得失敗: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []

    # 実際の爆サイHTMLに合わせて要調整
    for item in soup.select(".res-item, .bbs-res, article"):
        post_id = item.get("id", "")
        text = item.get_text(separator=" ", strip=True)
        post_url = f"{thread_url}#{post_id}" if post_id else thread_url

        if post_id and text:
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
def notify_discord(webhook_url: str, keyword: str, post: dict, thread_url: str):
    message = (
        f"🔔 **キーワード検知: `{keyword}`**\n"
        f"🧵 スレッド: {thread_url}\n"
        f"🔗 投稿URL: {post['url']}\n"
        f"📝 内容（抜粋）: {post['text'][:200]}"
    )
    payload = {"content": message}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[OK] Discord通知送信: post_id={post['id']}")
    except Exception as e:
        print(f"[ERROR] Discord通知失敗: {e}")

# ── メイン処理 ────────────────────────────────────────
def main():
    print("=== 爆サイ監視スタート ===")

    # 通知済みIDを読み込み
    notified_ids = load_notified_ids()
    updated = False

    # 監視リストをスプシから取得
    try:
        targets = load_targets_from_sheet()
    except Exception as e:
        print(f"[ERROR] スプシ読み込み失敗: {e}")
        return

    print(f"監視対象: {len(targets)}件")

    for target in targets:
        keyword_title = target["thread_title_keyword"]
        category = target["category"]
        detect_keyword = target["detect_keyword"]
        webhook_url = target["discord_webhook_url"]

        print(f"\n--- [{keyword_title}] 処理中 ---")

        # 最新スレッドURLを取得
        thread_url = get_latest_thread_url(category, keyword_title)
        if not thread_url:
            continue

        print(f"最新スレッド: {thread_url}")

        # 書き込みを取得
        posts = get_posts(thread_url)
        print(f"取得した書き込み数: {len(posts)}")

        # キーワード検知
        notified_key = f"{keyword_title}_{category}"
        if notified_key not in notified_ids:
            notified_ids[notified_key] = []

        for post in posts:
            post_id = post["id"]

            # 通知済みはスキップ
            if post_id in notified_ids[notified_key]:
                continue

            # キーワード検知
            if detect_keyword in post["text"]:
                notify_discord(webhook_url, detect_keyword, post, thread_url)
                notified_ids[notified_key].append(post_id)
                updated = True

        time.sleep(2)  # サーバー負荷軽減

    # 通知済みIDを保存
    if updated:
        save_notified_ids(notified_ids)
        print("\n通知済みIDを更新しました")

    print("\n=== 処理完了 ===")

if __name__ == "__main__":
    main()
