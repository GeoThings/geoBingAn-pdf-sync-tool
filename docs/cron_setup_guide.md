# 排程設定指南（macOS launchd）

**最後更新：** 2026-09-15

> 已從 cron 遷移到 launchd（macOS 原生排程系統）。
>
> ⚠️ **launchd 的 `StartCalendarInterval` 不會主動喚醒 Mac，睡眠期間錯過的排程也不保證補跑**，因此**必須**搭配 `sudo pmset repeat wakepoweron MTWRFSU 07:55:00` 讓機器在排程前醒來。（2026-05 事故即因缺少 wake schedule 導致排程整段跳過。）

---

## 當前排程

| 時間 | LaunchAgent | 內容 |
|------|-------------|------|
| 每日 08:00 | `com.geothings.geobingan.healthcheck` | 8 項檢查：Token／磁碟／同步狀態／API／launchd／上傳暫停／解析積壓／解析預算；異常去重後貼 ClickUp，error 級 @ |
| 每日 10:00 | `com.geothings.geobingan.weeklysync` | 完整同步流程；週一加產 sync 週報 PDF → ClickUp |
| 週五 17:00 | `com.geothings.geobingan.fridayreport` | 總結週報 PDF → ClickUp |

**預計耗時：** ~15–25 分鐘（全量翻頁掃描後的正常水位；納入大量新案後首次可達 ~60 分鐘）

> 需搭配 `sudo pmset repeat wakepoweron MTWRFSU 07:55:00`，否則 Mac 睡眠時排程不會觸發。

---

## 安裝

```bash
# 一鍵安裝（自動移除舊 cron + 安裝 launchd）
./setup/setup_launchd.sh
```

---

## 執行流程

`run_weekly_sync.sh` 依序執行 7 個步驟：

| 步驟 | 腳本 | 說明 | 失敗行為 |
|------|------|------|----------|
| 1 | `geobingan_sync/steps/sync_permits.py` | 同步 PDF 到 Google Drive（5 thread 並行） | 跳過步驟 2-3 |
| 2 | `geobingan_sync/steps/upload_pdfs.py` | 上傳檔名日期 30 天窗內的 PDF 到究平安（每日上限 `MAX_UPLOADS`、解析預算守門；有 `.pause_upload` 旗標則跳過本步） | 跳過步驟 3 |
| 2.5 | `geobingan_sync/steps/match_permits.py` | 建案名稱交叉比對（6 來源） | 使用現有 registry |
| 3 | `geobingan_sync/steps/generate_permit_tracking_report.py` | 生成追蹤報告 HTML/CSV | 繼續步驟 4 |
| 4 | `git push` | 推送報告到 GitHub | — |
| 5 | `geobingan_sync/steps/generate_weekly_report.py` | 週報 PDF → ClickUp | 不影響同步 |

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
./setup/uninstall_launchd.sh
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
| Mac 睡眠漏跑 | launchd **不會**主動喚醒、也不保證補跑；確認 `pmset -g sched` 有 07:55 wakepoweron，否則重設 |
| Token 過期 | JWT 自動刷新 + 寫回 .env；Refresh Token 7 天過期需手動更新 |
| 腳本失敗 | 查看 `logs/` 目錄和 `logs/launchd_*_err.log` |

---

## 暫停/恢復

```bash
# 暫停（卸載）
./setup/uninstall_launchd.sh

# 恢復（重新安裝）
./setup/setup_launchd.sh
```
