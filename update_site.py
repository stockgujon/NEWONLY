"""
台灣財經儀表板 - 主程式
功能：抓取RSS新聞 → 分類存入資料庫 → 產生 index.html
三個分類（總經／台股國際股市／ETF）皆以條列清單呈現，不需要任何API金鑰。

執行方式：python update_site.py
"""

import os
import re
import time
import random
import sqlite3
import datetime
import email.utils
from html import escape

import feedparser
import requests

# ============================================================
# 設定區
# ============================================================

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "news.db")
OUTPUT_HTML = os.path.join(os.path.dirname(__file__), "index.html")

# RSS 來源
# 台股/總經 共用來源：中央社「產經證券」
CNA_FINANCE_RSS = "https://feeds.feedburner.com/rsscna/finance"
# 國際/總經 共用來源：經濟日報「國際」
UDN_INTL_RSS = "https://money.udn.com/rssfeed/news/1001/5588?ch=money"
# ETF 來源：經濟日報「理財」分類（此網址格式與已驗證可用的「國際」分類相同結構，
# 較為可靠），實際ETF內容透過下方 ETF_KEYWORDS 關鍵字篩選取得
UDN_LICAI_RSS = "https://money.udn.com/rssfeed/news/1001/5592?ch=money"

# ETF 相關關鍵字，用來從「理財」分類中篩選出真正的ETF新聞
ETF_KEYWORDS = [
    "ETF", "0050", "0056", "0052", "00878", "00919", "00713", "00929",
    "00981", "00982", "00985", "006208", "00631L", "高股息", "市值型",
    "主動式ETF", "定期定額", "受益人數", "投信", "基金",
]

# 「牽動台股的國際大事」關鍵字清單 —— 用來從國際新聞中篩選
INTL_KEYWORDS = [
    "聯準會", "Fed", "升息", "降息", "費城半導體", "費半", "那斯達克",
    "台積電ADR", "美股期貨", "關稅", "晶片法案", "AI晶片", "出口管制",
    "美債", "殖利率", "GDP", "通膨", "CPI", "油價", "布蘭特", "西德州",
]

# 「總經」相關關鍵字（給 AI 篩選前先做粗篩，減少丟給 AI 的雜訊）
MACRO_KEYWORDS = [
    "央行", "升息", "降息", "利率", "通膨", "物價", "CPI", "PPI",
    "匯率", "新台幣", "美元", "日圓", "貿易", "進出口", "出超", "入超",
    "GDP", "經濟成長", "油價", "布蘭特", "西德州", "就業", "失業率",
    "公債", "殖利率", "關稅", "財政", "聯準會", "Fed",
]

# 明確要排除的個股/雜訊關鍵字（即使含有總經關鍵字也優先排除）
EXCLUDE_KEYWORDS = [
    "彩券", "今彩539", "大樂透", "威力彩", "核安演習", "地震",
]

USER_AGENT = "tw-finance-dashboard/1.0 (personal non-commercial use)"

# 每個分類希望顯示的新聞則數範圍
ARTICLES_MIN = 10
ARTICLES_MAX = 15
# 優先只看 24 小時內的新聞；不夠的話才放寬到這個天數
FALLBACK_DAYS = 3


