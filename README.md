# geoBingAn PDF 同步上傳工具

自動從台北市政府建管處同步建案 PDF，並上傳到 geoBingAn Backend API 建立監測報告。

## 📊 最新測試結果（2026-01-09）

**上傳測試：** ✅ 通過

| 測試項目 | 狀態 | 成功率 |
|---------|------|--------|
| upload_pdfs.py | ✅ 成功 | 100% (16/16 PDF) |
| JWT 自動刷新 | ✅ 正常 | Token 過期自動更新 |
| 後端 AI 解析 | ✅ 完成 | Gemini 2.5 Pro |

**功能特色：**
- ✅ JWT Token 自動刷新（過期前 5 分鐘自動更新）
- ✅ 死鎖問題已修復（threading.Lock 優化）
- ✅ 智慧快取機制（99.5% 效能提升）
- ✅ 支援每日 cron job 自動執行

詳見文件：
- [問題排解指南](docs/troubleshooting.md) 🔧 常見問題解決方案
- [效能優化報告](docs/cache_optimization_report.md)

---

## 🎯 工具定位

**這是一個純粹的 PDF 同步上傳工具**，只負責：
1. ✅ 從台北市政府建管處同步建案 PDF 到 Google Drive
2. ✅ 從 Google Drive 上傳 PDF 到 geoBingAn Backend API
3. ✅ 定期自動執行（cron job）

**後端負責（不由此工具處理）**：
- AI 分析（Gemini 2.5/3.0 Pro）
- 建立 Report 和 ConstructionProject
- 檔案儲存（S3 或本地）
- JSON 資料儲存

---

## 🚀 快速開始

### 1. 安裝依賴

```bash
# 建立虛擬環境
python3 -m venv venv
source venv/bin/activate

# 安裝套件
pip install -r requirements.txt
```

### 2. 設定 Google Drive 認證

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立 Service Account
3. 下載 JSON 金鑰並儲存為 `credentials.json`
4. 將 Service Account email 加入共享雲端的協作者

### 3. 設定 API 認證

複製範例並填入你的認證資訊：
```bash
cp config.py.example config.py
# 編輯 config.py 填入以下資訊
```

**config.py 必要設定：**
```python
JWT_TOKEN = 'your_access_token'           # Access Token（會自動刷新）
REFRESH_TOKEN = 'your_refresh_token'      # Refresh Token（有效期 7 天）
USER_EMAIL = 'your_email@example.com'
GROUP_ID = 'your-group-id'
GEOBINGAN_API_URL = 'https://riskmap.today/api/reports/construction-reports/upload/'
GEOBINGAN_REFRESH_URL = 'https://riskmap.today/api/auth/auth/refresh_token/'
```

### 4. 執行

#### 手動執行：
```bash
# 步驟 1: 同步建案 PDF from 台北市政府
python3 sync_permits.py

# 步驟 2: 上傳最近 7 天的 PDF 到 Backend
python3 upload_pdfs.py
```

#### 自動執行（已設定 cron job）：
```bash
# 每週一早上 9:00 自動執行
./run_weekly_sync.sh
```

---

## 📋 核心腳本說明

### `sync_permits.py`
從台北市政府建管處網站同步建案 PDF 到 Google Drive

**功能：**
- 下載台北市政府最新建案清單 PDF
- 解析 PDF 中的建案代碼和 Google Drive 連結
- 自動建立資料夾並下載 PDF 到共享雲端
- 斷點續傳，避免重複下載

**設定：**
```python
SERVICE_ACCOUNT_FILE = '/path/to/credentials.json'
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'
PDF_LIST_URL = 'https://www-ws.gov.taipei/...'
```

**狀態追蹤：**
```
state/sync_permits_progress.json
```

---

### `upload_pdfs.py`
從 Google Drive 上傳最近 7 天的 PDF 到 geoBingAn Backend API

**功能：**
- 掃描 Google Drive 中的建案 PDF
- 過濾最近 7 天更新的檔案
- 呼叫 Backend API `/api/reports/construction-reports/upload/` 上傳 PDF
- 自動建立 Report（由後端 Gemini 2.5 Pro 處理）
- JWT Token 自動刷新（過期前 5 分鐘）
- 智慧快取機制避免重複掃描
- 避免重複上傳

