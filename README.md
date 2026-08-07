# 香港網騙及數碼風險新聞監察器 — VERSION 1

**VERSION 1** 是首個正式穩定版，以 GitHub Actions、GitHub Pages 及 Python 標準函式庫運行。無需 API Key、獨立伺服器或付費資料庫。

## VERSION 1 已完成的核心能力

- 每 15 分鐘自動收集香港網騙、電騙、網安、網罪、平台罪案及 AI 安全新聞。
- 監察 15 個香港新聞來源，並支援官方 RSS、Google News 關鍵字搜尋及全站後備搜尋。
- 以 SQLite 去重、保存最近資料、分類新聞及記錄來源健康狀態。
- Gmail 式響應介面，支援桌面、Android、iPhone、搜尋、多選分類、來源及時間篩選。
- PWA 可安裝至手機主畫面，並保留最近成功載入的資料作離線查看。
- 可勾選文章並匯出 TXT；亦會產生最近 48 小時 CSV。

## 文章連結安全與正確性

VERSION 1 對文章連結採取「寧可暫不提供，也不錯誤導向」原則：

- 空白、相對、非 HTTP/HTTPS、Google News wrapper 及未驗證網址不會變成可點擊連結。
- RSS 連結後方誤夾的 `target`、`rel` 等 HTML 屬性會被移除，URL 內空白會安全編碼。
- 空白網址不會被瀏覽器解析成本站首頁。
- Google News opaque URL 逐條解碼，不按批次位置配對。
- 解碼結果必須符合預期新聞來源；可讀取原文標題時亦必須與 RSS 標題相符。
- Redirect fallback 沒有可驗證標題時不會獲准成為原文連結。
- `url_overrides.json` 可按「來源＋標題」修復已知文章直連，並防止後續解析器覆蓋。
- 網頁、TXT 及 CSV 均不會輸出尚未確認的 Google News 或舊版錯配網址。

## 一次部署到 GitHub

1. 建立一個新的 **Public repository**，例如 `hk-digital-risk-news-monitor`。
2. 把本資料夾內所有檔案上傳到 repository 根目錄；`.github` 隱藏資料夾亦必須上傳。
3. 進入 **Settings → Pages**。
4. 在 **Build and deployment → Source** 選擇 **GitHub Actions**。
5. 進入 **Actions → Update news and deploy website → Run workflow**，手動執行第一次。
6. Build 與 Deploy 全部完成後，GitHub Pages 會顯示網站網址。

排程位於 `.github/workflows/update-and-deploy.yml`，以 `Asia/Hong_Kong` 時區於每小時 00、15、30、45 分執行。

## 加入自訂監察關鍵字

編輯根目錄 `keywords.txt`，每行一個搜尋條件，例如：

```text
深偽詐騙
假冒客服
("加密貨幣" OR Bitcoin) (騙案 OR 詐騙)
```

空白行及以 `#` 開頭的行會被忽略。自訂查詢結果仍只接受 `config.json` 已設定的目標新聞來源。

## 修正指定文章網址

在 `url_overrides.json` 加入：

```json
{
  "source": "新聞來源",
  "titles": ["文章完整標題", "另一個標題版本"],
  "url": "https://新聞來源的原文網址"
}
```

系統會正規化空格、標點、時間及日期後綴，但仍要求來源與標題準確配對。

## 本機預覽

Windows 雙擊：

```text
run_local.bat
```

macOS／Linux：

```bash
./run_local.sh
```

瀏覽 `http://localhost:8000`。請勿直接雙擊 `index.html`，因為瀏覽器通常不允許 `file://` 頁面讀取 JSON。

## 本機收集最新新聞

```bash
python scripts/build_web_data.py --workers 10
python scripts/validate_web.py
python -m http.server 8000 --directory web
```

## 正式發布前檢查

```bash
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
python scripts/build_web_data.py --skip-collect
python scripts/validate_web.py
node --check web/assets/app.js
node --check web/service-worker.js
```

## 主要檔案

```text
.github/workflows/update-and-deploy.yml  自動收集及部署
src/news_core.py                         收集、分類、連結驗證及 SQLite
scripts/build_web_data.py                產生 JSON、狀態及 CSV
scripts/validate_web.py                  完整靜態發布驗證
web/                                     GitHub Pages／PWA 網站
seed/news_monitor_seed.db                首次運行的資料庫種子
runtime/                                 Actions Cache／本機運行資料
config.json                              新聞來源及分類設定
keywords.txt                             自訂監察搜尋條件
url_overrides.json                       已核實文章直連修復表
tests/                                   無網絡單元測試
```

## 安全及限制

GitHub Pages 是公開網站。不要把密碼、Token、私人資料或 API Key 寫入 `web/`、`config.json`、workflow 或其他 repository 檔案。

新聞來源可能改動 RSS、封鎖自動請求或返回 429／5xx。系統會保留可用資料並把無法確認的文章連結設為不可點擊。自動分類只供監察及整理用途，重要判斷應閱讀新聞原文核實。
