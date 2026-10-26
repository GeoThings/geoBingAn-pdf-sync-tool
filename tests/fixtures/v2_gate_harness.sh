#!/bin/bash
# 測試用 harness：source **正式的** lib/v2_gate.sh 跑真邏輯，不在測試裡抄一份
# （同 watchdog_harness.sh 的理由：抄的那份會和正式的走鐘，等於沒測）。
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$ROOT/lib/v2_gate.sh"

state="$(v2_token_state "${TOKEN_EXIT:-0}")"
echo "TOKEN_STATE=$state"

if v2_usable "$state"; then
    echo "V2_USABLE=1"
    V2_SKIPPED=0
else
    echo "V2_USABLE=0"
    echo "REASON=${V2_SKIP_REASON}"
    V2_SKIPPED=1
fi

echo "FINAL=$(sync_final_status "${HAS_ERROR:-0}" "$V2_SKIPPED")"
