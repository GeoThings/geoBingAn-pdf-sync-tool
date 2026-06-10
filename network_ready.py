"""網路就緒檢查 — 根治 launchd post-wake DNS race (issue #59)。

launchd 在 pmset wake schedule 喚醒 Mac 後幾乎立即 fire job，此時 Wi-Fi 重連 /
DNS resolver 可能尚未就緒，導致 sync_permits.py step1 下載政府 PDF 撞
NameResolutionError、整條 pipeline 硬中止（見 docs/troubleshooting.md #7）。

本模組在 step1 前阻塞等待，直到 enabled 城市的所有 PDF host 都能 DNS 解析成功，
或超過 timeout。被 run_weekly_sync.sh 在步驟 1 之前呼叫；即使逾時也只回傳非零、
不 abort（交由 step1 既有錯誤處理），因此屬「只會改善、不會更糟」的前置 gate。
"""
import socket
import sys
import time
from urllib.parse import urlparse
from typing import Callable, List, Tuple

from city_config import get_enabled_cities


def hosts_from_cities() -> List[str]:
    """從 enabled 城市的 pdf_list_url 取出去重後的 hostname 清單（保序）。"""
    hosts: List[str] = []
    for city in get_enabled_cities():
        url = city.get('pdf_list_url')
        if not url:
            continue
        host = urlparse(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _resolves(host: str) -> bool:
    """走與實際下載相同的 resolver path 判斷 host 是否可解析。"""
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


def wait_for_dns(
    hosts: List[str],
    timeout: float = 120.0,
    initial_backoff: float = 2.0,
    max_backoff: float = 15.0,
    resolves: Callable[[str], bool] = _resolves,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Tuple[bool, List[str]]:
    """阻塞直到所有 host 都能 DNS 解析，或超過 timeout。

    一就緒就立即回傳（典型 post-wake 只需數秒）。回傳 (ready, 仍無法解析的 host)。
    backoff 從 initial_backoff 倍增到 max_backoff 上限，且永不睡超過剩餘時間。
    sleep / now / resolves 可注入以利測試。
    """
    if not hosts:
        return True, []
    deadline = now() + timeout
    backoff = initial_backoff
    while True:
        unresolved = [h for h in hosts if not resolves(h)]
        if not unresolved:
            return True, []
        remaining = deadline - now()
        if remaining <= 0:
            return False, unresolved
        sleep(min(backoff, remaining))
        backoff = min(backoff * 2, max_backoff)


def main() -> int:
    hosts = hosts_from_cities()
    if not hosts:
        print("ℹ️  無 pdf_list_url host 需檢查，跳過網路就緒檢查")
        return 0
    print(f"🌐 網路就緒檢查：等待 DNS 解析 {', '.join(hosts)} ...")
    start = time.monotonic()
    ready, unresolved = wait_for_dns(hosts)
    elapsed = time.monotonic() - start
    if ready:
        print(f"✅ 網路就緒（{elapsed:.1f}s）")
        return 0
    print(f"⚠️  網路就緒檢查逾時（{elapsed:.1f}s），仍無法解析: {', '.join(unresolved)}")
    print("   仍將嘗試繼續，由步驟 1 既有錯誤處理接手")
    return 1


if __name__ == '__main__':
    sys.exit(main())
