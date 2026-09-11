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

# ------------------------------------------------------------
# 指數區塊設定
# ------------------------------------------------------------
# 台股兩指數：官方來源，累積約半年(130個交易日)歷史供走勢圖使用
TAIEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={date}"
TW_INDEX_HISTORY_DAYS = 130      # 台股指數保留的交易日數量（約半年）
TW_INDEX_BACKFILL_MONTHS = 6     # 第一次執行時，回補過去幾個月的歷史

# 美股三指數：透過 yfinance 套件抓取（比直接呼叫Stooq/Yahoo API更穩定，
# 該套件內建處理了Yahoo Finance的驗證機制），只保留最新兩筆用來算漲跌
US_INDICES = [
    ("SOX", "^SOX", "費城半導體"),
    ("SPX", "^GSPC", "S&P500"),
    ("IXIC", "^IXIC", "那斯達克"),
]

# 台股指數的合理數值範圍（防呆用：如果抓到的數字超出這個範圍，
# 代表欄位判讀錯誤，會直接捨棄，不讓錯誤資料混入資料庫）
INDEX_SANITY_RANGE = {
    "TAIEX": (10000, 150000),
}
INDEX_DISPLAY_NAMES = {
    "TAIEX": "加權指數",
    "SOX": "費城半導體",
    "SPX": "S&P500",
    "IXIC": "那斯達克",
}