# ============================================================
# 資料庫
# ============================================================

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            pub_date TEXT,
            pub_date_ts REAL,
            fetched_at TEXT NOT NULL,
            description TEXT
        )
    """)
    conn.commit()
    return conn


def parse_pubdate(raw_date):
    """把RSS的發布時間字串轉成統一的時間戳記，方便之後排序與篩選。
    解析失敗就回傳 None（該篇新聞篩選時會被視為「時間不明」，排在最後）。"""
    if not raw_date:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def save_article(conn, title, link, source, category, pub_date, description):
    pub_date_ts = parse_pubdate(pub_date)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO articles
               (title, link, source, category, pub_date, pub_date_ts, fetched_at, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, link, source, category, pub_date, pub_date_ts,
             datetime.datetime.now().isoformat(), description),
        )
    except sqlite3.Error as e:
        print(f"[警告] 存入資料庫失敗: {e}")


# ============================================================
# RSS 抓取（含穩健性設計：自訂 UA、隨機延遲、錯誤重試）
# ============================================================

def fetch_rss_with_retry(url, max_retries=3):
    """抓取 RSS，失敗時退避重試"""
    headers = {"User-Agent": USER_AGENT}
    backoff_seconds = [60, 300, 900]  # 1分鐘、5分鐘、15分鐘

    for attempt in range(max_retries):
        try:
            # 隨機延遲，避免行為過於機械化
            time.sleep(random.uniform(1, 5))
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return feedparser.parse(resp.content)
            elif resp.status_code in (429, 503):
                print(f"[警告] {url} 回應 {resp.status_code}，將重試")
            else:
                print(f"[警告] {url} 回應狀態碼 {resp.status_code}")
        except requests.RequestException as e:
            print(f"[警告] 抓取 {url} 失敗: {e}")

        if attempt < max_retries - 1:
            wait = backoff_seconds[attempt]
            print(f"等待 {wait} 秒後重試...")
            time.sleep(wait)

    print(f"[錯誤] {url} 抓取失敗，已達最大重試次數，放棄本次抓取")
    return None


def contains_any(text, keywords):
    return any(kw.lower() in text.lower() for kw in keywords)


def fetch_all_sources(conn):
    """抓取三個來源，分類存入資料庫"""

    # 1. 中央社「產經證券」→ 同時用於「台股新聞」列表 與 「總經」候選池
    feed = fetch_rss_with_retry(CNA_FINANCE_RSS)
    if feed:
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            desc = re.sub("<[^<]+?>", "", entry.get("description", ""))
            pub_date = entry.get("published", "")

            if contains_any(title, EXCLUDE_KEYWORDS):
                continue

            # 台股新聞列表：全部收錄
            save_article(conn, title, link, "中央社", "taiwan_stock", pub_date, desc)

            # 總經新聞列表：用關鍵字篩選出總經相關內容
            if contains_any(title + desc, MACRO_KEYWORDS):
                save_article(conn, title, link, "中央社", "macro", pub_date, desc)

    # 2. 經濟日報「國際」→ 用於「國際股市」列表（關鍵字篩選）與「總經」候選池
    feed = fetch_rss_with_retry(UDN_INTL_RSS)
    if feed:
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            desc = re.sub("<[^<]+?>", "", entry.get("description", ""))
            pub_date = entry.get("published", "")

            if contains_any(title, EXCLUDE_KEYWORDS):
                continue

            # 國際股市列表：只收會牽動台股的國際大事
            if contains_any(title + desc, INTL_KEYWORDS):
                save_article(conn, title, link, "經濟日報", "intl_stock", pub_date, desc)

            # 總經新聞列表
            if contains_any(title + desc, MACRO_KEYWORDS):
                save_article(conn, title, link, "經濟日報", "macro", pub_date, desc)

    # 3. 經濟日報「理財」→ 用ETF關鍵字篩選出「ETF」列表
    feed = fetch_rss_with_retry(UDN_LICAI_RSS)
    if feed:
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            desc = re.sub("<[^<]+?>", "", entry.get("description", ""))
            pub_date = entry.get("published", "")

            if contains_any(title + desc, ETF_KEYWORDS):
                save_article(conn, title, link, "經濟日報", "etf", pub_date, desc)

    conn.commit()


# ============================================================
# 產生網頁
# ============================================================

def get_recent_articles(conn, category, min_count=ARTICLES_MIN, max_count=ARTICLES_MAX,
                         fallback_days=FALLBACK_DAYS):
    """
    依「新聞發布時間」篩選，優先取24小時內的新聞；
    如果24小時內不夠 min_count 篇，才放寬到 fallback_days 天內。
    時間不明的新聞（RSS格式異常）一律排在最後面，優先度最低。
    """
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    def query(cutoff_ts):
        return conn.execute(
            """SELECT title, link, source, pub_date, description, pub_date_ts
               FROM articles
               WHERE category = ?
                 AND (pub_date_ts IS NULL OR pub_date_ts >= ?)
               ORDER BY (pub_date_ts IS NULL), pub_date_ts DESC
               LIMIT ?""",
            (category, cutoff_ts, max_count),
        ).fetchall()

    cutoff_24h = now_ts - 24 * 3600
    rows = query(cutoff_24h)

    if len(rows) < min_count:
        cutoff_fallback = now_ts - fallback_days * 24 * 3600
        rows = query(cutoff_fallback)

    return rows


def truncate(text, max_len=200):
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def render_article_list(rows):
    if not rows:
        return '<p class="empty-state">目前沒有新聞，稍後會自動更新。</p>'
    items = []
    for title, link, source, pub_date, description, _pub_date_ts in rows:
        desc_html = ""
        if description and description.strip() and description.strip() != title.strip():
            desc_html = f'<p class="news-desc">{escape(truncate(description))}</p>'
        items.append(f"""
        <li class="news-item">
            <a href="{escape(link)}" target="_blank" rel="noopener">{escape(title)}</a>
            {desc_html}
            <span class="news-meta">{escape(source)} · {escape(pub_date)}</span>
        </li>""")
    return f'<ul class="news-list">{"".join(items)}</ul>'


def render_macro_section(rows):
    news_html = render_article_list(rows)
    return f"""
    <section class="card">
        <div class="section-head">
            <h2>總經新聞</h2>
        </div>
        {news_html}
    </section>"""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>台灣財經儀表板</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@500;700&family=Noto+Sans+TC:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #F7F6F2;
    --card-bg: #FFFFFF;
    --ink: #1C1C1A;
    --muted: #6B6B66;
    --accent: #1E3A5F;
    --accent-warm: #B23A2E;
    --border: #E4E1D8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: 'Noto Sans TC', -apple-system, sans-serif;
    line-height: 1.7;
  }}
  .page {{
    max-width: 720px;
    margin: 0 auto;
    padding: 32px 20px 80px;
  }}
  header.site-head {{
    margin-bottom: 32px;
  }}
  header.site-head h1 {{
    font-family: 'Noto Serif TC', serif;
    font-size: 28px;
    font-weight: 700;
    margin: 0 0 6px;
    color: var(--accent);
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .radar-icon {{
    width: 30px;
    height: 30px;
    flex-shrink: 0;
  }}
  header.site-head p {{
    margin: 0;
    color: var(--muted);
    font-size: 14px;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 24px;
    margin-bottom: 24px;
  }}
  .section-head {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 14px;
    border-bottom: 2px solid var(--accent);
    padding-bottom: 10px;
  }}
  .section-head h2 {{
    font-family: 'Noto Serif TC', serif;
    font-size: 20px;
    margin: 0;
  }}
  .updated-badge {{
    font-size: 12px;
    color: var(--muted);
  }}
  .news-list {{
    list-style: none;
    padding: 0;
    margin: 0;
  }}
  .news-item {{
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }}
  .news-item:last-child {{
    border-bottom: none;
  }}
  .news-item a {{
    color: var(--ink);
    text-decoration: none;
    font-size: 15px;
    display: block;
    margin-bottom: 4px;
    font-weight: 500;
  }}
  .news-item a:hover {{
    color: var(--accent);
  }}
  .news-desc {{
    font-size: 14px;
    color: var(--ink);
    opacity: 0.75;
    margin: 0 0 6px;
    line-height: 1.7;
  }}
  .news-meta {{
    font-size: 12px;
    color: var(--muted);
  }}
  .empty-state {{
    color: var(--muted);
    font-size: 14px;
  }}
  .site-footer {{
    text-align: center;
    margin-top: 32px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
  }}
  .site-footer p {{
    margin-top: 4px;
    margin-bottom: 4px;
  }}
  .media-note {{
    max-width: 420px;
    margin-left: auto;
    margin-right: auto;
    margin-bottom: 12px !important;
    line-height: 1.7;
    text-align: center;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #15171A;
      --card-bg: #1E2024;
      --ink: #EDEDE8;
      --muted: #9A9A93;
      --border: #33352E;
    }}
  }}
</style>
</head>
<body>
<div class="page">
  <header class="site-head">
    <h1>
      <svg class="radar-icon" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <defs>
          <linearGradient id="goldGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#F5E7A1"/>
            <stop offset="45%" stop-color="#D4AF37"/>
            <stop offset="100%" stop-color="#9C7A1E"/>
          </linearGradient>
          <radialGradient id="goldShine" cx="35%" cy="30%" r="60%">
            <stop offset="0%" stop-color="#FFFDF2" stop-opacity="0.9"/>
            <stop offset="60%" stop-color="#FFFDF2" stop-opacity="0.15"/>
            <stop offset="100%" stop-color="#FFFDF2" stop-opacity="0"/>
          </radialGradient>
        </defs>
        <circle cx="20" cy="20" r="17" fill="none" stroke="url(#goldGrad)" stroke-width="2"/>
        <circle cx="20" cy="20" r="11" fill="none" stroke="url(#goldGrad)" stroke-width="1.6" opacity="0.85"/>
        <circle cx="20" cy="20" r="5" fill="none" stroke="url(#goldGrad)" stroke-width="1.3" opacity="0.7"/>
        <path d="M 20 20 L 20 3 A 17 17 0 0 1 34.7 11.5 Z" fill="url(#goldGrad)" opacity="0.55"/>
        <circle cx="20" cy="20" r="2.2" fill="url(#goldGrad)"/>
        <circle cx="20" cy="20" r="17" fill="url(#goldShine)"/>
      </svg>
      台灣財經儀表板
    </h1>
    <p>總經 · 台股與國際股市 · 國內ETF　最後產生時間：{build_time}</p>
  </header>

  {macro_section}

  <section class="card">
    <div class="section-head">
      <h2>國內外股市</h2>
    </div>
    <h3 style="font-size:14px;color:var(--muted);margin:0 0 8px;">台股</h3>
    {taiwan_stock_list}
    <h3 style="font-size:14px;color:var(--muted);margin:16px 0 8px;">國際大事（牽動台股）</h3>
    {intl_stock_list}
  </section>

  <section class="card">
    <div class="section-head">
      <h2>國內ETF</h2>
    </div>
    {etf_list}
  </section>

  <footer class="site-footer">
    <p class="media-note">
      本站新聞內容僅擷取自各媒體公開提供之RSS服務（標題、前言、原文連結），
      不轉載全文、不另作商業利用，並依各媒體RSS服務條款合理頻率抓取。
      完整內容請點擊各則新聞連結至原始網站閱讀。
    </p>
    <p>網頁製作：股匠理財部落</p>
  </footer>
</div>
</body>
</html>
"""


def build_html(conn):
    macro_rows = get_recent_articles(conn, "macro")
    macro_section = render_macro_section(macro_rows)

    taiwan_stock_list = render_article_list(get_recent_articles(conn, "taiwan_stock"))
    intl_stock_list = render_article_list(get_recent_articles(conn, "intl_stock"))
    etf_list = render_article_list(get_recent_articles(conn, "etf"))

    build_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    html = HTML_TEMPLATE.format(
        build_time=build_time,
        macro_section=macro_section,
        taiwan_stock_list=taiwan_stock_list,
        intl_stock_list=intl_stock_list,
        etf_list=etf_list,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已產生網頁: {OUTPUT_HTML}")


# ============================================================
# 主流程
# ============================================================

def main():
    conn = init_db()

    print("步驟 1/2：抓取 RSS 新聞...")
    fetch_all_sources(conn)

    print("步驟 2/2：產生網頁...")
    build_html(conn)

    conn.close()
    print("完成！")


if __name__ == "__main__":
    main()
