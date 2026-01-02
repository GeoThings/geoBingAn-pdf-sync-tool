# 補充上傳 PDF 附件工具使用指南

## 📋 功能說明

`upload_attachments.py` 是用來補充已建立的 Report 缺失的 PDF 附件。

### 使用場景

當您發現：
- ✅ Report 成功建立（有 AI 分析結果）
- ✅ Report 的 metadata 中有檔名記錄
- ❌ Report 沒有附件（`attachments: []`）
- ❌ S3 上找不到原始 PDF

這個工具可以：
1. 從 Django API 查詢沒有附件的 Reports
2. 從 Google Drive 重新下載原始 PDF
3. 使用 `AttachmentService` 建立 FileAttachment 並上傳到 S3

## 🚀 使用方式

### 前置需求

1. **Docker 容器正在運行**：
   ```bash
   docker ps | grep geobingan-web
   ```

2. **配置檔案已設定**（`config.py`）：
   - JWT_TOKEN
   - GROUP_ID
   - USER_EMAIL
   - GEOBINGAN_API_URL

3. **Google Drive Service Account 金鑰**（`service-account-key.json`）

### 執行步驟

#### 步驟 1: 檢查需要補充的 Reports

```bash
cd /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool
python3 upload_attachments.py
```

#### 步驟 2: 查看輸出

工具會顯示：
```
🔧 補充上傳 PDF 附件工具
============================================================
用戶: jerryjo0802@gmail.com
群組 ID: 921630a9-41d6-436e-8da4-492f01446bdc
============================================================

✅ Google Drive API 已初始化 (xxx@xxx.iam.gserviceaccount.com)

📋 正在查詢沒有附件的 Reports...
✅ 找到 15 個沒有附件的 Reports

============================================================
開始處理 15 個 Reports
============================================================

[1/15] 處理: 建案安全監測週報
  - Report ID: bdbc23e9-a528-4b35-bcac-12ab1d5dd790
  - 檔名: 恆合-錦西街    日報表 114.12.25.pdf
  - Google Drive ID: 1ABC...XYZ
  - 檔案大小: 0.85 MB
  📥 正在下載...
  📤 正在建立附件並上傳到 S3...
  ✅ 附件已建立
     - Attachment ID: a1b2c3d4-...
     - File Path: attachments/bdbc23e9-.../file.pdf

[2/15] 處理: ...
...

============================================================
處理完成
============================================================
✅ 成功: 12
❌ 失敗: 2
⏭️  跳過: 1
📊 總計: 15
============================================================
```

## 🔍 驗證結果

### 方法 1: 透過 Django Admin

1. 開啟 http://localhost:8000/admin/
2. 登入管理員帳號
3. 進入 Reports → File attachments
4. 篩選最近建立的附件

### 方法 2: 透過 Django Shell

```bash
docker exec geobingan-web python manage.py shell -c "
from apps.reports.models import Report, FileAttachment

# 檢查特定 Report
report_id = 'bdbc23e9-a528-4b35-bcac-12ab1d5dd790'
report = Report.objects.get(id=report_id)

print(f'Report: {report.title}')
print(f'Attachments: {report.attachments.count()}')

for att in report.attachments.all():
    print(f'  - {att.original_filename}')
    print(f'    File Path: {att.file_path}')
    print(f'    Size: {att.file_size / 1024 / 1024:.2f} MB')
"
```

### 方法 3: 檢查 S3（如果配置了）

```bash
# 使用 AWS CLI
aws s3 ls s3://your-bucket-name/attachments/ --recursive
```

## 🛠️ 故障排除

### 問題 1: 找不到 Google Drive 檔案

**症狀**：
```
⚠️  找不到檔案: xxx.pdf
⏭️  跳過（找不到檔案）
```

**原因**：
- Google Drive 中檔名不完全一致
- 檔案已被刪除
- Service Account 沒有存取權限

**解決**：
1. 檢查 Google Drive 中的檔名
2. 確認 Service Account 有讀取權限
3. 手動分享檔案給 Service Account email

