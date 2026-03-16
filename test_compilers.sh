#!/usr/bin/env bash
# Test each compiler by building hello with O2-baseline-unhardened on x86_64
# Each build runs inside a systemd --user cgroup with 60GB RAM + 60GB swap

set -euo pipefail

RESULTS_FILE="$(pwd)/test_results.jsonl"
> "$RESULTS_FILE"

COMPILERS=(
  clang10 clang11 clang12 clang13 clang14
  clang15 clang16 clang17 clang18 clang19
  clang20 clang21 clang22
  clang3_4 clang3_5 clang3_6 clang3_7 clang3_8 clang3_9
  clang4 clang5 clang6 clang7 clang8 clang9
  gcc10 gcc11 gcc12 gcc13 gcc14 gcc15
  gcc4_4 gcc4_5 gcc4_6 gcc4_8 gcc4_9
  gcc5 gcc6 gcc7 gcc8 gcc9
)

TOTAL=${#COMPILERS[@]}
COUNT=0

for compiler in "${COMPILERS[@]}"; do
  COUNT=$((COUNT + 1))
  ATTR=".#dataset.x86_64-linux.hello.x86_64.${compiler}-O2-baseline-unhardened"
  echo "[$COUNT/$TOTAL] Building $compiler ..."

  START=$(date +%s)
  if systemd-run --user --scope \
    --property=MemoryMax=64424509440 \
    --property=MemorySwapMax=64424509440 \
    nix build "$ATTR" --no-link 2>&1; then
    STATUS="ok"
    END=$(date +%s)
    DURATION=$((END - START))
    echo "  -> OK (${DURATION}s)"
  else
    STATUS="fail"
    END=$(date +%s)
    DURATION=$((END - START))
    echo "  -> FAILED (${DURATION}s)"
  fi

  # Capture the build log on failure
  if [ "$STATUS" = "fail" ]; then
    LOG=$(nix log "$ATTR" 2>&1 | tail -30 || echo "no log available")
  else
    LOG=""
  fi

  # Write result as JSON line
  python3 -c "
import json, sys
print(json.dumps({
    'compiler': sys.argv[1],
    'status': sys.argv[2],
    'duration_s': int(sys.argv[3]),
    'log_tail': sys.argv[4]
}))
" "$compiler" "$STATUS" "$DURATION" "$LOG" >> "$RESULTS_FILE"

done

echo ""
echo "Done. Results in $RESULTS_FILE"
