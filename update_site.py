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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_metric (
            metric TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (metric, date)
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
    """21:00時段執行：抓加權指數今天的收盤值，並確保歷史資料已回補。
    改成21:00而非收盤後的14:30，是因為實測發現證交所資料更新常有延遲，
    14:30當下常常還抓不到當天的資料，21:00較能確保資料已經備妥。"""
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
    """07:30時段執行（隔天早上抓前一交易日資料）：抓產業類股當日漲跌幅。
    原訂14:30、後改21:00皆發現證交所資料尚未更新完成，
    改到隔天07:30（與美股指數同時段），有近10小時緩衝時間，較能確保資料已備妥。
    已實際連線驗證過此端點格式正確，欄位為清楚的中文名稱，
    不需要像之前那樣用範圍檢測猜欄位。
    注意：日期一律採用API回傳資料本身附帶的「日期」欄位（民國年格式），
    不可用程式執行當下的系統日期，避免收盤前執行時被誤標成當天。"""
    data = fetch_json_with_retry(SECTOR_INDEX_URL)
    if not data:
        print("[警告] 產業類股熱力圖資料抓取失敗，本次跳過")
        return

    count = 0
    data_date = None
    for item in data:
        name = item.get("指數", "")
        if not name.endswith("類指數") or name in EXCLUDED_SECTOR_NAMES:
            continue

        # 解析資料本身的日期欄位（民國年格式，例如 "1150910" → 2026-09-10）
        roc_date_str = item.get("日期", "")
        try:
            roc_year = int(roc_date_str[:3])
            month = int(roc_date_str[3:5])
            day = int(roc_date_str[5:7])
            data_date = f"{roc_year + 1911}-{month:02d}-{day:02d}"
        except (ValueError, IndexError):
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
                (name, pct, data_date, datetime.datetime.now().isoformat()),
            )
            count += 1
        except sqlite3.Error as e:
            print(f"[警告] 存入產業熱力圖資料失敗: {e}")

    conn.commit()
    print(f"[資訊] 產業類股熱力圖抓取到 {count} 個分類，資料日期：{data_date}")


# ============================================================
# 大盤指標抓取（漲跌家數、成交金額、融資、法人買賣超、本益比）
# ============================================================
# 設計原則：
# 1. 每個指標獨立抓取、獨立存檔，某一項失敗不影響其他項目。
# 2. 每個排程時段都會嘗試抓一次；已經是最新資料就跳過，抓不到就保留資料庫裡
#    最後一筆，網頁上每張卡片都會標示自己的資料日期。
# 3. 所有資料都來自證交所公開資料，不需要任何金鑰。
# 4. 融資維持率、大盤本益比，證交所沒有直接公布，是依公開資料自行估算。

TWSE_BREADTH_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=MS&response=json"
TWSE_MARGIN_SUMMARY_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=MS&response=json"
TWSE_MARGIN_STOCK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
TWSE_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_PE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={date}&selectType=ALL&response=json"
TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_INST_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={date}&response=json"

METRIC_KEEP_DAYS = 120          # 指標歷史保留天數（日曆天）
INST_MIN_HISTORY = 15           # 三大法人至少要有幾個交易日，才算完成回補（用來算連買連賣）
TZ_TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

_indicator_fail_streak = 0      # 連續失敗次數；連續3次失敗就判定連線有問題，本次不再繼續抓