**設定：**
```python
DAYS_AGO = 7                    # 只上傳最近 7 天的 PDF
MAX_UPLOADS = 5                 # 單次最多上傳 5 個
DELAY_BETWEEN_UPLOADS = 2       # 上傳間隔 2 秒
AUTO_CONFIRM = True             # 自動確認模式
```

**狀態追蹤：**
```
state/uploaded_to_geobingan_7days.json
```

**API 呼叫：**
```bash
POST /api/reports/construction-reports/upload/
Headers:
  - Authorization: Bearer <JWT_TOKEN>
Body (multipart/form-data):
  - file: PDF 檔案
  - group_id: "user-group-id"
```

---

### `run_weekly_sync.sh`
自動執行完整流程的 Shell 腳本

**執行順序：**
1. `sync_permits.py` - 同步最新 PDF 到 Google Drive
2. `upload_pdfs.py` - 上傳最近 7 天的 PDF 到 Backend

**日誌管理：**
- 日誌檔案：`logs/weekly_sync_YYYYMMDD_HHMMSS.log`
- 自動清理超過 30 天的舊日誌

**手動執行：**
```bash
./run_weekly_sync.sh

# 查看日誌
tail -f logs/weekly_sync_*.log
```

---

## ⏰ 定期執行設定

### Cron Job（已設定）

```bash
# 查看當前排程
crontab -l

# 當前設定：每週一早上 9:00
0 9 * * 1 /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/run_weekly_sync.sh
```

### 修改排程時間

```bash
# 編輯 crontab
crontab -e

# Cron 格式：分 時 日 月 星期
# 範例：每週三下午 3:00
0 15 * * 3 /path/to/run_weekly_sync.sh

# 範例：每天早上 8:00
0 8 * * * /path/to/run_weekly_sync.sh
```

---

## 📁 專案結構

```
geoBingAn-pdf-sync-tool/
├── sync_permits.py              # 核心：同步 PDF from 台北市政府
├── upload_pdfs.py               # 核心：上傳 PDF to Backend API
├── run_weekly_sync.sh           # 核心：自動執行腳本
│
├── config.py                    # API 認證配置（需自行建立）
├── credentials.json             # Google Drive 金鑰（需自行建立）
├── requirements.txt             # Python 依賴
├── README.md                    # 本文件
├── .gitignore                   # Git 忽略清單
│
├── state/                       # 狀態追蹤
│   ├── sync_permits_progress.json       # 同步進度
│   └── uploaded_to_geobingan_7days.json # 上傳記錄
│
├── logs/                        # 執行日誌
│   └── weekly_sync_*.log        # 週期執行日誌
│
└── archive/                     # 舊檔案備份（可選工具）
    ├── check_upload_status.py  # 檢查上傳狀態工具
    ├── upload_attachments.py   # 附件補充工具（已廢棄）
    └── ...
```

---

## 🔍 狀態追蹤

### `state/sync_permits_progress.json`
記錄已同步的建案（從台北市政府）
```json
{
  "processed": ["112建字第0001號", "113建字第0008號", ...],
  "errors": [],
  "restricted": []
}
```

### `state/uploaded_to_geobingan_7days.json`
記錄最近 7 天上傳的 PDF
```json
{
  "uploaded_files": [
    "112建字第0087號/北士科服務中心監測日報20251229.pdf",
    "111建字第0252號/1141222 25觀測報告.pdf",
    ...
  ],
  "errors": []
}
```

### 重置狀態

```bash
# 清除上傳記錄（重新上傳最近 7 天的檔案）
rm state/uploaded_to_geobingan_7days.json

# 清除同步記錄（重新同步所有建案）
rm state/sync_permits_progress.json

# 清除所有狀態
rm state/*.json
```

---

## 🔧 故障排除

詳細問題排解請參考：**[docs/troubleshooting.md](docs/troubleshooting.md)**

### 常見問題快速解答

| 問題 | 解決方案 |
|------|----------|
| 腳本卡住不動 | 死鎖問題，已在 v2.1.0 修復 |
| 401 Unauthorized | JWT Token 過期，會自動刷新 |
| 500 Server Error | 檢查 API 端點是否正確 |
| 504 Timeout | 大型 PDF，增加 timeout 設定 |

