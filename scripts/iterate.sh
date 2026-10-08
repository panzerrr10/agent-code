#!/bin/bash
# ============================================================
# agent-code 自举迭代脚本 v2 (Bootstrapping Iteration)
# 用法: ./iterate.sh [迭代次数]  (默认 1 次)
# 位置: PycharmProjects/agent-code/scripts/iterate.sh
# 更新: 自动拷贝 .env 到副本，方便 5004 直接启动
# ============================================================

set -e
BASE="${AGENT_CODE_BASE:-/home/pz/PycharmProjects}"   # 可用环境变量覆盖
MAIN="$BASE/agent-code"           # 主版本（5003 端口）
COPY="$BASE/agent-code-copy"      # 副本（5004 端口，Agent 修改用）
ARCHIVE_DIR="$BASE/agent-code-archive"
TIMESTAMP=$(date +%Y%m%d_%H%M)
MAX_ITER=${1:-1}

mkdir -p "$ARCHIVE_DIR"

echo "=============================================="
echo "🚀 Agent 自举迭代开始 (共 $MAX_ITER 轮)"
echo "   时间戳:   $TIMESTAMP"
echo "   主版本:   $MAIN  (5003)"
echo "   副版本:   $COPY  (5004)"
echo "   归档目录: $ARCHIVE_DIR"
echo "=============================================="

for ((i=1; i<=MAX_ITER; i++)); do
    echo ""
    echo "========== 🔄 第 $i / $MAX_ITER 轮 =========="

    # ---- Step 1: 目录检查 ----
    echo "[1/7] 检查目录"
    if [ ! -d "$MAIN" ]; then
        echo "❌ 缺少主版本目录: $MAIN"
        exit 1
    fi
    echo "  ✅ 主版本目录存在"

    # ---- Step 2: 备份当前副本（若有） ----
    echo "[2/7] 备份当前 agent-code-copy"
    if [ -d "$COPY" ]; then
        cp -r "$COPY" "$ARCHIVE_DIR/agent-code-copy-snapshot-$TIMESTAMP"
        echo "  ✅ 备份到 agent-code-copy-snapshot-$TIMESTAMP"
    else
        echo "  ℹ️  副本不存在，跳过备份"
    fi

    # ---- Step 3: 归档主版本 ----
    echo "[3/7] 归档当前主版本"
    cp -r "$MAIN" "$ARCHIVE_DIR/agent-code-v$i-$TIMESTAMP"
    echo "  ✅ 归档到 agent-code-v$i-$TIMESTAMP"

    # ---- Step 4: 从主版本生成新副本 ----
    echo "[4/7] 从主版本生成新副本"
    rm -rf "$COPY"
    cp -r "$MAIN" "$COPY"
    # 清理副本里的运行时垃圾（保留 .env！）
    rm -rf "$COPY/data" "$COPY/__pycache__" "$COPY/.trash" \
           "$COPY/.backup" "$COPY/.webview_data" "$COPY/.git" \
           "$COPY/.pytest_cache" "$COPY/.venv"
    echo "  ✅ 副本已生成，运行时垃圾已清理"

    # ---- Step 4.5: 拷贝 .env（方案 A 新增） ----
    echo "[4.5/7] 拷贝主版本 .env → 副本"
    if [ -f "$MAIN/.env" ]; then
        cp "$MAIN/.env" "$COPY/.env"
        echo "  ✅ .env 已拷贝"
    else
        echo "  ⚠️  主版本没有 .env，副本需要自己创建"
    fi

    # ---- Step 5: 副本端口 5003 → 5004 ----
    echo "[5/7] 修改副本端口 5003 → 5004"
    if [ -f "$COPY/src/app.py" ]; then
        sed -i 's/port=5003/port=5004/g' "$COPY/src/app.py"
    fi
    if [ -f "$COPY/src/desktop.py" ]; then
        sed -i 's/5003/5004/g' "$COPY/src/desktop.py"
    fi
    echo "  ✅ 端口已改为 5004"


        # ---- Step 6: 重建副本 venv（用 python3 -m venv 干净重建） ----
    echo "[6/7] 重建副本 venv 并安装依赖"
    if [ -f "$MAIN/requirements.txt" ]; then
        cd "$COPY"
        python3 -m venv .venv
        # shellcheck disable=SC1091
        source .venv/bin/activate
        pip install -q -r requirements.txt
        pip install -q pytest
        deactivate
        cd "$BASE"
        echo "  ✅ venv 已重建 + 依赖已安装"
    else
        echo "  ⚠️  主版本没有 requirements.txt，副本需要手动创建 venv"
    fi

    # ---- Step 7: 验证端口 + .env ----
    echo "[7/7] 验证端口和 .env"
    if grep -q "5003" "$MAIN/src/app.py" 2>/dev/null; then
        echo "  ✅ 主版本 $MAIN 端口 = 5003（src/app.py）"
    else
        echo "  ⚠️  主版本 src/app.py 中未找到 port=5003"
    fi
    if grep -q "5004" "$COPY/src/app.py" 2>/dev/null; then
        echo "  ✅ 副本 $COPY 端口 = 5004（src/app.py）"
    else
        echo "  ⚠️  副本 src/app.py 中未找到 port=5004"
    fi
    [ -f "$COPY/.env" ] && echo "  ✅ 副本 .env 已就绪" || echo "  ⚠️  副本 .env 缺失"

    # ---- 本轮就绪 ----
    echo ""
    echo "========== 📢 第 $i 轮就绪 =========="
    echo "现在打开 http://127.0.0.1:5003，让 agent-code 改以下目录："
    echo "  → $COPY"
    echo ""
    echo "验证通过后，如需合入主版本（按改动文件精准合入，不要全量覆盖）："
    echo "  cp $COPY/src/agent.py $MAIN/src/agent.py"
    echo "  cp $COPY/src/app.py   $MAIN/src/app.py   # 注意：主版本端口是 5003"
    echo ""

    if [ $i -lt $MAX_ITER ]; then
        read -p "⏸  按 Enter 继续下一轮（先确保已合入），或 Ctrl+C 中止：" -r
    fi
done

echo ""
echo "=============================================="
echo "🎉 全部 $MAX_ITER 轮迭代脚本执行完毕！"
echo "   主版本:   $MAIN  (5003)"
echo "   最新副本: $COPY  (5004)"
echo "   归档目录: $ARCHIVE_DIR"
echo "=============================================="
echo ""
[ -f "$MAIN/.env" ] && echo "  ✅ $MAIN/.env 存在" || echo "  ⚠️  $MAIN/.env 不存在"
[ -f "$COPY/.env" ] && echo "  ✅ $COPY/.env 存在（已自动拷贝）" || echo "  ⚠️  $COPY/.env 不存在"