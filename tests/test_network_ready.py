"""Tests for network_ready — post-wake DNS readiness probe (#59)."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from network_ready import hosts_from_cities, wait_for_dns


class TestHostsFromCities:
    def test_extracts_enabled_pdf_host(self):
        # 刻意讀真實 cities.json 作 config 整合 smoke：驗 probe 真的有接上現行設定。
        # 若 taipei 改名/停用此 test 會紅 —— 那正是要提醒「probe host 清單也要跟著改」。
        hosts = hosts_from_cities()
        assert 'www-ws.gov.taipei' in hosts

    def test_dedup_and_no_disabled(self):
        hosts = hosts_from_cities()
        assert len(hosts) == len(set(hosts))  # 去重
        # CSV 範例城市 disabled 且無 pdf_list_url，不應出現
        assert '' not in hosts


class _Clock:
    """可注入的假時鐘：sleep 推進 now，不真的等待。"""
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.slept.append(secs)
        self.t += secs


class TestWaitForDns:
    def test_empty_hosts_ready_immediately(self):
        ready, unresolved = wait_for_dns([])
        assert ready is True
        assert unresolved == []

    def test_all_resolve_first_try_no_sleep(self):
        clock = _Clock()
        ready, unresolved = wait_for_dns(
            ['a', 'b'], resolves=lambda h: True,
            sleep=clock.sleep, now=clock.now,
        )
        assert ready is True
        assert unresolved == []
        assert clock.slept == []  # 一就緒不睡

    def test_becomes_ready_after_a_few_tries(self):
        clock = _Clock()
        calls = {'n': 0}

        def resolves(host):
            # 前兩輪都失敗，第三輪起成功
            calls['n'] += 1
            return calls['n'] > 4  # 2 hosts * 2 failed rounds = 4 calls

        ready, unresolved = wait_for_dns(
            ['a', 'b'], timeout=120, initial_backoff=2, max_backoff=15,
            resolves=resolves, sleep=clock.sleep, now=clock.now,
        )
        assert ready is True
        assert unresolved == []
        assert clock.slept == [2, 4]  # backoff 倍增

    def test_timeout_returns_unresolved(self):
        clock = _Clock()
        ready, unresolved = wait_for_dns(
            ['down.example'], timeout=10, initial_backoff=2, max_backoff=15,
            resolves=lambda h: False, sleep=clock.sleep, now=clock.now,
        )
        assert ready is False
        assert unresolved == ['down.example']

    def test_backoff_capped_and_never_oversleeps_deadline(self):
        clock = _Clock()
        ready, unresolved = wait_for_dns(
            ['down'], timeout=9, initial_backoff=2, max_backoff=4,
            resolves=lambda h: False, sleep=clock.sleep, now=clock.now,
        )
        assert ready is False
        # 2 -> 4 (capped) -> 剩 3 (不超過 deadline) -> 逾時
        assert clock.slept == [2, 4, 3]
        assert sum(clock.slept) == 9
