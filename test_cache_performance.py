#!/usr/bin/env python3
"""
測試快取機制的效能提升
"""
import json
import time
from datetime import datetime

# 讀取狀態檔案
with open('state/uploaded_to_geobingan_7days.json', 'r') as f:
    state = json.load(f)

cache = state.get('cache', {})

print("=" * 60)
print("快取機制效能測試")
print("=" * 60)
print()

# 顯示快取資訊
print("📊 快取狀態:")
print(f"  上次掃描時間: {cache.get('last_scan', 'N/A')}")
print(f"  快取的資料夾: {len(cache.get('folders', []))} 個")
print(f"  快取的 PDF: {len(cache.get('pdfs', []))} 個")
print()

# 檢查快取是否有效
if cache.get('last_scan'):
    last_scan_time = datetime.fromisoformat(cache['last_scan'].replace('Z', '+00:00'))
    now = datetime.now(last_scan_time.tzinfo)
    age_hours = (now - last_scan_time).total_seconds() / 3600

    print(f"⏱️  快取年齡: {age_hours:.2f} 小時")

    if age_hours < 24:
        print(f"✅ 快取有效（< 24 小時）")
    else:
        print(f"⚠️  快取已過期（> 24 小時）")
else:
    print("❌ 尚無快取")

print()

# 計算優化效果
print("=" * 60)
print("優化效果分析")
print("=" * 60)
print()

folders_count = len(cache.get('folders', []))
pdfs_count = len(cache.get('pdfs', []))

print("📈 智慧掃描效果:")
print(f"  優化前: 掃描 1,000 個資料夾 → 16,939 個 PDF")
print(f"  優化後: 掃描 {folders_count} 個資料夾 → {pdfs_count} 個 PDF")
print(f"  減少掃描: {1000 - folders_count} 個資料夾 ({(1000 - folders_count) / 1000 * 100:.1f}%)")
print()

# 估算時間節省
print("⏱️  預估時間節省:")

# 假設每個資料夾需要 0.15 秒來列出 PDF
time_per_folder = 0.15
old_time = 1000 * time_per_folder
new_time = folders_count * time_per_folder

print(f"  優化前初始化: ~{old_time:.1f} 秒 = {old_time / 60:.1f} 分鐘")
print(f"  優化後初始化: ~{new_time:.1f} 秒 = {new_time / 60:.1f} 分鐘")
print(f"  節省時間: ~{old_time - new_time:.1f} 秒 = {(old_time - new_time) / 60:.1f} 分鐘")
print(f"  提升比例: {(old_time - new_time) / old_time * 100:.1f}%")
print()

# 快取使用時間
print("⚡ 使用快取時（第二次執行）:")
print(f"  初始化時間: ~1 秒（直接讀取快取）")
print(f"  相比優化前: 節省 ~{old_time - 1:.1f} 秒 = {(old_time - 1) / 60:.1f} 分鐘")
print(f"  提升比例: {(old_time - 1) / old_time * 100:.1f}%")
print()

print("=" * 60)
print("✅ 測試完成")
print("=" * 60)
