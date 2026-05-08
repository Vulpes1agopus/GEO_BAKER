#!/usr/bin/env bash
# 运行沿海城市修正（低优先级screen后台）
# Usage:
#   bash tools/fix_coastal.sh              # 默认参数
#   bash tools/fix_coastal.sh --dry-run    # 仅检测，不实际修改

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/fix_coastal_${TIMESTAMP}.log"

echo "==============================================="
echo "  Geo Baker - 沿海城市修正"
echo "  $(date)"
echo "  Log: $LOG_FILE"
echo "==============================================="

# Screen 名称
SESSION_NAME="geo_fix_coastal"

# 检查是否已有screen在运行
if screen -list | grep -q "$SESSION_NAME"; then
    echo "警告: screen '$SESSION_NAME' 已在运行"
    echo "可用screen: screen -list"
    read -p "是否要结束已有session并重新开始? (y/N): " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        screen -S "$SESSION_NAME" -X quit
        sleep 1
    else
        echo "退出"
        exit 0
    fi
fi

# 解析参数
DRY_RUN=""
WORKERS=8
CONN=60
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo "模式: 仅检测，不修改任何文件"
fi

# 使用 ionice + nice 低优先级运行
# ionice -c 3: 仅在空闲时运行（最佳效果）
# nice -n 19: 最低CPU优先级
screen -dmS "$SESSION_NAME" bash -c "
    exec > >(tee '$LOG_FILE') 2>&1
    echo \"Screen session: $SESSION_NAME\"
    echo \"PID: \$\$ start: \$(date)\"
    ionice -c 3 nice -n 19 python3 -m geo_baker_pkg \
        --fix-coastal \
        --workers $WORKERS \
        --conn $CONN \
        --pop-threshold 50.0 \
        $DRY_RUN
    EXIT_CODE=\$?
    echo \"完成 at \$(date) with exit code \$EXIT_CODE\"
    if [ \$EXIT_CODE -eq 0 ]; then
        echo '成功! 查看日志: $LOG_FILE'
    else
        echo '失败 (exit \$EXIT_CODE)，查看日志: $LOG_FILE'
    fi
    if [ -z '$DRY_RUN' ]; then
        echo ''
        echo '正在验证修正结果...'
        ionice -c 3 nice -n 19 python3 tools/verify_cities.py 2>&1 | tee -a '$LOG_FILE'
    fi
    read -p '按回车关闭此窗口...'
"

echo "Screen session '$SESSION_NAME' 已启动"
echo "查看日志: tail -f $LOG_FILE"
echo "重新进入: screen -r $SESSION_NAME"
echo "分离screen: Ctrl+A D"
echo "列出演出: screen -list"
