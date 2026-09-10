"""
本機測試腳本：用模擬資料測試「依發布時間篩選」的邏輯是否正確，
並驗證24小時內不夠時會自動放寬到3天內。
不實際呼叫外部RSS（因為測試環境網路白名單限制）。
"""
import os
import sys
import email.utils
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import update_site as app

conn = app.init_db()
now = datetime.datetime.now(datetime.timezone.utc)


def hours_ago(h):
    return email.utils.format_datetime(now - datetime.timedelta(hours=h))


def days_ago(d):
    return email.utils.format_datetime(now - datetime.timedelta(days=d))


# ---- 情境一：總經新聞，24小時內有超過10則 → 應該只顯示24小時內的 ----
for i in range(12):
    app.save_article(
        conn,
        f"【24h內】總經測試新聞 {i+1}",
        f"https://example.com/macro-recent-{i+1}",
        "中央社", "macro", hours_ago(i * 2),
        f"這是第{i+1}則24小時內的總經測試新聞內容摘要，用來驗證時間篩選邏輯。"
    )
# 加幾則更舊的（3天前），驗證不會被抓進來（因為24h內已經夠了）
for i in range(3):
    app.save_article(
        conn,
        f"【3天前，應被排除】總經舊新聞 {i+1}",
        f"https://example.com/macro-old-{i+1}",
        "中央社", "macro", days_ago(2 + i),
        "這則是3天前的舊新聞，應該不會出現在畫面上。"
    )

# ---- 情境二：台股新聞，24小時內只有3則（不夠10則）→ 應該放寬到3天內 ----
for i in range(3):
    app.save_article(
        conn,
        f"【24h內】台股測試新聞 {i+1}",
        f"https://example.com/tw-recent-{i+1}",
        "中央社", "taiwan_stock", hours_ago(i * 3),
        f"這是第{i+1}則24小時內的台股測試新聞，數量不足，應該會觸發放寬規則。"
    )
for i in range(8):
    app.save_article(
        conn,
        f"【1-2天前】台股測試新聞 {i+4}",
        f"https://example.com/tw-fallback-{i+1}",
        "中央社", "taiwan_stock", days_ago(1) if i % 2 == 0 else hours_ago(30 + i),
        f"這是第{i+4}則1-2天前的台股測試新聞，應該會因為24h內不夠而被納入。"
    )

# ---- 情境三：國際股市，維持先前的簡單範例 ----
for i, (title, link) in enumerate([
    ("AI熱潮灌頂！南韓晶片出口暴增209% 央行更有底氣偏鷹", "https://example.com/intl-1"),
    ("布蘭特油價漲破91美元 美伊再交火 荷莫茲依然受阻", "https://example.com/intl-2"),
]):
    app.save_article(conn, title, link, "經濟日報", "intl_stock", hours_ago(i * 5),
                      "這是國際股市測試新聞的摘要內容，用來確認畫面顯示正常。")

# ---- 情境四：ETF ----
for i, title in enumerate([
    "0050、0052等五檔重量級ETF換股今生效",
    "跌點即買點？0050人氣擴大領先台積電",
    "7月台股大怒神 0050定期定額124萬戶續創新高",
]):
    app.save_article(conn, title, f"https://example.com/etf-{i+1}", "經濟日報", "etf",
                      hours_ago(i * 4),
                      "這是ETF測試新聞的摘要內容，用來確認畫面顯示正常。")

conn.commit()

# ---- 驗證篩選邏輯是否正確 ----
macro_result = app.get_recent_articles(conn, "macro")
taiwan_result = app.get_recent_articles(conn, "taiwan_stock")

print(f"[驗證] 總經新聞應該只有24h內的12則，實際筛選出：{len(macro_result)} 則")
assert all("3天前" not in r[0] for r in macro_result), "❌ 錯誤：3天前的舊新聞不該出現在24h內已足夠的情況下！"
print("  ✅ 通過：24h內已足夠時，沒有混入更舊的新聞")

print(f"[驗證] 台股新聞24h內只有3則，應觸發放寬規則，實際篩選出：{len(taiwan_result)} 則")
assert len(taiwan_result) > 3, "❌ 錯誤：應該要放寬到3天內，數量應該要超過3則！"
print("  ✅ 通過：24h內不足時，成功放寬到3天內")

app.build_html(conn)
conn.close()

print("\n測試完成，已產生 index.html")
