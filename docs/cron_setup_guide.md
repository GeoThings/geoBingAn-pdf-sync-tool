# Cron Job 設定指南

**最後更新：** 2026-04-02

---

## 當前設定

```cron
SHELL=/bin/bash
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

# geoBingAn PDF 週期同步 - 每週一早上 9:00
0 9 * * 1 /path/to/geoBingAn-pdf-sync-tool/run_weekly_sync.sh
```

**預計耗時：** ~5 分鐘（v3.4 優化後）

---

## 執行流程

`run_weekly_sync.sh` 依序執行 4 個步驟：

| 步驟 | 腳本 | 說明 | 失敗行為 |
|------|------|------|----------|
| 1 | `sync_permits.py` | 同步 PDF 到 Google Drive（5 thread 並行） | 跳過步驟 2-3 |
| 2 | `upload_pdfs.py` | 上傳農曆新年後的 PDF 到究平安 | 跳過步驟 3 |
| 3 | `generate_permit_tracking_report.py` | 生成追蹤報告 HTML/CSV | 繼續步驟 4 |
| 4 | `git push` | 推送報告到 GitHub Pages | — |

---

## 必要檔案

```
geoBingAn-pdf-sync-tool/
├── .env                  # JWT_TOKEN, REFRESH_TOKEN, GROUP_ID 等
├── credentials.json      # Google Drive Service Account 金鑰
├── venv/                 # Python 虛擬環境
└── run_weekly_sync.sh    # 入口腳本（需有執行權限）
```

---

## 設定步驟

```bash
# 1. 確認腳本有執行權限
chmod +x run_weekly_sync.sh

# 2. 編輯 crontab
crontab -e

# 3. 添加排程（改成實際路徑）
0 9 * * 1 /path/to/geoBingAn-pdf-sync-tool/run_weekly_sync.sh

# 4. 確認 macOS 有給 cron 完整磁碟取用權限
# 系統設定 > 隱私權與安全性 > 完整磁碟取用權限 > 加入 /usr/sbin/cron
```

---

## 查看執行狀態

```bash
# 最新日誌
ls -lt logs/weekly_sync_*.log | head -1

# 查看日誌內容
tail -50 logs/weekly_sync_$(ls -t logs/weekly_sync_*.log | head -1 | xargs basename)

# macOS cron 系統日誌
log show --predicate 'process == "cron"' --last 24h
```

---

## 故障排除

| 問題 | 解決方案 |
|------|----------|
| Cron 沒執行 | `crontab -l` 確認設定存在，檢查完整磁碟取用權限 |
| 找不到 Python | 在 crontab 設定 `PATH=/opt/homebrew/bin:...` |
| Token 過期 | 程式自動刷新，但 Refresh Token 7 天過期需手動更新 .env |
| 腳本執行失敗 | 查看 `logs/weekly_sync_*.log` 找到具體錯誤 |

---

## 暫停/恢復

```bash
# 暫停：註解掉
crontab -e
# 行首加 #

# 恢復：移除 #
crontab -e
```
