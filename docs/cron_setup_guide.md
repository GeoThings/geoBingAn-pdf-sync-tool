# 排程設定指南（macOS launchd）

**最後更新：** 2026-04-20

> 已從 cron 遷移到 launchd。launchd 是 macOS 原生排程系統，Mac 從睡眠醒來時會自動補跑錯過的任務（cron 不會）。

---

## 當前排程

| 時間 | LaunchAgent | 內容 |
|------|-------------|------|
| 每日 08:00 | `com.geothings.geobingan.healthcheck` | Token/磁碟/同步狀態/API 檢查 |
| 週一 09:00 | `com.geothings.geobingan.weeklysync` | 完整同步流程（7 步驟） |
| 週五 18:00 | `com.geothings.geobingan.fridayreport` | 總結週報 PDF → ClickUp |

**預計耗時：** ~16 分鐘（週一完整同步）

---

## 安裝

```bash
# 一鍵安裝（自動移除舊 cron + 安裝 launchd）
./setup_launchd.sh
```

---

## 執行流程

`run_weekly_sync.sh` 依序執行 7 個步驟：

| 步驟 | 腳本 | 說明 | 失敗行為 |
|------|------|------|----------|
| 1 | `sync_permits.py` | 同步 PDF 到 Google Drive（5 thread 並行） | 跳過步驟 2-3 |
| 2 | `upload_pdfs.py` | 上傳農曆新年後的 PDF 到究平安 | 跳過步驟 3 |
| 2.5 | `match_permits.py` | 建案名稱交叉比對（6 來源） | 使用現有 registry |
| 3 | `generate_permit_tracking_report.py` | 生成追蹤報告 HTML/CSV | 繼續步驟 4 |
| 4 | `git push` | 推送報告到 GitHub | — |
| 5 | `generate_weekly_report.py` | 週報 PDF → ClickUp | 不影響同步 |

---

## 必要檔案

```
geoBingAn-pdf-sync-tool/
├── .env                  # JWT_TOKEN, REFRESH_TOKEN, GROUP_ID, CLICKUP_TOKEN 等
├── credentials.json      # Google Drive Service Account 金鑰
├── cities.json           # 多城市配置
├── venv/                 # Python 虛擬環境
├── run_weekly_sync.sh    # 週一同步入口
├── run_friday_report.sh  # 週五週報入口
└── launchd/              # plist 檔案（LaunchAgent 配置）
```

---

## 管理指令

```bash
# 查看排程狀態
launchctl list | grep geobingan

# 手動觸發同步
launchctl kickstart gui/$(id -u)/com.geothings.geobingan.weeklysync

# 手動觸發健康檢查
launchctl kickstart gui/$(id -u)/com.geothings.geobingan.healthcheck

# 卸載全部排程
./uninstall_launchd.sh
```

---

## 查看執行狀態

```bash
# 同步日誌
tail -50 $(ls -t logs/weekly_sync_*.log | head -1)

# 健康檢查日誌
tail -20 logs/health_check.log

# 週五週報日誌
tail -20 $(ls -t logs/friday_report_*.log | head -1)

# launchd 錯誤日誌
cat logs/launchd_weeklysync_err.log
```

---

## 故障排除

| 問題 | 解決方案 |
|------|----------|
| 排程沒執行 | `launchctl list \| grep geobingan` 確認已載入 |
| Mac 睡眠漏跑 | launchd 會在醒來後自動補跑（這是選擇 launchd 的原因） |
| Token 過期 | JWT 自動刷新 + 寫回 .env；Refresh Token 7 天過期需手動更新 |
| 腳本失敗 | 查看 `logs/` 目錄和 `logs/launchd_*_err.log` |

---

## 暫停/恢復

```bash
# 暫停（卸載）
./uninstall_launchd.sh

# 恢復（重新安裝）
./setup_launchd.sh
```
