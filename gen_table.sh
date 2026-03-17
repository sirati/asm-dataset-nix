#!/usr/bin/env bash
# Generate a markdown results table from .cross-results/ data.
# Usage: ./gen_table.sh > table.md
set -euo pipefail

RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)/.cross-results"

COMPILERS=(
  gcc15 gcc14 gcc13 gcc12 gcc11 gcc10
  gcc9 gcc8 gcc7 gcc6 gcc5 gcc4_9 gcc4_8 gcc4_6 gcc4_5 gcc4_4
  clang22 clang21 clang20 clang19 clang18
  clang17 clang16 clang15 clang14 clang13 clang12 clang11 clang10
  clang9 clang8 clang7 clang6 clang5
  clang4 clang3_9 clang3_8 clang3_7 clang3_5 clang3_4
)

ARCHS=(i686 aarch64 armv7l mipsel mips64el ppc32 ppc64 riscv64)

# Count results
PASS=0; FAIL=0; SKIP=0
for f in "$RESULTS_DIR"/*; do
  [[ "$f" == *.log ]] && continue
  [[ "$f" == *.buildlog ]] && continue
  [[ "$f" == *.status* ]] && continue
  [[ "$f" == *.lock ]] && continue
  [ ! -f "$f" ] && continue
  case "$(cat "$f")" in
    ok)   PASS=$((PASS + 1)) ;;
    fail) FAIL=$((FAIL + 1)) ;;
    skip) SKIP=$((SKIP + 1)) ;;
  esac
done

echo "- **Passed**: $PASS"
echo "- **Failed**: $FAIL"
echo "- **Skipped** (after first failure): $SKIP"
echo ""

# Table header
printf "| %-10s |" "Compiler"
for arch in "${ARCHS[@]}"; do
  printf " %-9s |" "$arch"
done
echo ""

printf '%s' "|------------|"
for arch in "${ARCHS[@]}"; do
  printf '%s' "-----------|"
done
echo ""

# Table body
for compiler in "${COMPILERS[@]}"; do
  printf '| %-10s |' "$compiler"
  for arch in "${ARCHS[@]}"; do
    outfile="$RESULTS_DIR/${compiler}__${arch}"
    if [ -f "$outfile" ]; then
      case "$(cat "$outfile")" in
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