### 1. Google Drive 認證失敗

```
❌ 找不到 Service Account 金鑰
```

**解決：**
```bash
# 確認檔案存在
ls -la credentials.json

# 確認 JSON 格式正確
python3 -m json.tool credentials.json
```

### 2. JWT Token 過期

```
❌ 401 Unauthorized - Token has expired
```

**解決：**
- v2.1.0 已內建自動刷新機制
- 確認 `config.py` 中有設定 `REFRESH_TOKEN`
- Refresh Token 有效期 7 天，過期需重新登入取得

### 3. PDF 上傳失敗

可能原因：
- API 速率限制 → 增加 `DELAY_BETWEEN_UPLOADS`
- PDF 格式不支援 → 檢查 Backend 日誌
- 認證過期 → Token 會自動刷新

---

## 📊 監控

### 查看處理統計

```bash
# 已同步建案數量
python3 -c "
import json
with open('state/sync_permits_progress.json') as f:
    data = json.load(f)
    print(f'已處理建案: {len(data[\"processed\"])}')
    print(f'錯誤: {len(data[\"errors\"])}')
"

# 已上傳 PDF 數量
python3 -c "
import json
with open('state/uploaded_to_geobingan_7days.json') as f:
    data = json.load(f)
    print(f'已上傳: {len(data[\"uploaded_files\"])}')
    print(f'失敗: {len(data[\"errors\"])}')
"
```

### 查看最新日誌

```bash
# 查看最新週期執行日誌
tail -100 logs/weekly_sync_*.log | tail -100

# 即時監控
tail -f logs/weekly_sync_*.log
```

---

## 🔐 安全性

### 敏感檔案管理

**不要提交到 Git：**
- ❌ `credentials.json` - Service Account 金鑰
- ❌ `config.py` - API 認證資訊
- ❌ `state/*.json` - 可能包含敏感資訊
- ❌ `*.log` - 執行日誌

這些檔案已在 `.gitignore` 中排除。

### 權限最小化

Service Account 只需要：
- Google Drive API - 讀取權限
- 共享雲端 - 檢視者權限

---

## 📝 版本歷史

### v2.1.0 (2026-01-09)
- ✅ 新增 JWT Token 自動刷新機制
- ✅ 修復死鎖問題（threading.Lock 優化）
- ✅ 改用正確的 API 端點 `/api/reports/construction-reports/upload/`
- ✅ 新增問題排解指南 `docs/troubleshooting.md`
- ⚡ 上傳間隔優化：20秒 → 2秒

### v2.0.0 (2026-01-02)
- ♻️ 簡化工具定位：只負責同步和上傳
- ♻️ 移除附件補充功能（由後端處理）
- ♻️ 移除狀態檢查工具（可選功能移到 archive/）
- ✅ 保留核心功能：`sync_permits.py` + `upload_pdfs.py`
- ✅ 定期執行設定：cron job 每週一早上 9:00
- 📄 更新文檔反映正確需求

### v1.1.0 (2026-01-02)
- ✅ 完成自動上傳測試：13 個 Reports 成功建立
- ✅ 新增完整的上傳歷史記錄
- ✅ 改進狀態追蹤機制
- 📊 測試結果：81.25% 成功率（13/16）

### v1.0.0 (2025-12-31)
- ✅ 初始版本
- ✅ 支援從台北市政府同步建案 PDF
- ✅ 支援上傳到 geoBingAn Backend API
- ✅ 狀態追蹤和 7 天時間過濾

---

## 🆘 獲取幫助

### 問題回報

1. **工具問題**：建立 Issue 在此 repository
2. **Backend 問題**：前往 [geoBingAn_v2_backend](https://github.com/GeoThings/geoBingAn_v2_backend)
3. **API 問題**：查看 Backend API 文檔

### 相關連結

- [geoBingAn Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)
- [geoBingAn Web App](https://riskmap.tw/)

---

## 📄 授權

MIT License

---

**維護者**: geoBingAn Team
**最後更新**: 2026-01-09
