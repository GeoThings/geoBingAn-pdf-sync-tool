# 自動化改善計劃

**建立日期：** 2026-03-09
**目標：** 提升專案的自動化程度、可靠性和可維護性

---

## 📊 當前狀態評估

### 已有的自動化
- ✅ Cron Job 每週執行
- ✅ 自動同步 PDF 到 Google Drive
- ✅ 自動上傳到究平安
- ✅ 自動生成報告
- ✅ 自動推送到 GitHub
- ✅ JWT Token 自動刷新

### 缺少的自動化
- ❌ 錯誤通知機制
- ❌ 健康檢查監控
- ❌ 環境變數管理 (.env)
- ❌ 失敗自動重試
- ❌ 執行狀態儀表板
- ❌ CI/CD 整合

---

## 🎯 改善建議（優先級排序）

### 第一優先級：必要改善

#### 1. 環境變數管理 (.env)
**現狀：** 敏感資訊在 config.py 中
**改善：** 使用 .env 檔案 + python-dotenv

**優點：**
- 更安全的憑證管理
- 更容易在不同環境切換
- 符合 12-factor app 原則

**實作難度：** 低 (30分鐘)

#### 2. 錯誤通知機制
**現狀：** 錯誤只記錄在日誌
**改善：** 失敗時發送通知

**方案選擇：**
- A) macOS 通知 (最簡單)
- B) LINE Notify (推薦)
- C) Slack Webhook
- D) Email 通知

**實作難度：** 低-中 (1小時)

#### 3. 執行狀態追蹤
**現狀：** 需手動檢查日誌
**改善：** 建立狀態追蹤機制

**內容：**
- 最後執行時間
- 成功/失敗次數
- 上傳檔案數量統計

**實作難度：** 低 (30分鐘)

### 第二優先級：建議改善

#### 4. 失敗自動重試
**現狀：** 失敗需手動重跑
**改善：** 記錄失敗項目，下次自動重試

**實作難度：** 中 (1-2小時)

#### 5. GitHub Actions CI/CD
**現狀：** 只有本地 cron
**改善：** 使用 GitHub Actions 排程執行

**優點：**
- 不依賴本地電腦
- 自動執行更可靠
- 可視化執行歷史

**實作難度：** 中 (2小時)

#### 6. 健康檢查端點
**現狀：** 無監控
**改善：** 建立簡單的狀態頁面

**實作難度：** 中 (1-2小時)

### 第三優先級：進階改善

#### 7. Docker 容器化
**優點：** 可移植、環境一致
**實作難度：** 中-高 (3-4小時)

#### 8. 監控儀表板
**方案：** 使用 GitHub Pages 顯示統計
**實作難度：** 高 (4-6小時)

---

## 🚀 執行計劃

### 階段一：基礎改善 (今日執行)

1. **建立 .env 環境變數管理**
   - 建立 .env.example 範本
   - 修改腳本使用 python-dotenv
   - 更新 .gitignore

2. **加入錯誤通知 (LINE Notify)**
   - 失敗時發送 LINE 通知
   - 成功時發送摘要

3. **建立執行狀態追蹤**
   - 建立 state/sync_status.json
   - 記錄每次執行結果

### 階段二：可靠性改善 (下週)

4. 失敗項目自動重試機制
5. GitHub Actions 排程執行

### 階段三：進階功能 (下月)

6. 健康檢查頁面
7. Docker 容器化
8. 監控儀表板

---

## 📋 詳細實作規格

### 1. .env 環境變數

```bash
# .env.example
# Google Drive
GOOGLE_CREDENTIALS=./credentials.json

# geoBingAn API
JWT_TOKEN=your-jwt-token
REFRESH_TOKEN=your-refresh-token
GROUP_ID=your-group-id
USER_EMAIL=your-email

# API URLs
GEOBINGAN_API_URL=https://riskmap.today/api/reports/construction-reports/upload/
GEOBINGAN_REFRESH_URL=https://riskmap.today/api/auth/auth/refresh_token/

# 通知設定 (可選)
LINE_NOTIFY_TOKEN=your-line-notify-token
```

### 2. LINE Notify 整合

```python
def send_line_notify(message: str):
    """發送 LINE Notify 通知"""
    token = os.environ.get('LINE_NOTIFY_TOKEN')
    if not token:
        return

    requests.post(
        'https://notify-api.line.me/api/notify',
        headers={'Authorization': f'Bearer {token}'},
        data={'message': message}
    )
```

### 3. 狀態追蹤格式

```json
{
  "last_run": "2026-03-09T09:00:00",
  "last_status": "success",
  "stats": {
    "total_runs": 52,
    "success_count": 50,
    "failure_count": 2,
    "last_7_days": {
      "synced_pdfs": 1234,
      "uploaded_pdfs": 567,
      "failed_uploads": 3
    }
  },
  "history": [
    {
      "date": "2026-03-09",
      "status": "success",
      "synced": 45,
      "uploaded": 12,
      "duration_seconds": 3600
    }
  ]
}
```

---

## ✅ 建議執行順序

| 優先級 | 項目 | 時間 | 效益 |
|--------|------|------|------|
| 1 | .env 環境變數 | 30分鐘 | 安全性大幅提升 |
| 2 | 執行狀態追蹤 | 30分鐘 | 可視化執行結果 |
| 3 | LINE Notify 通知 | 30分鐘 | 即時錯誤通知 |
| 4 | 整合到 run_weekly_sync.sh | 30分鐘 | 完整自動化 |

**總計：約 2 小時**

---

## 🔧 執行後驗證

1. 手動執行 `./run_weekly_sync.sh` 測試
2. 確認 LINE 收到通知
3. 確認 state/sync_status.json 更新
4. 確認錯誤處理正常

---

**文件版本：** v1.0
**作者：** Claude Code
