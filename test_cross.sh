#!/usr/bin/env bash
# Test cross-compilation of hello for each working compiler across all cross targets.
# Up to 12 compilers run in parallel. Newest compilers first.
#
# Usage: ./test_cross.sh [--no-skip]
#   default:    stop at first failure per compiler, mark remaining archs as SKIP
#   --no-skip:  test every arch independently, never skip
#
# The script re-execs itself inside a screen session under a systemd cgroup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT="$SCRIPT_DIR/report_hello_cross.md"
RESULTS_DIR="$SCRIPT_DIR/.cross-results"
SCREEN_NAME="test-cross"
MAX_JOBS=12
SKIP_ON_FAIL=1

for arg in "$@"; do
  case "$arg" in
    --no-skip) SKIP_ON_FAIL=0 ;;
  esac
done

# ── Step 1: Re-exec inside screen if not already ──────────────────────────
if [ -z "${IN_SCREEN:-}" ]; then
  # Kill any previous screen with this name
  screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
  sleep 0.5

  echo "Launching inside screen '$SCREEN_NAME' with cgroup limits (60GB RAM, 60GB swap)..."
  echo "Attach with: screen -r $SCREEN_NAME"
  echo "Report will be written to: $REPORT"

  systemd-run --user --scope \
    --property=MemoryMax=64424509440 \
    --property=MemorySwapMax=64424509440 \
    screen -dmS "$SCREEN_NAME" \
      env IN_SCREEN=1 bash "$0" "$@"
  exit 0
fi

# ── Step 2: We are inside screen + cgroup ──────────────────────────────────

# On SIGINT/SIGTERM, kill entire process group so all children (nix build) die too
trap 'echo "Interrupted, killing all children..."; kill 0; exit 130' INT TERM

# Compilers that passed the native hello test, newest first
COMPILERS=(
  gcc15 gcc14 gcc13 gcc12 gcc11 gcc10
  gcc9 gcc8 gcc7 gcc6 gcc5 gcc4_9 gcc4_8 gcc4_6 gcc4_5 gcc4_4
  clang22 clang21 clang20 clang19 clang18
  clang17 clang16 clang15 clang14 clang13 clang12 clang11 clang10
  clang9 clang8 clang7 clang6 clang5
  clang4 clang3_9 clang3_8 clang3_7 clang3_5 clang3_4
)

ARCHS=(i686 aarch64 armv7l-hf armv7l-sf mipsel mips64el ppc32 ppc64 riscv64)

rm -rf "$RESULTS_DIR"
mkdir -p "$RESULTS_DIR"

