#!/usr/bin/env bash
# Re-test all entries currently marked "skip" in .cross-results/.
# Writes results back into the same directory so gen_table.sh and
# test_cross.sh report generation pick them up.
#
# Usage: ./test_skipped.sh
# The script re-execs itself inside a screen session under a systemd cgroup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/.cross-results"
SCREEN_NAME="test-skipped"
MAX_JOBS=12

# ── Step 1: Re-exec inside screen if not already ──────────────────────────
if [ -z "${IN_SCREEN:-}" ]; then
  screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
  sleep 0.5

  echo "Launching inside screen '$SCREEN_NAME' with cgroup limits (60GB RAM, 60GB swap)..."
  echo "Attach with: screen -r $SCREEN_NAME"

  systemd-run --user --scope \
    --property=MemoryMax=64424509440 \
    --property=MemorySwapMax=64424509440 \
    screen -dmS "$SCREEN_NAME" \
      env IN_SCREEN=1 bash "$0" "$@"
  exit 0
fi

# ── Step 2: Inside screen + cgroup ────────────────────────────────────────

trap 'echo "Interrupted, killing all children..."; kill 0; exit 130' INT TERM

if [ ! -d "$RESULTS_DIR" ]; then
  echo "No .cross-results/ directory. Run test_cross.sh first."
  exit 1
fi

# Collect all skip entries
SKIPPED=()
for f in "$RESULTS_DIR"/*; do
  [[ "$f" == *.log ]] && continue
  [[ "$f" == *.buildlog ]] && continue
  [[ "$f" == *.status* ]] && continue
  [[ "$f" == *.lock ]] && continue
  [ ! -f "$f" ] && continue
  [ "$(cat "$f")" = "skip" ] || continue
  base=$(basename "$f")
  # base is "compiler__arch"
  compiler="${base%%__*}"
  arch="${base#*__}"
  SKIPPED+=("$compiler $arch")
done

TOTAL=${#SKIPPED[@]}
if [ "$TOTAL" = "0" ]; then
  echo "No skipped entries found in $RESULTS_DIR."
  exit 0
fi

STATUS_LOG="$RESULTS_DIR/.status.log"
: > "$STATUS_LOG"
LOCK_FILE="$RESULTS_DIR/.status.lock"

echo "Re-testing $TOTAL skipped entries, $MAX_JOBS parallel"
echo ""

tail -f "$STATUS_LOG" &
TAIL_PID=$!

export RESULTS_DIR STATUS_LOG LOCK_FILE
build_one() {
  log_status() {
    flock "$LOCK_FILE" echo "$1" >> "$STATUS_LOG"
  }

  local compiler="$1"
  local arch="$2"
  local attr=".#dataset.x86_64-linux.hello.${arch}.${compiler}-O2-baseline-unhardened"
  local outfile="$RESULTS_DIR/${compiler}__${arch}"
  local label="${compiler}/${arch}"

  log_status "starting: ${label}"
  if output=$(nix build "$attr" --no-link 2>&1); then
    echo "ok" > "$outfile"
    echo "$output" > "${outfile}.buildlog"
    # Remove stale .log from any previous failure
    rm -f "${outfile}.log"
    log_status "OK: ${label}"
  else
    echo "fail" > "$outfile"
    echo "$output" > "${outfile}.buildlog"
    echo "$output" | tail -10 > "${outfile}.log"
    log_status "FAIL: ${label}"
  fi
}
export -f build_one

printf '%s\n' "${SKIPPED[@]}" | xargs -P "$MAX_JOBS" -I {} bash -c 'build_one $@' _ {}

sleep 0.5
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true

echo ""
echo "Done. Summary of re-tested entries:"
OK=0; FAIL=0
for entry in "${SKIPPED[@]}"; do
  read -r compiler arch <<< "$entry"
  f="$RESULTS_DIR/${compiler}__${arch}"
  r=$(cat "$f")
  case "$r" in
    ok)   OK=$((OK+1)) ;;
    fail) FAIL=$((FAIL+1)) ;;
  esac
done
echo "  OK: $OK, FAIL: $FAIL (out of $TOTAL previously skipped)"
echo ""
echo "Run ./gen_table.sh to regenerate the table with updated results."
