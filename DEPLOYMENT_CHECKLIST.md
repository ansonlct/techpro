# VERSION 1 GitHub 部署檢查表

- [ ] repository 根目錄顯示 `VERSION`、`README.md`、`.github/`、`src/`、`scripts/`、`web/`
- [ ] 沒有提交 `runtime/news_monitor.db`、Token、密碼或 API Key
- [ ] Settings → Actions → General：允許 Actions 運行
- [ ] Settings → Pages → Source：選擇 GitHub Actions
- [ ] 手動執行 `Update news and deploy website`
- [ ] Tests、Collect、Validate、Deploy 全部綠色
- [ ] 網站頁尾及系統狀態顯示 `VERSION 1`
- [ ] 最後更新時間、新聞數量及下一次更新倒數正常
- [ ] 抽查至少 10 篇可點擊文章，均進入相同標題的新聞原文
- [ ] 無法確認的文章顯示為不可點擊，而不是進入本站首頁或 Google News
- [ ] 搜尋、分類、來源、時間、排序及核准 TXT 匯出正常
- [ ] `latest_48h.csv` 沒有 Google News wrapper 或本站首頁網址
- [ ] Android Chrome 可安裝；iPhone Safari 可加入主畫面
- [ ] 關閉網絡後，最近成功載入的頁面及資料仍可查看

## 常見問題

### Actions 沒有自動運行

排程只在 default branch 的最新 workflow 上運行。長期沒有 repository 活動時，GitHub 亦可能暫停排程；到 Actions 手動執行一次即可恢復。

### 網站 404

確認 Settings → Pages 的 Source 已選 `GitHub Actions`，並等待 Deploy job 完成。

### 部分 Feed 失敗

新聞來源可能短暫封鎖、變更 RSS 或回傳 429／5xx。只要仍有其他來源成功，系統會保留最近資料；未確認網址不會變成可點擊連結。