TOTAL_COMPILERS=${#COMPILERS[@]}
TOTAL_ARCHS=${#ARCHS[@]}

# Status log file — workers append, tail -f displays
STATUS_LOG="$RESULTS_DIR/.status.log"
: > "$STATUS_LOG"
LOCK_FILE="$RESULTS_DIR/.status.lock"

if [ "$SKIP_ON_FAIL" = "1" ]; then
  echo "Testing $TOTAL_COMPILERS compilers x $TOTAL_ARCHS archs (stop-on-fail per compiler), $MAX_JOBS parallel"
else
  echo "Testing $TOTAL_COMPILERS compilers x $TOTAL_ARCHS archs (no-skip mode), $MAX_JOBS parallel"
fi
echo ""

# ── Status printer (tail -f the status log) ──────────────────────────────
tail -f "$STATUS_LOG" &
TAIL_PID=$!

# Helper: atomic append to status log
log_status() {
  flock "$LOCK_FILE" echo "$1" >> "$STATUS_LOG"
}

# ── Worker: test one compiler across all archs ───────────────────────────
export RESULTS_DIR STATUS_LOG LOCK_FILE SKIP_ON_FAIL
worker() {
  log_status() {
    flock "$LOCK_FILE" echo "$1" >> "$STATUS_LOG"
  }

  local compiler="$1"

  for arch in i686 aarch64 armv7l-hf armv7l-sf mipsel mips64el ppc32 ppc64 riscv64; do
    local attr=".#dataset.x86_64-linux.hello.${arch}.${compiler}-O2-baseline-unhardened"
    local outfile="$RESULTS_DIR/${compiler}__${arch}"
    local label="${compiler} hello with target arch ${arch}"

    log_status "starting: ${label}"
    if output=$(nix build "$attr" --no-link 2>&1); then
      echo "ok" > "$outfile"
      echo "$output" > "${outfile}.buildlog"
      log_status "success: ${label}"
    else
      echo "fail" > "$outfile"
      echo "$output" > "${outfile}.buildlog"
      echo "$output" | tail -10 > "${outfile}.log"
      log_status "failed: ${label}"
      if [ "$SKIP_ON_FAIL" = "1" ]; then
        # Mark remaining archs as skipped
        local skip=0
        for a2 in i686 aarch64 armv7l-hf armv7l-sf mipsel mips64el ppc32 ppc64 riscv64; do
          if [ "$skip" = "1" ]; then
            echo "skip" > "$RESULTS_DIR/${compiler}__${a2}"
            log_status "skipped: ${compiler} hello with target arch ${a2}"
          fi
          [ "$a2" = "$arch" ] && skip=1
        done
        return
      fi
    fi
  done
}
export -f worker

# ── Run compilers in parallel via xargs ───────────────────────────────────
printf '%s\n' "${COMPILERS[@]}" | xargs -P "$MAX_JOBS" -I {} bash -c 'worker "$@"' _ {}

# Stop the tail printer
sleep 0.5
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true

echo ""
echo "All builds finished."
echo ""

# ── Step 3: Generate report ───────────────────────────────────────────────

{
  echo "# Cross-Compilation Test Report: hello (O2-baseline-unhardened)"
  echo ""
  echo "Host: x86_64-linux"
  echo "Constraints: systemd cgroup, 60GB RAM + 60GB swap, $MAX_JOBS parallel compilers"
  echo "Each compiler tries archs in order, stops on first failure."
  echo ""

  # Count results
  PASS=0
  FAIL=0
  SKIP=0
  for f in "$RESULTS_DIR"/*; do
    [[ "$f" == *.log ]] && continue
    [[ "$f" == *.buildlog ]] && continue
    [[ "$f" == *.status* ]] && continue
    [[ "$f" == *.lock ]] && continue
    [ ! -f "$f" ] && continue
    result=$(cat "$f")
    case "$result" in
      ok)   PASS=$((PASS + 1)) ;;
      fail) FAIL=$((FAIL + 1)) ;;
      skip) SKIP=$((SKIP + 1)) ;;
    esac
  done

  echo "## Summary"
  echo ""
  echo "- **Passed**: $PASS"
  echo "- **Failed**: $FAIL"
  echo "- **Skipped** (after first failure): $SKIP"
  echo ""

  # Table header
  echo "## Results Matrix"
  echo ""
  printf "| %-10s |" "Compiler"
  for arch in "${ARCHS[@]}"; do
    printf " %-9s |" "$arch"
  done
  echo ""

  printf "|------------|"
  for arch in "${ARCHS[@]}"; do
    printf "-----------|"
  done
  echo ""

  # Table body
  for compiler in "${COMPILERS[@]}"; do
    printf "| %-10s |" "$compiler"
    for arch in "${ARCHS[@]}"; do
      outfile="$RESULTS_DIR/${compiler}__${arch}"
      if [ -f "$outfile" ]; then
        result=$(cat "$outfile")
        case "$result" in
          ok)   printf " OK        |" ;;
          fail) printf " FAIL      |" ;;
          skip) printf " SKIP      |" ;;
          *)    printf " ???       |" ;;
        esac
      else
        printf " N/A       |"
      fi
    done
    echo ""
  done

  echo ""

  # Failure details
  HAS_FAILURES=0
  for f in "$RESULTS_DIR"/*.log 2>/dev/null; do
    [ -f "$f" ] && HAS_FAILURES=1 && break
  done

  if [ "$HAS_FAILURES" = "1" ]; then
    echo "## Failure Details"
    echo ""
    for compiler in "${COMPILERS[@]}"; do
      for arch in "${ARCHS[@]}"; do
        logfile="$RESULTS_DIR/${compiler}__${arch}.log"
        if [ -f "$logfile" ]; then
          echo "### ${compiler} / ${arch}"
          echo ""
          echo '```'
          cat "$logfile"
          echo '```'
          echo ""
        fi
      done
    done
  fi

} > "$REPORT"

echo "Report written to $REPORT"
echo ""

# Quick summary to terminal
PASS=0
FAIL=0
SKIP=0
for f in "$RESULTS_DIR"/*; do
  [[ "$f" == *.log ]] && continue
  [[ "$f" == *.buildlog ]] && continue
  [[ "$f" == *.status* ]] && continue
  [[ "$f" == *.lock ]] && continue
  [ ! -f "$f" ] && continue
  result=$(cat "$f")
  case "$result" in
    ok)   PASS=$((PASS + 1)) ;;
    fail) FAIL=$((FAIL + 1)) ;;
    skip) SKIP=$((SKIP + 1)) ;;
  esac
done
echo "=== SUMMARY: $PASS passed, $FAIL failed, $SKIP skipped ==="