# ------------------------------------------------------------
# 產業類股熱力圖設定
# ------------------------------------------------------------
# 已實際連線驗證過此端點格式，欄位為清楚的中文名稱（指數/收盤指數/漲跌/漲跌點數/漲跌百分比）
SECTOR_INDEX_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
# 排除掉已被更細分類取代的舊版彙總分類，避免跟細分類重複顯示
EXCLUDED_SECTOR_NAMES = {"水泥窯製類指數", "塑膠化工類指數", "機電類指數", "化學生技醫療類指數"}


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_code TEXT NOT NULL,
            date TEXT NOT NULL,
            close_value REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE(index_code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_heatmap (
            sector_name TEXT PRIMARY KEY,
            pct_change REAL NOT NULL,
            date TEXT NOT NULL,
            fetched_at TEXT NOT NULL
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
# 指數抓取（加權指數、櫃買指數、美股三指數）
# ============================================================

def save_index_point(conn, index_code, date_str, close_value):
    """存一筆指數收盤值。同一天重複存入會直接覆蓋舊值——
    這是刻意設計，讓修正後的抓取邏輯能夠覆蓋掉先前可能寫入的錯誤資料，
    也讓證交所/櫃買中心如果事後修正數字時，我們也能跟著更新。"""
    try:
        conn.execute(
            """INSERT OR REPLACE INTO index_history (index_code, date, close_value, fetched_at)
               VALUES (?, ?, ?, ?)""",
            (index_code, date_str, close_value, datetime.datetime.now().isoformat()),
        )
    except sqlite3.Error as e:
        print(f"[警告] 存入指數資料失敗: {e}")


def index_history_count(conn, index_code):
    row = conn.execute(
        "SELECT COUNT(*) FROM index_history WHERE index_code = ?", (index_code,)
    ).fetchone()
    return row[0] if row else 0


def trim_index_history(conn, index_code, keep_days):
    """只保留最近 keep_days 筆，避免資料庫無限長大"""
    conn.execute(
        """DELETE FROM index_history WHERE index_code = ? AND date NOT IN (
               SELECT date FROM index_history WHERE index_code = ?
               ORDER BY date DESC LIMIT ?
           )""",
        (index_code, index_code, keep_days),
    )


def fetch_taiex_month(year_month):
    """抓證交所加權指數，year_month 格式 YYYYMM，回傳 [(date, close), ...]"""
    date_param = f"{year_month}01"
    url = TAIEX_URL.format(date=date_param)
    feed_data = fetch_json_with_retry(url)
    if not feed_data or "data" not in feed_data:
        return []
    results = []
    for row in feed_data["data"]:
        try:
            # 欄位：日期(民國), 成交股數, 成交金額, 成交筆數, 發行量加權股價指數, 漲跌點數
            roc_date = row[0].replace(",", "")
            y, m, d = roc_date.split("/")
            date_str = f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
            close_value = float(row[4].replace(",", ""))
            if is_plausible_index_value("TAIEX", close_value):
                results.append((date_str, close_value))
            else:
                print(f"[警告] 加權指數 {date_str} 數值 {close_value} 超出合理範圍，該筆跳過")
        except (ValueError, IndexError, AttributeError):
            continue
    return results


def is_plausible_index_value(index_code, value):
    """防呆檢查：數值是否落在合理範圍內，避免欄位判讀錯誤污染資料庫"""
    bounds = INDEX_SANITY_RANGE.get(index_code)
    if not bounds:
        return True
    return bounds[0] <= value <= bounds[1]


def fetch_json_with_retry(url, max_retries=3):
    headers = {"User-Agent": USER_AGENT}
    backoff_seconds = [60, 300, 900]
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(1, 5))
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            print(f"[警告] {url} 回應狀態碼 {resp.status_code}")
        except (requests.RequestException, ValueError) as e:
            print(f"[警告] 抓取或解析 {url} 失敗: {e}")
        if attempt < max_retries - 1:
            time.sleep(backoff_seconds[attempt])
    print(f"[錯誤] {url} 抓取失敗，已達最大重試次數")
    return None


def backfill_tw_index_if_needed(conn, index_code, fetch_month_fn):
    """如果歷史資料不足100筆（代表還沒回補過），一次性回補過去幾個月"""
    if index_history_count(conn, index_code) >= 100:
        return
    print(f"[資訊] {index_code} 歷史資料不足，開始一次性回補過去 {TW_INDEX_BACKFILL_MONTHS} 個月")
    today = datetime.date.today()
    for i in range(TW_INDEX_BACKFILL_MONTHS):
        target_month = today.month - i
        target_year = today.year
        while target_month <= 0:
            target_month += 12
            target_year -= 1
        year_month = f"{target_year}{target_month:02d}"
        points = fetch_month_fn(year_month)
        for date_str, close_value in points:
            save_index_point(conn, index_code, date_str, close_value)
        conn.commit()


def fetch_us_index(symbol):
    """透過yfinance抓最近兩個交易日的收盤值，回傳 [(date, close), ...]（最多2筆）。
    一次抓兩天是為了讓「第一次成功抓取」當下就能算出漲跌，不用等到隔天。
    yfinance是社群維護的成熟套件，內建處理了Yahoo Finance的存取限制，
    比直接發request穩定。若失敗會安全地回傳空清單，畫面上會沿用資料庫裡
    最後一次成功抓到的數值並標示日期。"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist.empty:
            return []
        recent = hist.tail(2)
        results = []
        for idx, row in recent.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            close_value = float(row["Close"])
            results.append((date_str, close_value))
        return results
    except Exception as e:
        print(f"[警告] 抓取美股指數 {symbol} 失敗: {e}")
        return []


def fetch_tw_indices(conn):
    """14:30時段執行：抓加權指數今天的收盤值，並確保歷史資料已回補"""
    backfill_tw_index_if_needed(conn, "TAIEX", fetch_taiex_month)

    this_month = datetime.date.today().strftime("%Y%m")
    points = fetch_taiex_month(this_month)
    for date_str, close_value in points:
        save_index_point(conn, "TAIEX", date_str, close_value)
    trim_index_history(conn, "TAIEX", TW_INDEX_HISTORY_DAYS)
    conn.commit()


def fetch_us_indices_data(conn):
    """07:30時段執行：抓美股三指數最近兩個交易日的收盤值"""
    for code, symbol, _name in US_INDICES:
        points = fetch_us_index(symbol)
        for date_str, close_value in points:
            save_index_point(conn, code, date_str, close_value)
        if points:
            trim_index_history(conn, code, 2)  # 只需要留最新兩筆算漲跌
    conn.commit()


def fetch_sector_heatmap(conn):
    """14:30時段執行：抓產業類股當日漲跌幅。
    已實際連線驗證過此端點格式正確，欄位為清楚的中文名稱，
    不需要像之前那樣用範圍檢測猜欄位。"""
    data = fetch_json_with_retry(SECTOR_INDEX_URL)
    if not data:
        print("[警告] 產業類股熱力圖資料抓取失敗，本次跳過")
        return

    today = datetime.date.today().isoformat()
    count = 0
    for item in data:
        name = item.get("指數", "")
        if not name.endswith("類指數") or name in EXCLUDED_SECTOR_NAMES:
            continue
        try:
            pct_str = item.get("漲跌百分比", "").replace(",", "")
            sign = -1 if item.get("漲跌") == "-" else 1
            pct = sign * abs(float(pct_str))
        except (ValueError, TypeError):
            continue

        try:
            conn.execute(
                """INSERT OR REPLACE INTO sector_heatmap
                   (sector_name, pct_change, date, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (name, pct, today, datetime.datetime.now().isoformat()),
            )
            count += 1
        except sqlite3.Error as e:
            print(f"[警告] 存入產業熱力圖資料失敗: {e}")

    conn.commit()
    print(f"[資訊] 產業類股熱力圖抓取到 {count} 個分類")


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
  .chart-grid {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    margin-bottom: 16px;
  }}
  .chart-card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px;
  }}
  .chart-card h3 {{
    font-size: 14px;
    margin: 0 0 8px;
    color: var(--ink);
  }}
  .sparkline {{
    width: 100%;
    height: auto;
    display: block;
  }}
  .sparkline .axis-label {{
    font-size: 9px;
    fill: var(--muted);
    font-family: 'Noto Sans TC', sans-serif;
  }}
  .value-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
  }}
  .value-card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 10px 8px;
    text-align: center;
  }}
  .value-card h4 {{
    font-size: 12px;
    margin: 0 0 6px;
    color: var(--muted);
    font-weight: 500;
  }}
  .value-number {{
    font-size: 16px;
    font-weight: 700;
    margin: 0;
  }}
  .value-change {{
    font-size: 12px;
    margin: 4px 0 0;
    font-weight: 600;
  }}
  .value-date {{
    font-size: 10px;
    color: var(--muted);
    margin: 4px 0 0;
  }}
  .heatmap-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
  }}
  .heat-block {{
    border-radius: 4px;
    padding: 10px 8px;
    text-align: center;
    min-height: 64px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }}
  .heat-name {{
    font-size: 12px;
    font-weight: 500;
    margin: 0 0 4px;
    line-height: 1.3;
  }}
  .heat-pct {{
    font-size: 14px;
    font-weight: 700;
    margin: 0;
  }}
  .legend {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    margin-top: 16px;
    font-size: 11px;
    color: var(--muted);
  }}
  .legend-bar {{
    width: 160px;
    height: 10px;
    border-radius: 5px;
    background: linear-gradient(to right, #1E7A4C, #F7F6F2, #B23A2E);
  }}
  @media (max-width: 640px) {{
    .chart-grid {{
      grid-template-columns: 1fr;
    }}
    .value-grid {{
      grid-template-columns: repeat(2, 1fr);
    }}
    .heatmap-grid {{
      grid-template-columns: repeat(3, 1fr);
    }}
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
    <p>總經 · 台股與國際股市 · 國內ETF　最後產生時間：{build_time}（台灣時間）</p>
  </header>

  {index_section}

  {heatmap_section}

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


def get_index_series(conn, index_code, limit=TW_INDEX_HISTORY_DAYS):
    rows = conn.execute(
        """SELECT date, close_value FROM index_history
           WHERE index_code = ? ORDER BY date ASC LIMIT ?""",
        (index_code, limit),
    ).fetchall()
    return rows


def get_latest_change(conn, index_code):
    """回傳 (最新收盤值, 漲跌點, 漲跌幅%, 最新日期) 或全部None（沒有資料時）"""
    rows = conn.execute(
        """SELECT date, close_value FROM index_history
           WHERE index_code = ? ORDER BY date DESC LIMIT 2""",
        (index_code,),
    ).fetchall()
    if not rows:
        return None, None, None, None
    latest_date, latest_close = rows[0]
    if len(rows) < 2:
        return latest_close, None, None, latest_date
    _, prev_close = rows[1]
    change_value = latest_close - prev_close
    change_pct = (change_value / prev_close * 100) if prev_close else None
    return latest_close, change_value, change_pct, latest_date


def render_sparkline_svg(dates, values, width=300, height=90):
    """畫一條極簡折線圖，並在下緣標示月份作為時間軸參考。
    不畫Y軸數值刻度，維持精簡、避免資訊壓迫感。"""
    if len(values) < 2:
        return '<p class="empty-state">資料不足，尚無法繪製走勢圖</p>'

    label_area = 18  # 下方留給月份文字的高度
    chart_height = height - label_area
    min_v, max_v = min(values), max(values)
    range_v = (max_v - min_v) or 1
    padding = 6
    usable_w = width - padding * 2
    usable_h = chart_height - padding * 2

    def x_at(i):
        return padding + (i / (len(values) - 1)) * usable_w

    line_color = "#B23A2E"

    points = []
    for i, v in enumerate(values):
        x = x_at(i)
        y = padding + usable_h - ((v - min_v) / range_v) * usable_h
        points.append(f"{x:.1f},{y:.1f}")
    polyline_points = " ".join(points)

    # 找出每個月第一次出現的位置，標上月份文字（例如「9月」）
    month_labels = []
    last_month = None
    for i, d in enumerate(dates):
        month = d[5:7]  # 從 YYYY-MM-DD 取出月份
        if month != last_month:
            month_labels.append((x_at(i), int(month)))
            last_month = month

    labels_svg = "".join(
        f'<text x="{x:.1f}" y="{height - 4}" text-anchor="middle" class="axis-label">{m}月</text>'
        for x, m in month_labels
    )

    return f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="sparkline">
        <polyline points="{polyline_points}" fill="none" stroke="{line_color}" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round"/>
        {labels_svg}
    </svg>"""


def render_sparkline_card(conn, index_code):
    name = INDEX_DISPLAY_NAMES.get(index_code, index_code)
    series = get_index_series(conn, index_code)
    if not series:
        return f"""<div class="chart-card">
            <h3>{name}</h3>
            <p class="empty-state">暫無資料</p>
        </div>"""

    dates = [r[0] for r in series]
    values = [r[1] for r in series]
    svg = render_sparkline_svg(dates, values)
    return f"""<div class="chart-card">
        <h3>{name}</h3>
        {svg}
    </div>"""


def render_value_card(conn, index_code):
    name = INDEX_DISPLAY_NAMES.get(index_code, index_code)
    latest, change_value, change_pct, latest_date = get_latest_change(conn, index_code)

    if latest is None:
        return f"""<div class="value-card">
            <h4>{name}</h4>
            <p class="empty-state">暫無資料</p>
        </div>"""

    if change_value is None:
        change_html = '<p class="value-change">－</p>'
    else:
        # 紅漲綠跌（台灣習慣）
        color = "#B23A2E" if change_value >= 0 else "#1E7A4C"
        arrow = "▲" if change_value >= 0 else "▼"
        change_html = f"""<p class="value-change" style="color:{color};">
            {arrow} {abs(change_value):,.2f}（{abs(change_pct):.2f}%）
        </p>"""

    return f"""<div class="value-card">
        <h4>{name}</h4>
        <p class="value-number">{latest:,.2f}</p>
        {change_html}
        <p class="value-date">{escape(latest_date)}</p>
    </div>"""


def render_index_section(conn):
    chart_cards = render_sparkline_card(conn, "TAIEX")
    value_cards = "".join(
        render_value_card(conn, code) for code in ["TAIEX", "SOX", "SPX", "IXIC"]
    )
    return f"""
    <section class="card">
        <div class="section-head">
            <h2>大盤指數</h2>
        </div>
        <div class="chart-grid">{chart_cards}</div>
        <div class="value-grid">{value_cards}</div>
    </section>"""


def pct_to_heat_color(pct):
    """紅漲綠跌，幅度越大顏色越深（台灣習慣）"""
    clamped = max(-3, min(3, pct))
    ratio = abs(clamped) / 3
    if pct >= 0:
        r = 247 - round((247 - 178) * ratio)
        g = 246 - round((246 - 58) * ratio)
        b = 242 - round((242 - 46) * ratio)
    else:
        r = 247 - round((247 - 30) * ratio)
        g = 246 - round((246 - 122) * ratio)
        b = 242 - round((242 - 76) * ratio)
    return f"rgb({r},{g},{b})"


def render_sector_heatmap_section(conn):
    rows = conn.execute(
        """SELECT sector_name, pct_change, date FROM sector_heatmap
           ORDER BY pct_change DESC"""
    ).fetchall()

    if not rows:
        return """
    <section class="card">
        <div class="section-head"><h2>產業類股熱力圖</h2></div>
        <p class="empty-state">目前沒有資料，稍後會自動更新。</p>
    </section>"""

    latest_date = rows[0][2]
    blocks = []
    for name, pct, _date in rows:
        display_name = name.replace("類指數", "")
        text_color = "#FFFFFF" if abs(pct) > 1.5 else "var(--ink)"
        sign = "+" if pct >= 0 else ""
        blocks.append(f"""
        <div class="heat-block" style="background:{pct_to_heat_color(pct)};">
            <p class="heat-name" style="color:{text_color};">{escape(display_name)}</p>
            <p class="heat-pct" style="color:{text_color};">{sign}{pct:.2f}%</p>
        </div>""")

    return f"""
    <section class="card">
        <div class="section-head">
            <h2>產業類股熱力圖</h2>
            <span class="updated-badge">{escape(latest_date)} 收盤</span>
        </div>
        <div class="heatmap-grid">{"".join(blocks)}</div>
        <div class="legend">
            <span>下跌</span>
            <div class="legend-bar"></div>
            <span>上漲</span>
        </div>
    </section>"""


def build_html(conn):
    index_section = render_index_section(conn)
    heatmap_section = render_sector_heatmap_section(conn)

    macro_rows = get_recent_articles(conn, "macro")
    macro_section = render_macro_section(macro_rows)

    taiwan_stock_list = render_article_list(get_recent_articles(conn, "taiwan_stock"))
    intl_stock_list = render_article_list(get_recent_articles(conn, "intl_stock"))
    etf_list = render_article_list(get_recent_articles(conn, "etf"))

    taiwan_tz = datetime.timezone(datetime.timedelta(hours=8))
    build_time = datetime.datetime.now(taiwan_tz).strftime("%Y-%m-%d %H:%M")

    html = HTML_TEMPLATE.format(
        build_time=build_time,
        index_section=index_section,
        heatmap_section=heatmap_section,
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
    time_slot = os.environ.get("TIME_SLOT", "manual")
    conn = init_db()

    print(f"目前時段: {time_slot}")
    print("步驟 1/3：抓取 RSS 新聞...")
    fetch_all_sources(conn)

    print("步驟 2/3：抓取指數資料...")
    if time_slot in ("14:30", "manual"):
        try:
            fetch_tw_indices(conn)
        except Exception as e:
            print(f"[警告] 台股指數抓取過程發生例外，已略過本次: {e}")
        try:
            fetch_sector_heatmap(conn)
        except Exception as e:
            print(f"[警告] 產業熱力圖抓取過程發生例外，已略過本次: {e}")
    if time_slot in ("07:30", "manual"):
        try:
            fetch_us_indices_data(conn)
        except Exception as e:
            print(f"[警告] 美股指數抓取過程發生例外，已略過本次: {e}")

    print("步驟 3/3：產生網頁...")
    build_html(conn)

    conn.close()
    print("完成！")


if __name__ == "__main__":
    main()
