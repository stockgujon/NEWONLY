# 台灣財經儀表板

個人使用的財經資訊網站：總經新聞、國內外股市新聞（台股為主）、國內ETF新聞。
每個分類顯示10-15則新聞，優先顯示24小時內的內容，不足時自動放寬到3天內。
全部以條列清單呈現，不需要申請任何API金鑰、不需要註冊任何帳號。
每天自動更新 4 次（07:30 / 12:30 / 14:30 / 21:00），透過 GitHub Actions 全自動執行，
不需要自己的電腦開機。

## 隱私與安全性建議（重要）

這個網站設計上已經盡量降低被陌生人發現的機率，但有個限制要先了解：
**免費的 GitHub 帳號，GitHub Pages 功能只支援 Public（公開）的 Repository**
——Private repository 需要升級付費方案（GitHub Pro）才能使用 Pages 功能。
也就是說，**Repository 必須設為 Public，程式碼技術上任何人都看得到**
（我們已經確認過程式碼內容乾淨、沒有任何機密資訊或個人資料，所以這不是
資安風險，純粹是「會不會被意外看到」的問題）。

以下是實際能降低「被陌生人發現」機率的做法，建議都採用：

1. **Repository 名稱不要用明顯字眼**：例如不要取名 `my-finance-news`，
   改用不容易被聯想或搜尋到的名稱
2. **已內建 `robots.txt` 與 noindex 標籤**：告知 Google 等搜尋引擎不要收錄本站，
   大幅降低被意外搜到的機率（已包含在這次的檔案中，不用額外設定）
3. **不要把這個網址分享或貼到任何公開的地方**（社群、論壇等），
   是目前最有效降低被發現機率的做法
4. **README 與 commit 訊息避免寫出太具體的個人識別資訊**（目前檔案內容已經檢查過乾淨）

## 媒體使用規範說明

本專案只使用各媒體「官方提供的RSS服務」，且只擷取RSS本身附帶的標題與前言
（不會另外爬取全文），並在網頁頁尾附上使用規範說明。抓取頻率已控制在
合理範圍內（每天4次，對應媒體RSS建議的更新頻率），並採用自訂
User-Agent、隨機延遲、錯誤重試退避等機制，降低對來源網站造成負擔。

## 設定步驟

### 1. 建立 GitHub 儲存庫

1. 到 GitHub 建立一個新的 repository，**設為 Public**
   （免費帳號的 GitHub Pages 僅支援 Public repo，詳見上方「隱私與安全性建議」）
2. 建立時取一個不容易被聯想或搜尋到的名稱
3. 把這個資料夾裡的所有檔案上傳到你的 repository

### 2. 開啟 GitHub Pages

1. 到 repository 的 Settings → Pages
2. Source 選擇 "Deploy from a branch"
3. Branch 選擇 `main`，資料夾選擇 `/ (root)`
4. 儲存後，GitHub 會給你一個網址，格式類似：
   `https://你的帳號.github.io/repository名稱/`
5. 這就是你之後每天要打開的固定網址

### 3. 測試自動執行

1. 到 repository 的 Actions 頁籤
2. 選擇左側的「更新財經儀表板」
3. 點選右側的 "Run workflow" 按鈕手動觸發一次，測試整個流程是否正常
4. 執行完成後（大約1-2分鐘），重新整理你的 GitHub Pages 網址，應該就能看到內容了

之後系統會依照排程自動在每天 07:30 / 12:30 / 14:30 / 21:00（台灣時間）
自動執行，你不需要再手動操作。

## 檔案說明

- `update_site.py`：主程式，抓取新聞、篩選分類、產生網頁
- `requirements.txt`：需要安裝的 Python 套件清單
- `.github/workflows/update.yml`：GitHub 自動排程設定
- `data/news.db`：新聞資料庫（程式第一次執行後會自動建立）
- `index.html`：產生出來的網頁（程式執行後自動產生/更新）

## 之後如果想調整

- **想改更新時間**：修改 `.github/workflows/update.yml` 裡的 `cron` 設定
  （注意時間是 UTC，需要跟台灣時間換算，台灣時間 = UTC + 8 小時）
- **想調整每分類的新聞則數或時間篩選範圍**：修改 `update_site.py` 裡的
  `ARTICLES_MIN`、`ARTICLES_MAX`、`FALLBACK_DAYS` 變數
- **想調整總經/國際新聞的篩選關鍵字**：修改 `update_site.py` 裡的
  `MACRO_KEYWORDS`、`INTL_KEYWORDS` 變數
- **想調整網頁樣式**：修改 `update_site.py` 裡的 `HTML_TEMPLATE` 變數中的 `<style>` 區塊
- **ETF來源網址如果抓不到資料**：修改 `update_site.py` 裡的 `UDN_ETF_RSS`
  變數，換成正確的網址（可以到 https://money.udn.com/money/cate/5618
  頁面查看該分類，或搜尋該媒體目前有效的RSS路徑）

## 已知待確認事項

- `UDN_ETF_RSS` 這個網址是根據網站分類結構推導出來的，建議第一次執行後
  檢查網頁上「國內ETF」區塊是否有正確顯示ETF相關新聞，如果是空的或內容不對，
  代表這個網址可能已經失效，需要重新確認正確路徑