def init_market_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_metric (
            metric TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (metric, date)
        )
    """)
    conn.commit()


def save_metric(conn, metric, date_str, value):
    conn.execute(
        "INSERT OR REPLACE INTO market_metric (metric, date, value, fetched_at) VALUES (?, ?, ?, ?)",
        (metric, date_str, float(value), datetime.datetime.now().isoformat()),
    )


def get_metric_series(conn, metric, limit=10):
    """回傳 [(date, value), ...]，最新的在最前面"""
    return conn.execute(
        "SELECT date, value FROM market_metric WHERE metric = ? ORDER BY date DESC LIMIT ?",
        (metric, limit),
    ).fetchall()


def get_metric_latest_date(conn, metric):
    row = conn.execute("SELECT MAX(date) FROM market_metric WHERE metric = ?", (metric,)).fetchone()
    return row[0] if row and row[0] else None


def get_metric_on(conn, metric, date_str):
    row = conn.execute(
        "SELECT value FROM market_metric WHERE metric = ? AND date = ?", (metric, date_str)
    ).fetchone()
    return row[0] if row else None


def count_metric_dates(conn, metric):
    row = conn.execute("SELECT COUNT(*) FROM market_metric WHERE metric = ?", (metric,)).fetchone()
    return row[0] if row else 0


def trim_metric_history(conn):
    cutoff = (datetime.date.today() - datetime.timedelta(days=METRIC_KEEP_DAYS)).isoformat()
    conn.execute("DELETE FROM market_metric WHERE date < ?", (cutoff,))


def to_number(raw):
    """把 '1,234.5' 之類的文字轉成數字；空白、'-'、'--' 回傳 None"""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if s in ("", "-", "--", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_slash_to_iso(raw):
    """'115/10/19' → '2026-10-19'"""
    try:
        y, m, d = str(raw).replace(",", "").strip().split("/")
        return f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return None


def roc7_to_iso(raw):
    """'1151019' → '2026-10-19'"""
    s = str(raw).strip()
    try:
        return f"{int(s[:3]) + 1911}-{int(s[3:5]):02d}-{int(s[5:7]):02d}"
    except (ValueError, IndexError):
        return None


def candidate_dates(days_back):
    """從今天（台灣日期）往回數 days_back 個日曆天內的平日，最新的在前面"""
    today = datetime.datetime.now(TZ_TAIPEI).date()
    result = []
    for i in range(days_back + 1):
        d = today - datetime.timedelta(days=i)
        if d.weekday() < 5:
            result.append(d)
    return result


def fetch_json_quick(url):
    """指標專用的輕量抓取：失敗只重試1次（間隔20秒），不像新聞那樣等15分鐘，
    避免證交所暫時連不上時整個排程拖太久。連續失敗3次就視為連線有問題。"""
    global _indicator_fail_streak
    if _indicator_fail_streak >= 3:
        return None
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(2):
        try:
            time.sleep(random.uniform(1, 5))
            resp = requests.get(url, headers=headers, timeout=25)
            if resp.status_code == 200:
                _indicator_fail_streak = 0
                return resp.json()
            print(f"[警告] {url} 回應狀態碼 {resp.status_code}")
        except (requests.RequestException, ValueError) as e:
            print(f"[警告] 抓取或解析 {url} 失敗: {e}")
        if attempt == 0:
            time.sleep(20)
    _indicator_fail_streak += 1
    return None


def twse_ok(data, d):
    """證交所 rwd 回應是否為「該日期有資料」。沒資料（假日、尚未公布）時 stat 不是 OK。"""
    if not isinstance(data, dict) or data.get("stat") != "OK":
        return False
    resp_date = str(data.get("date", "")).strip()
    if resp_date and resp_date != d.strftime("%Y%m%d"):
        return False
    return True


def parse_count_with_limit(raw):
    """'163(7)' → (163, 7)；'61' → (61, 0)"""
    m = re.match(r"\s*([\d,]+)\s*(?:\(\s*([\d,]+)\s*\))?", str(raw))
    if not m:
        return None, None
    n = int(m.group(1).replace(",", ""))
    lim = int(m.group(2).replace(",", "")) if m.group(2) else 0
    return n, lim


def fetch_breadth(conn, d):
    """上漲／下跌家數（採「股票」欄，不含權證、ETF 等其他證券）"""
    data = fetch_json_quick(TWSE_BREADTH_URL.format(date=d.strftime("%Y%m%d")))
    if not twse_ok(data, d):
        return False
    for table in data.get("tables", []):
        if "漲跌證券數" not in str(table.get("title", "")):
            continue
        fields = table.get("fields", [])
        col = fields.index("股票") if "股票" in fields else 2
        values = {}
        for row in table.get("data", []):
            label = str(row[0])
            n, lim = parse_count_with_limit(row[col])
            if n is None:
                continue
            if label.startswith("上漲"):
                values["adv"], values["adv_lim"] = n, lim
            elif label.startswith("下跌"):
                values["dec"], values["dec_lim"] = n, lim
            elif label.startswith("持平"):
                values["flat"] = n
        if "adv" in values and "dec" in values and values["adv"] + values["dec"] >= 300:
            iso = d.isoformat()
            for k, v in values.items():
                save_metric(conn, k, iso, v)
            conn.commit()
            print(f"[資訊] 漲跌家數 {iso}: 上漲{values['adv']} 下跌{values['dec']}")
            return True
        print("[警告] 漲跌家數數值不合理，已捨棄")
    return False


def fetch_volume(conn):
    """成交金額／成交股數：和加權指數同一支 FMTQIK，抓當月（資料不足時加抓上月）"""
    today = datetime.datetime.now(TZ_TAIPEI).date()
    months = [today.strftime("%Y%m")]
    if count_metric_dates(conn, "vol_amt") < 8:
        prev_last = today.replace(day=1) - datetime.timedelta(days=1)
        months.append(prev_last.strftime("%Y%m"))
    saved = 0
    for ym in months:
        data = fetch_json_quick(TAIEX_URL.format(date=f"{ym}01"))
        if not isinstance(data, dict) or "data" not in data:
            continue
        for row in data["data"]:
            try:
                iso = roc_slash_to_iso(row[0])
                shares = to_number(row[1])
                amount = to_number(row[2])
                if iso and amount and amount > 1e10:  # 成交金額至少百億，過濾明顯異常
                    save_metric(conn, "vol_amt", iso, amount)
                    if shares:
                        save_metric(conn, "vol_shares", iso, shares)
                    saved += 1
            except (IndexError, TypeError):
                continue
    conn.commit()
    print(f"[資訊] 成交金額更新 {saved} 筆")


def fetch_margin_summary(conn, d):
    """大盤融資餘額（張數與金額），含前日餘額以便算增減"""
    data = fetch_json_quick(TWSE_MARGIN_SUMMARY_URL.format(date=d.strftime("%Y%m%d")))
    if not twse_ok(data, d):
        return False
    tables = data.get("tables", [])
    if not tables or not tables[0].get("data"):
        return False
    table = tables[0]
    fields = table.get("fields", [])
    if "今日餘額" not in fields or "前日餘額" not in fields:
        print("[警告] 融資統計欄位與預期不同，已捨棄")
        return False
    i_today, i_prev = fields.index("今日餘額"), fields.index("前日餘額")
    values = {}
    for row in table["data"]:
        label = str(row[0])
        if label.startswith("融資(交易單位)"):
            values["margin_lots"], values["margin_lots_prev"] = to_number(row[i_today]), to_number(row[i_prev])
        elif label.startswith("融資金額"):
            values["margin_amt"], values["margin_amt_prev"] = to_number(row[i_today]), to_number(row[i_prev])
    if not all(values.get(k) for k in ("margin_lots", "margin_amt")):
        return False
    iso = d.isoformat()
    for k, v in values.items():
        if v is not None:
            save_metric(conn, k, iso, v)
    conn.commit()
    print(f"[資訊] 融資餘額 {iso}: {values['margin_amt'] / 1e5:,.0f} 億元")
    return True


def estimate_maintenance(conn, iso):
    """估算大盤融資維持率 = 融資股票市值 ÷ 融資金額。
    融資市值 = 各股融資餘額(張)×1000×收盤價 加總。
    安全檢查：個股融資張數加總必須和大盤統計吻合（誤差1%內），且收盤價日期必須是同一天，
    才能確定兩份資料是同一天的，否則不更新、保留舊值。"""
    stock_rows = fetch_json_quick(TWSE_MARGIN_STOCK_URL)
    price_rows = fetch_json_quick(TWSE_STOCK_DAY_ALL_URL)
    if not isinstance(stock_rows, list) or not isinstance(price_rows, list) or not stock_rows or not price_rows:
        return False
    price_date = roc7_to_iso(price_rows[0].get("Date", ""))
    if price_date != iso:
        print(f"[資訊] 融資維持率：收盤價日期 {price_date} 與融資日期 {iso} 不同，等下次再試")
        return False
    prices = {str(r.get("Code", "")).strip(): to_number(r.get("ClosingPrice")) for r in price_rows}
    total_lots, market_value = 0.0, 0.0
    for r in stock_rows:
        lots = to_number(r.get("融資今日餘額")) or 0
        total_lots += lots
        px = prices.get(str(r.get("股票代號", "")).strip())
        if lots and px:
            market_value += lots * 1000 * px
    summary_lots = get_metric_on(conn, "margin_lots", iso)
    summary_amt = get_metric_on(conn, "margin_amt", iso)
    if not summary_lots or not summary_amt:
        return False
    if abs(total_lots - summary_lots) / summary_lots > 0.01:
        print(f"[資訊] 融資維持率：個股加總 {total_lots:,.0f} 張與大盤 {summary_lots:,.0f} 張不吻合，等下次再試")
        return False
    ratio = market_value / (summary_amt * 1000) * 100
    if not (100 <= ratio <= 400):
        print(f"[警告] 融資維持率估算值 {ratio:.1f}% 超出合理範圍，已捨棄")
        return False
    save_metric(conn, "maint", iso, ratio)
    conn.commit()
    print(f"[資訊] 融資維持率(估算) {iso}: {ratio:.1f}%")
    return True


def load_issued_shares():
    """各上市公司已發行普通股數（用來算市值）"""
    data = fetch_json_quick(TWSE_COMPANY_URL)
    shares = {}
    if isinstance(data, list):
        for rec in data:
            code = str(rec.get("公司代號", "")).strip()
            n = to_number(rec.get("已發行普通股數或TDR原股發行股數"))
            if code and n:
                shares[code] = n
    return shares


def fetch_market_pe(conn, d):
    """估算大盤本益比（市值加權）= Σ市值 ÷ Σ(市值÷個股本益比)，
    只計入有正本益比的公司，虧損公司不納入，所以是估算值。"""
    data = fetch_json_quick(TWSE_PE_URL.format(date=d.strftime("%Y%m%d")))
    if not twse_ok(data, d) or not data.get("data"):
        return False
    fields = data.get("fields", [])
    try:
        i_code, i_close, i_pe = fields.index("證券代號"), fields.index("收盤價"), fields.index("本益比")
    except ValueError:
        print("[警告] 本益比欄位與預期不同，已捨棄")
        return False
    shares = load_issued_shares()
    if not shares:
        return False
    total_cap, total_earn, used = 0.0, 0.0, 0
    for row in data["data"]:
        code = str(row[i_code]).strip()
        close, pe = to_number(row[i_close]), to_number(row[i_pe])
        sh = shares.get(code)
        if not close or not pe or pe <= 0 or not sh:
            continue
        cap = close * sh
        total_cap += cap
        total_earn += cap / pe
        used += 1
    if used < 300 or total_earn <= 0:
        print(f"[警告] 本益比可用樣本只有 {used} 檔，已捨棄")
        return False
    market_pe = total_cap / total_earn
    if not (5 <= market_pe <= 80):
        print(f"[警告] 大盤本益比估算值 {market_pe:.1f} 超出合理範圍，已捨棄")
        return False
    save_metric(conn, "pe", d.isoformat(), market_pe)
    conn.commit()
    print(f"[資訊] 大盤本益比(估算) {d.isoformat()}: {market_pe:.2f}（樣本 {used} 檔）")
    return True


def fetch_institutional(conn, d):
    """三大法人買賣超（買賣差額，單位：元）。外資 = 外資及陸資＋外資自營商；
    自營商 = 自行買賣＋避險。"""
    data = fetch_json_quick(TWSE_INST_URL.format(date=d.strftime("%Y%m%d")))
    if not twse_ok(data, d) or not data.get("data"):
        return False
    foreign, trust, dealer = 0.0, 0.0, 0.0
    found = set()
    for row in data["data"]:
        name = str(row[0])
        net = to_number(row[3])
        if net is None:
            continue
        if name.startswith("外資"):
            foreign += net
            found.add("foreign")
        elif name.startswith("投信"):
            trust += net
            found.add("trust")
        elif name.startswith("自營商"):
            dealer += net
            found.add("dealer")
    if found != {"foreign", "trust", "dealer"}:
        print("[警告] 三大法人列名與預期不同，已捨棄")
        return False
    iso = d.isoformat()
    save_metric(conn, "inst_foreign", iso, foreign)
    save_metric(conn, "inst_trust", iso, trust)
    save_metric(conn, "inst_dealer", iso, dealer)
    conn.commit()
    print(f"[資訊] 三大法人 {iso}: 外資{foreign / 1e8:+,.1f}億 投信{trust / 1e8:+,.1f}億 自營{dealer / 1e8:+,.1f}億")
    return True


def fetch_market_indicators(conn):
    """每個排程都會呼叫。每項指標：資料庫裡已有最新日期就不重抓；
    抓不到（尚未公布、假日、連線失敗）就保留舊值，下個時段再試。"""
    init_market_table(conn)

    # 成交金額（含歷史，一次一個請求）
    try:
        fetch_volume(conn)
    except Exception as e:
        print(f"[警告] 成交金額抓取例外，已略過: {e}")

    # 漲跌家數、融資餘額、本益比：只要抓到最新一天即可
    singles = [
        ("上漲下跌家數", "adv", fetch_breadth),
        ("融資餘額", "margin_amt", fetch_margin_summary),
        ("大盤本益比", "pe", fetch_market_pe),
    ]
    for label, key, fn in singles:
        latest = get_metric_latest_date(conn, key)
        for d in candidate_dates(8):
            if _indicator_fail_streak >= 3:
                break
            if latest and d.isoformat() <= latest:
                break
            try:
                if fn(conn, d):
                    break
            except Exception as e:
                print(f"[警告] {label}抓取例外，已略過: {e}")
                break

    # 融資維持率：融資餘額有新日期、但維持率還沒跟上時才算
    try:
        margin_date = get_metric_latest_date(conn, "margin_amt")
        maint_date = get_metric_latest_date(conn, "maint")
        if margin_date and (not maint_date or maint_date < margin_date):
            estimate_maintenance(conn, margin_date)
    except Exception as e:
        print(f"[警告] 融資維持率估算例外，已略過: {e}")

    # 三大法人：需要連續多日資料才能算連買連賣，所以缺的日期都補
    try:
        backfill = count_metric_dates(conn, "inst_foreign") < INST_MIN_HISTORY
        stored = {r[0] for r in conn.execute(
            "SELECT date FROM market_metric WHERE metric = 'inst_foreign'")}
        latest = max(stored) if stored else None
        if backfill:
            print("[資訊] 三大法人歷史不足，開始一次性回補（用來計算連買連賣）")
        for d in candidate_dates(35 if backfill else 8):
            if _indicator_fail_streak >= 3:
                break
            iso = d.isoformat()
            if iso in stored or (not backfill and latest and iso <= latest):
                continue
            fetch_institutional(conn, d)
    except Exception as e:
        print(f"[警告] 三大法人抓取例外，已略過: {e}")

    trim_metric_history(conn)
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
  .ind-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }}
  .ind-card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px 10px;
    text-align: center;
  }}
  .ind-card.ind-wide {{
    grid-column: 1 / -1;
  }}
  .ind-card h4 {{
    font-size: 12px;
    margin: 0 0 6px;
    color: var(--muted);
    font-weight: 500;
  }}
  .ind-main {{
    font-size: 20px;
    font-weight: 700;
    margin: 0;
  }}
  .ind-unit {{
    font-size: 12px;
    font-weight: 500;
    color: var(--muted);
  }}
  .ind-slash {{
    color: var(--muted);
    font-weight: 400;
  }}
  .ind-sub {{
    font-size: 11px;
    color: var(--muted);
    margin: 6px 0 0;
    line-height: 1.6;
  }}
  .ind-est {{
    display: inline-block;
    margin-left: 4px;
    padding: 0 4px;
    font-size: 10px;
    border: 1px solid var(--muted);
    border-radius: 3px;
    color: var(--muted);
  }}
  .ind-foot {{
    font-size: 11px;
    color: var(--muted);
    margin: 14px 0 0;
    line-height: 1.7;
  }}
  .inst-list {{
    max-width: 420px;
    margin: 4px auto 0;
  }}
  .inst-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }}
  .inst-row:last-child {{
    border-bottom: none;
  }}
  .inst-name {{
    width: 56px;
    text-align: left;
  }}
  .inst-amount {{
    flex: 1;
    text-align: right;
    padding-right: 12px;
  }}
  .ind-streak {{
    min-width: 52px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--muted);
    border-radius: 10px;
    padding: 1px 8px;
    text-align: center;
  }}
  @media (max-width: 640px) {{
    .ind-grid {{
      grid-template-columns: repeat(2, 1fr);
    }}
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

  {indicator_section}

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


COLOR_UP = "#B23A2E"     # 紅：漲／買超／正乖離
COLOR_DOWN = "#1E7A4C"   # 綠：跌／賣超／負乖離


def signed_color(v):
    return COLOR_UP if v >= 0 else COLOR_DOWN


def calc_streak(series_desc):
    """series_desc：[(date, 買賣超), ...]，最新在前。回傳 (是否買超, 連續天數) 或 None"""
    if not series_desc or series_desc[0][1] == 0:
        return None
    is_buy = series_desc[0][1] > 0
    days = 0
    for _d, v in series_desc:
        if v != 0 and (v > 0) == is_buy:
            days += 1
        else:
            break
    return is_buy, days


def streak_html(series_desc):
    result = calc_streak(series_desc)
    if not result:
        return '<span class="ind-streak">－</span>'
    is_buy, days = result
    color = COLOR_UP if is_buy else COLOR_DOWN
    if days >= 2:
        text = f"連{days}買" if is_buy else f"連{days}賣"
    else:
        text = "轉買" if is_buy else "轉賣"
        if len(series_desc) < 2:
            text = "買超" if is_buy else "賣超"
    return f'<span class="ind-streak" style="color:{color};border-color:{color};">{text}</span>'


def change_html(delta, pct=None, unit=""):
    """▲紅／▼綠 的變動文字"""
    if delta is None:
        return ""
    color = signed_color(delta)
    arrow = "▲" if delta >= 0 else "▼"
    pct_text = f"（{abs(pct):.1f}%）" if pct is not None else ""
    return f'<span style="color:{color};">{arrow} {abs(delta):,.1f}{unit}{pct_text}</span>'


def ind_card(title, main, sub="", date_str=None, estimate=False, wide=False):
    tag = '<span class="ind-est">估算</span>' if estimate else ""
    date_html = f'<p class="value-date">{escape(date_str)}</p>' if date_str else ""
    sub_html = f'<p class="ind-sub">{sub}</p>' if sub else ""
    cls = "ind-card ind-wide" if wide else "ind-card"
    return f"""<div class="{cls}">
        <h4>{escape(title)}{tag}</h4>
        <p class="ind-main">{main}</p>
        {sub_html}
        {date_html}
    </div>"""


def ind_empty(title, estimate=False):
    return ind_card(title, '<span class="empty-state">暫無資料</span>', estimate=estimate)


def render_breadth_card(conn):
    adv = get_metric_series(conn, "adv", 1)
    dec = get_metric_series(conn, "dec", 1)
    if not adv or not dec or adv[0][0] != dec[0][0]:
        return ind_empty("上漲／下跌家數")
    date_str = adv[0][0]
    adv_lim = get_metric_on(conn, "adv_lim", date_str) or 0
    dec_lim = get_metric_on(conn, "dec_lim", date_str) or 0
    flat = get_metric_on(conn, "flat", date_str) or 0
    main = (f'<span style="color:{COLOR_UP};">{adv[0][1]:,.0f}</span>'
            f' <span class="ind-slash">/</span> '
            f'<span style="color:{COLOR_DOWN};">{dec[0][1]:,.0f}</span>')
    sub = f"漲停 {adv_lim:,.0f}・跌停 {dec_lim:,.0f}・持平 {flat:,.0f}"
    return ind_card("上漲／下跌家數", main, sub, date_str)


def render_volume_card(conn):
    series = get_metric_series(conn, "vol_amt", 5)
    if not series:
        return ind_empty("成交金額")
    date_str, latest = series[0]
    main = f"{latest / 1e8:,.0f} <span class=\"ind-unit\">億元</span>"
    parts = []
    if len(series) >= 2 and series[1][1]:
        prev = series[1][1]
        parts.append("較前日 " + change_html((latest - prev) / 1e8, (latest - prev) / prev * 100, "億"))
    if len(series) >= 5:
        avg5 = sum(v for _d, v in series[:5]) / 5
        parts.append(f"近5日均 {avg5 / 1e8:,.0f} 億")
    return ind_card("成交金額", main, "<br>".join(parts), date_str)


def render_margin_card(conn):
    series = get_metric_series(conn, "margin_amt", 1)
    if not series:
        return ind_empty("融資餘額")
    date_str, amt = series[0]
    lots = get_metric_on(conn, "margin_lots", date_str)
    prev_amt = get_metric_on(conn, "margin_amt_prev", date_str)
    main = f"{amt / 1e5:,.0f} <span class=\"ind-unit\">億元</span>"
    parts = []
    if prev_amt:
        diff = (amt - prev_amt) / 1e5
        parts.append("較前日 " + change_html(diff, (amt - prev_amt) / prev_amt * 100, "億"))
    if lots:
        parts.append(f"{lots:,.0f} 張")
    return ind_card("融資餘額", main, "<br>".join(parts), date_str)


def render_maint_card(conn):
    series = get_metric_series(conn, "maint", 2)
    if not series:
        return ind_empty("融資維持率", estimate=True)
    date_str, ratio = series[0]
    sub = ""
    if len(series) >= 2:
        sub = "較前次 " + change_html(ratio - series[1][1], None, "%")
    return ind_card("融資維持率", f"{ratio:.1f}<span class=\"ind-unit\">%</span>", sub, date_str, estimate=True)


def render_pe_card(conn):
    series = get_metric_series(conn, "pe", 2)
    if not series:
        return ind_empty("大盤本益比", estimate=True)
    date_str, pe = series[0]
    sub = "市值加權，不含虧損公司"
    return ind_card("大盤本益比", f"{pe:.1f}<span class=\"ind-unit\"> 倍</span>", sub, date_str, estimate=True)


def render_bias_card(conn):
    """與季線（60日均線）的乖離率，直接用資料庫裡的加權指數歷史計算"""
    rows = conn.execute(
        "SELECT date, close_value FROM index_history WHERE index_code = 'TAIEX' ORDER BY date DESC LIMIT 60"
    ).fetchall()
    if len(rows) < 60:
        return ind_empty("與季線乖離")
    date_str, close = rows[0]
    ma60 = sum(v for _d, v in rows) / len(rows)
    bias = (close - ma60) / ma60 * 100
    label = "正乖離" if bias >= 0 else "負乖離"
    main = f'<span style="color:{signed_color(bias)};">{bias:+.2f}<span class="ind-unit">%</span></span>'
    sub = f"{label}・收盤 {close:,.0f} / 季線 {ma60:,.0f}"
    return ind_card("與季線乖離", main, sub, date_str)


def render_institutional_card(conn):
    items = [("外資", "inst_foreign"), ("投信", "inst_trust"), ("自營商", "inst_dealer")]
    first = get_metric_series(conn, "inst_foreign", 1)
    if not first:
        return ind_empty("三大法人買賣超").replace('class="ind-card"', 'class="ind-card ind-wide"')
    date_str = first[0][0]
    rows_html = []
    for label, metric in items:
        series = get_metric_series(conn, metric, 30)
        if not series:
            continue
        net = series[0][1]
        amount = f'<span style="color:{signed_color(net)};font-weight:700;">{net / 1e8:+,.1f} 億</span>'
        rows_html.append(f"""<div class="inst-row">
            <span class="inst-name">{label}</span>
            <span class="inst-amount">{amount}</span>
            {streak_html(series)}
        </div>""")
    note = "買賣超金額；連買／連賣依資料庫累積的交易日計算"
    return f"""<div class="ind-card ind-wide">
        <h4>三大法人買賣超</h4>
        <div class="inst-list">{"".join(rows_html)}</div>
        <p class="ind-sub">{note}</p>
        <p class="value-date">{escape(date_str)}</p>
    </div>"""


def render_indicator_section(conn):
    cards = "".join([
        render_breadth_card(conn),
        render_volume_card(conn),
        render_margin_card(conn),
        render_maint_card(conn),
        render_pe_card(conn),
        render_bias_card(conn),
        render_institutional_card(conn),
    ])
    return f"""
    <section class="card">
        <div class="section-head">
            <h2>大盤指標</h2>
        </div>
        <div class="ind-grid">{cards}</div>
        <p class="ind-foot">資料來源：臺灣證券交易所公開資料。各項指標各自更新，尚未公布時沿用最近一筆並標示資料日期；標示「估算」者為依公開資料自行推算，數字可能與其他網站略有差異。</p>
    </section>"""


def build_html(conn):
    index_section = render_index_section(conn)
    indicator_section = render_indicator_section(conn)
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
        indicator_section=indicator_section,
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
    if time_slot in ("21:00", "manual"):
        try:
            fetch_tw_indices(conn)
        except Exception as e:
            print(f"[警告] 台股指數抓取過程發生例外，已略過本次: {e}")
    if time_slot in ("07:30", "manual"):
        try:
            fetch_us_indices_data(conn)
        except Exception as e:
            print(f"[警告] 美股指數抓取過程發生例外，已略過本次: {e}")
        try:
            fetch_sector_heatmap(conn)
        except Exception as e:
            print(f"[警告] 產業熱力圖抓取過程發生例外，已略過本次: {e}")
        except Exception as e:
            print(f"[警告] 美股指數抓取過程發生例外，已略過本次: {e}")

    print("步驟 2.5/3：抓取大盤指標（每個時段都嘗試，抓不到就沿用舊值）...")
    try:
        fetch_market_indicators(conn)
    except Exception as e:
        print(f"[警告] 大盤指標抓取過程發生例外，已略過本次: {e}")

    print("步驟 3/3：產生網頁...")
    build_html(conn)

    conn.close()
    print("完成！")


if __name__ == "__main__":
    main()
