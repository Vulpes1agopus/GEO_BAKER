#!/usr/bin/env bash
# Background bake runner for ocean tiles and water edge computation
# 低优先级后台烘焙脚本，用于完成海洋区块及水体边缘计算
#
# Usage / 用法:
#   bash tools/bake_background.sh              # Bake remaining ocean/land tiles
#   bash tools/bake_background.sh --retry      # Retry error tiles
#   bash tools/bake_background.sh --region 70 20 140 55  # Bake specific region

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/bake_bg_${TIMESTAMP}.log"
MODE="${1:-}"
LOCK_FILE="$LOG_DIR/bake_background.lock"
PYTHON_BIN="${PYTHON_BIN:-python3}"
WORKERS="${WORKERS:-4}"
CONN="${CONN:-30}"
TILE_TIMEOUT="${TILE_TIMEOUT:-900}"

run_low_priority() {
    if command -v ionice >/dev/null 2>&1; then
        ionice -c 3 nice -n 19 "$@"
    else
        nice -n 19 "$@"
    fi
}

# Run with low priority (ionice + nice) / 低优先级运行
echo "Starting background bake at $(date)" | tee "$LOG_FILE"
echo "Log: $LOG_FILE"

if [ -e "$LOCK_FILE" ]; then
    echo "Another bake_background job is running (lock: $LOCK_FILE)" | tee -a "$LOG_FILE"
    echo "If stale, remove lock manually and retry." | tee -a "$LOG_FILE"
    exit 1
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

if [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
    cat <<EOF
Usage:
  bash tools/bake_background.sh
  bash tools/bake_background.sh --retry
  bash tools/bake_background.sh --region <lon_min> <lat_min> <lon_max> <lat_max>

Env overrides:
  WORKERS=4 CONN=30 TILE_TIMEOUT=900 PYTHON_BIN=python3
EOF
    exit 0
fi

if [ "$MODE" = "--retry" ]; then
    echo "Mode: retry errors / 模式: 重试错误" | tee -a "$LOG_FILE"
    run_low_priority "$PYTHON_BIN" -m geo_baker_pkg --retry-errors --workers "$WORKERS" --conn "$CONN" --tile-timeout "$TILE_TIMEOUT" 2>&1 | tee -a "$LOG_FILE"
elif [ "$MODE" = "--region" ] && [ $# -ge 5 ]; then
    shift
    echo "Mode: region ($*) / 模式: 区域" | tee -a "$LOG_FILE"
    run_low_priority "$PYTHON_BIN" -m geo_baker_pkg --bbox "$@" --workers "$WORKERS" --conn "$CONN" --tile-timeout "$TILE_TIMEOUT" 2>&1 | tee -a "$LOG_FILE"
else
    echo "Mode: global (low priority) / 模式: 全球(低优先级)" | tee -a "$LOG_FILE"
    run_low_priority "$PYTHON_BIN" -m geo_baker_pkg --global --workers "$WORKERS" --conn "$CONN" --tile-timeout "$TILE_TIMEOUT" 2>&1 | tee -a "$LOG_FILE"
fi

echo "Completed at $(date)" | tee -a "$LOG_FILE"
