"""
狀態追蹤模組 - 記錄同步執行狀態

功能：
- 記錄每次執行結果
- 追蹤成功/失敗次數
- 統計上傳數量
- 保存執行歷史

使用方式：
    from sync_status import SyncStatus

    status = SyncStatus()
    status.start_run()

    # ... 執行同步 ...

    status.end_run(
        status='success',
        synced_pdfs=100,
        uploaded_pdfs=50,
        failed_uploads=2
    )
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List


class SyncStatus:
    """同步狀態追蹤器"""

    def __init__(self, state_dir: Optional[str] = None):
        """
        初始化狀態追蹤器

        Args:
            state_dir: 狀態檔案目錄，預設為 ./state
        """
        if state_dir:
            self.state_dir = Path(state_dir)
        else:
            self.state_dir = Path(__file__).parent / 'state'

        self.state_dir.mkdir(exist_ok=True)
        self.status_file = self.state_dir / 'sync_status.json'
        self._start_time_file = self.state_dir / '.sync_start_time'
        self.data = self._load_status()
        self._start_time: Optional[datetime] = None

    def _load_status(self) -> Dict[str, Any]:
        """載入狀態檔案"""
        if self.status_file.exists():
            try:
                with open(self.status_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        # 預設結構
        return {
            "last_run": None,
            "last_status": None,
            "stats": {
                "total_runs": 0,
                "success_count": 0,
                "failure_count": 0,
                "total_synced_pdfs": 0,
                "total_uploaded_pdfs": 0,
                "total_failed_uploads": 0
            },
            "history": []
        }

    def _save_status(self):
        """儲存狀態檔案"""
        with open(self.status_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def start_run(self):
        """標記執行開始（會保存到檔案以支援跨 process）"""
        self._start_time = datetime.now()
        # 保存到檔案以支援跨 process
        with open(self._start_time_file, 'w') as f:
            f.write(self._start_time.isoformat())
        print(f"📝 同步開始於 {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    def end_run(self,
                status: str = 'success',
                synced_pdfs: int = 0,
                uploaded_pdfs: int = 0,
                failed_uploads: int = 0,
                error_message: Optional[str] = None,
                duration_seconds: Optional[int] = None):
        """
        標記執行結束並記錄結果

        Args:
            status: 執行狀態 ('success', 'failure', 'partial')
            synced_pdfs: 同步的 PDF 數量
            uploaded_pdfs: 上傳的 PDF 數量
            failed_uploads: 上傳失敗的數量
            error_message: 錯誤訊息（如果失敗）
            duration_seconds: 執行秒數（可由 shell 傳入，否則從檔案計算）
        """
        end_time = datetime.now()

        # 優先使用傳入的 duration，否則從檔案計算
        if duration_seconds is None:
            duration_seconds = 0
            # 嘗試從檔案讀取 start_time
            if self._start_time_file.exists():
                try:
                    with open(self._start_time_file, 'r') as f:
                        start_iso = f.read().strip()
                        self._start_time = datetime.fromisoformat(start_iso)
                        duration_seconds = (end_time - self._start_time).total_seconds()
                    # 清理暫存檔
                    self._start_time_file.unlink()
                except Exception:
                    pass
            elif self._start_time:
                duration_seconds = (end_time - self._start_time).total_seconds()

        # 更新最後執行資訊
        self.data["last_run"] = end_time.isoformat()
        self.data["last_status"] = status

        # 更新統計
        stats = self.data["stats"]
        stats["total_runs"] += 1
        if status == 'success':
            stats["success_count"] += 1
        else:
            stats["failure_count"] += 1
        stats["total_synced_pdfs"] += synced_pdfs
        stats["total_uploaded_pdfs"] += uploaded_pdfs
        stats["total_failed_uploads"] += failed_uploads

        # 添加歷史記錄
        history_entry = {
            "date": end_time.strftime('%Y-%m-%d'),
            "time": end_time.strftime('%H:%M:%S'),
            "status": status,
            "synced": synced_pdfs,
            "uploaded": uploaded_pdfs,
            "failed": failed_uploads,
            "duration_seconds": round(duration_seconds)
        }
        if error_message:
            history_entry["error"] = error_message

        self.data["history"].append(history_entry)

        # 保留最近 100 筆歷史
        if len(self.data["history"]) > 100:
            self.data["history"] = self.data["history"][-100:]

        self._save_status()

        # 輸出摘要
        duration_minutes = duration_seconds / 60
        print(f"\n{'='*50}")
        print(f"📊 執行摘要")
        print(f"{'='*50}")
        print(f"狀態: {'✅ 成功' if status == 'success' else '❌ 失敗' if status == 'failure' else '⚠️ 部分完成'}")
        print(f"同步 PDF: {synced_pdfs}")
        print(f"上傳 PDF: {uploaded_pdfs}")
        print(f"上傳失敗: {failed_uploads}")
        print(f"執行時間: {duration_minutes:.1f} 分鐘")
        if error_message:
            print(f"錯誤訊息: {error_message}")
        print(f"{'='*50}\n")

        return {
            "status": status,
            "synced": synced_pdfs,
            "uploaded": uploaded_pdfs,
            "failed": failed_uploads,
            "duration_minutes": duration_minutes
        }

    def get_last_run_info(self) -> Optional[Dict[str, Any]]:
        """取得最後一次執行資訊"""
        if self.data["history"]:
            return self.data["history"][-1]
        return None

    def get_stats(self) -> Dict[str, Any]:
        """取得統計資訊"""
        return self.data["stats"]

    def get_recent_history(self, count: int = 10) -> List[Dict[str, Any]]:
        """取得最近的執行歷史"""
        return self.data["history"][-count:]

    def get_success_rate(self) -> float:
        """計算成功率"""
        stats = self.data["stats"]
        total = stats["total_runs"]
        if total == 0:
            return 0.0
        return (stats["success_count"] / total) * 100

    def print_summary(self):
        """印出狀態摘要"""
        stats = self.data["stats"]
        success_rate = self.get_success_rate()

        print(f"\n{'='*50}")
        print(f"📈 同步狀態總覽")
        print(f"{'='*50}")
        print(f"總執行次數: {stats['total_runs']}")
        print(f"成功次數: {stats['success_count']}")
        print(f"失敗次數: {stats['failure_count']}")
        print(f"成功率: {success_rate:.1f}%")
        print(f"總同步 PDF: {stats['total_synced_pdfs']}")
        print(f"總上傳 PDF: {stats['total_uploaded_pdfs']}")
        print(f"總上傳失敗: {stats['total_failed_uploads']}")

        if self.data["last_run"]:
            print(f"\n最後執行: {self.data['last_run']}")
            print(f"最後狀態: {self.data['last_status']}")

        print(f"{'='*50}\n")


if __name__ == '__main__':
    # 測試狀態追蹤
    print("測試狀態追蹤模組...")

    status = SyncStatus()
    status.print_summary()

    # 模擬一次執行
    print("\n模擬執行...")
    status.start_run()

    import time
    time.sleep(1)

    result = status.end_run(
        status='success',
        synced_pdfs=100,
        uploaded_pdfs=50,
        failed_uploads=2
    )

    print(f"\n執行結果: {result}")
    status.print_summary()

    print("\n最近歷史:")
    for entry in status.get_recent_history(5):
        print(f"  {entry['date']} - {entry['status']} - 同步:{entry['synced']} 上傳:{entry['uploaded']}")