### 問題 2: Django shell 執行失敗

**症狀**：
```
❌ Django 錯誤: ...
```

**原因**：
- Docker 容器未運行
- Report ID 不存在
- 檔案內容格式錯誤

**解決**：
```bash
# 檢查 Docker 狀態
docker ps | grep geobingan-web

# 檢查 Django 日誌
docker logs geobingan-web | tail -50
```

### 問題 3: 找到多個同名檔案

**症狀**：
```
⚠️  找到多個同名檔案，使用最新的
```

**說明**：
- 工具會自動選擇最新修改的檔案
- 如需指定特定檔案，需手動修改 metadata 加入 Google Drive file_id

### 問題 4: 執行超時

**症狀**：
```
⏱️  Django shell 執行超時
```

**原因**：
- 檔案太大（超過 60 秒處理時間）
- S3 上傳速度慢

**解決**：
- 修改腳本中的 `timeout=60` 增加時間
- 檢查網路連線和 S3 配置

## 📊 預期成效

執行成功後：

### 資料庫變化
```sql
-- 之前
SELECT COUNT(*) FROM reports_fileattachment;  -- 0

-- 之後
SELECT COUNT(*) FROM reports_fileattachment;  -- 15 (假設處理 15 個)
```

### S3 儲存空間
```
s3://your-bucket/
└── attachments/
    ├── bdbc23e9-a528-4b35-bcac-12ab1d5dd790/
    │   └── 恆合-錦西街日報表114.12.25.pdf
    ├── 705bcbba-1491-4c7d-b38c-241343d67fd9/
    │   └── 中華電信濱江資料中心監測報告-(1141223).pdf
    └── ...
```

### Django Admin
- Reports → File attachments 會顯示新建立的附件
- 每個附件都有關聯的 Report
- 可以點擊「File path」查看檔案路徑

## 🔄 定期執行

如果需要定期自動補充附件，可以設定 cron job：

```bash
# 每天凌晨 2 點執行
0 2 * * * cd /path/to/geoBingAn-pdf-sync-tool && python3 upload_attachments.py >> /var/log/upload_attachments.log 2>&1
```

## 🎯 與 upload_pdfs.py 的關係

### upload_pdfs.py（主要上傳工具）
- 從 Google Drive 掃描新 PDF
- 上傳到 Django API 進行 AI 分析
- 建立 Report 和 ConstructionProject
- ❌ **不建立附件**（目前的問題）

### upload_attachments.py（補充工具）
- 查詢已建立但沒有附件的 Reports
- 從 Google Drive 重新下載 PDF
- 建立 FileAttachment 並上傳到 S3
- ✅ **補上缺失的附件**

### 建議執行順序

```bash
# 1. 先執行主要上傳（AI 分析 + 建立 Report）
python3 upload_pdfs.py

# 2. 再執行補充工具（建立附件 + 上傳 S3）
python3 upload_attachments.py
```

或者合併為一個腳本：

```bash
#!/bin/bash
# 完整上傳流程

echo "步驟 1: AI 分析和建立 Report..."
python3 upload_pdfs.py

echo ""
echo "步驟 2: 補充 PDF 附件到 S3..."
python3 upload_attachments.py

echo ""
echo "✅ 完整流程完成"
```

## ⚠️ 注意事項

1. **執行順序**：必須在 `upload_pdfs.py` 執行後才執行此工具
2. **Docker 依賴**：需要 Docker 容器正在運行
3. **網路連線**：需要能連線到 Google Drive API 和 Django API
4. **檔案大小**：大型 PDF 可能需要較長時間處理
5. **權限檢查**：確保 Service Account 有 Google Drive 讀取權限

## 📞 技術支援

如有問題，請檢查：
1. Docker 容器日誌：`docker logs geobingan-web`
2. 工具執行日誌（螢幕輸出）
3. Django Admin 中的附件記錄
4. S3 Console（如果配置了）

---

**更新日期**: 2026-01-02
**工具版本**: 1.0.0
**Python 版本**: 3.8+
