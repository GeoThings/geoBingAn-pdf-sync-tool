#!/bin/bash
# 完整 PDF 上傳流程
# 包含 AI 分析 + Report 建立 + 附件上傳

set -e  # 遇到錯誤立即停止

echo "════════════════════════════════════════════════════════════"
echo "  完整 PDF 上傳流程"
echo "════════════════════════════════════════════════════════════"
echo ""

# 檢查 Docker 是否運行
echo "🔍 檢查 Docker 環境..."
if ! docker ps | grep -q geobingan-web; then
    echo "❌ Docker 容器未運行"
    echo "   請先啟動 Docker: cd ../geoBingAn_v2_backend && ./scripts/local-deploy.sh start"
    exit 1
fi
echo "✅ Docker 容器正在運行"
echo ""

# 步驟 1: AI 分析和建立 Report
echo "════════════════════════════════════════════════════════════"
echo "  步驟 1/2: 上傳 PDF 並進行 AI 分析"
echo "════════════════════════════════════════════════════════════"
echo ""

if ! python3 upload_pdfs.py; then
    echo ""
    echo "❌ 步驟 1 失敗：PDF 上傳和 AI 分析"
    exit 1
fi

echo ""
echo "✅ 步驟 1 完成：Report 已建立"
echo ""

# 等待一下，確保資料庫已更新
sleep 2

# 步驟 2: 補充 PDF 附件到 S3
echo "════════════════════════════════════════════════════════════"
echo "  步驟 2/2: 補充 PDF 附件到 S3"
echo "════════════════════════════════════════════════════════════"
echo ""

if ! python3 upload_attachments.py; then
    echo ""
    echo "⚠️  步驟 2 失敗：附件上傳"
    echo "   Reports 已建立，但附件可能未完全上傳"
    echo "   您可以稍後單獨執行: python3 upload_attachments.py"
    exit 1
fi

echo ""
echo "✅ 步驟 2 完成：附件已上傳到 S3"
echo ""

# 完成
echo "════════════════════════════════════════════════════════════"
echo "  ✅ 完整流程完成"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "📊 下一步："
echo "  1. 查看 Django Admin: http://localhost:8000/admin/reports/report/"
echo "  2. 查看附件記錄: http://localhost:8000/admin/reports/fileattachment/"
echo "  3. 檢查 S3（如果配置了）"
echo ""
