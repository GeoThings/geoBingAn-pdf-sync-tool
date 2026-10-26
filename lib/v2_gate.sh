#!/bin/bash
# v2（riskmap.today）的可用性閘門。
#
# 背景（ClickUp z956jf7zrd，slayer 2026-10-25）：geoBingAn_v2_backend
# （riskmap.today API 與 riskmap.tw 前端）要關閉。pdf-sync 有好幾步直接打 v2。
#
# 要守住的不變量：**步驟 1（台北市網站 → Google 雲端硬碟）不可以被 v2 的狀態擋住。**
# 它用的是 Google 服務帳號憑證與市府網站，完全不需要 riskmap 的 token；而新版
# riskmap.taipei 的資料源就是那個雲端硬碟，所以這一步停掉的代價最大。
#
# 原本的寫法是 refresh token 過期就 `exit 1`，位置在步驟 1 之前——等於讓一個
# 步驟 1 用不到的憑證，擋掉唯一不能停的那一步。token 到期日是 2026-10-30 18:03，
# 而關站後就拿不到新的 token，所以這是有硬期限的。
#
# 這個檔只放「判斷」，不做副作用，才能被測試 source 進去跑真邏輯
# （同 lib/watchdog.sh 的做法：測試不抄一份邏輯，抄的那份會和正式的走鐘）。

# v2 相關步驟被跳過時的理由，給日誌與狀態記錄用
V2_SKIP_REASON=""

# 把 token 檢查的 exit code 翻成一個有名字的狀態。
#   0=有效 1=快過期 2=已過期 其他=判讀不出來
v2_token_state() {
    case "$1" in
        0) echo "ok" ;;
        1) echo "warning" ;;
        2) echo "expired" ;;
        *) echo "unknown" ;;
    esac
}

# v2 相關步驟（上傳、名稱比對、追蹤報告）能不能跑。
# 回 0 = 能跑；回 1 = 不能跑，理由在 V2_SKIP_REASON。
v2_usable() {
    local token_state="$1"
    V2_SKIP_REASON=""
    case "$token_state" in
        expired)
            V2_SKIP_REASON="riskmap refresh token 已過期（riskmap.today 即將關閉，見 ClickUp z956jf7zrd）"
            return 1
            ;;
        unknown)
            V2_SKIP_REASON="token 檢查結果判讀不出來（exit code 不是 0/1/2）"
            return 1
            ;;
    esac
    return 0
}

# 本輪的最終狀態。
#   有錯 → failure
#   沒錯但 v2 步驟被跳過 → partial（不是 success：success 會把「報告已經停止更新」
#     藏起來；也不是 failure：步驟 1 其實成功了，而那是現在最重要的一步）
#   其餘 → success
sync_final_status() {
    local has_error="$1" v2_skipped="$2"
    if [ "${has_error:-0}" -ne 0 ]; then
        echo "failure"
        return
    fi
    if [ "${v2_skipped:-0}" -ne 0 ]; then
        echo "partial"
        return
    fi
    echo "success"
}
