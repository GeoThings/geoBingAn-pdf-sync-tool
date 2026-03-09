# 專案經驗教訓

## 建立時間：2026-03-09

---

## 🔴 必須避免的模式

### 1. 硬編碼絕對路徑
```python
# ❌ 錯誤
SERVICE_ACCOUNT_FILE = '/Users/username/Downloads/credentials.json'

# ✅ 正確
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_CREDENTIALS', './credentials.json')
```

### 2. 禁用 SSL 驗證
```python
# ❌ 錯誤
requests.get(url, verify=False)

# ✅ 正確
requests.get(url, verify=True)  # 或使用 certifi
```

### 3. 裸露的 except
```python
# ❌ 錯誤
except:
    pass

# ✅ 正確
except ValueError as e:
    print(f"錯誤: {e}")
```

### 4. 用正則修改程式碼檔案
```python
# ❌ 錯誤 - 用 regex 修改 config.py
re.sub(pattern, replacement, content)

# ✅ 正確 - 使用 .env 檔案
from dotenv import load_dotenv, set_key
set_key('.env', 'TOKEN', new_value)
```

---

## 📝 文檔維護規則

1. **API 端點變更時必須更新 docs/API.md**
2. **新增設定欄位時必須更新 .env.example**
3. **流程變更時必須更新 cron_setup_guide.md**
4. **每次發版時更新 README.md 版本歷史**

---

## 🧹 定期維護任務

- [ ] 每月檢查 archive/ 目錄，刪除過時檔案
- [ ] 每月檢查 logs/ 目錄，清理舊報告
- [ ] 每季度檢查 requirements.txt 是否完整
- [ ] Token 過期前主動更新（7 天有效期）

---

## ✅ 良好實踐

1. **使用環境變數管理敏感資訊**
2. **使用相對路徑或可配置路徑**
3. **完整的錯誤處理和日誌記錄**
4. **文檔與程式碼同步更新**
5. **定期清理臨時檔案和舊資料**
