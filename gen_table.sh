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

ARCHS=(i686 aarch64 armv7l-hf armv7l-sf mipsel mips64el ppc32 ppc64 riscv64)

# Minimum/maximum supported compiler versions per target.
# Format: major.minor (0.0 = no restriction).
declare -A MIN_GCC MIN_CLANG MAX_GCC MAX_CLANG
for arch in "${ARCHS[@]}"; do
  MIN_GCC[$arch]="0.0"; MIN_CLANG[$arch]="0.0"
  MAX_GCC[$arch]="0.0"; MAX_CLANG[$arch]="0.0"
done
MIN_GCC[aarch64]="4.8"
MIN_GCC[armv7l-hf]="4.7"
MAX_GCC[armv7l-sf]="4.6"
MIN_CLANG[armv7l-sf]="999.0"  # exclude all clang from armv7l-sf
MIN_GCC[ppc64]="4.8"
MIN_GCC[riscv64]="7.0"
MIN_CLANG[riscv64]="9.0"

# Parse compiler label (e.g. gcc4_8 → family=gcc major=4 minor=8,
# clang3_9 → family=clang major=3 minor=9, gcc15 → family=gcc major=15 minor=0).
# Returns via globals: _family, _major, _minor.
parse_compiler() {
  local label="$1"
  if [[ "$label" =~ ^gcc([0-9]+)_([0-9]+)$ ]]; then
    _family=gcc; _major="${BASH_REMATCH[1]}"; _minor="${BASH_REMATCH[2]}"
  elif [[ "$label" =~ ^gcc([0-9]+)$ ]]; then
    _family=gcc; _major="${BASH_REMATCH[1]}"; _minor=0
  elif [[ "$label" =~ ^clang([0-9]+)_([0-9]+)$ ]]; then
    _family=clang; _major="${BASH_REMATCH[1]}"; _minor="${BASH_REMATCH[2]}"
  elif [[ "$label" =~ ^clang([0-9]+)$ ]]; then
    _family=clang; _major="${BASH_REMATCH[1]}"; _minor=0
  else
    _family=unknown; _major=0; _minor=0
  fi
}

# Check if compiler meets min/max version for target. Returns 0 (true) or 1 (false).
is_supported() {
  local compiler="$1" arch="$2"
  parse_compiler "$compiler"
  local min max
  if [[ "$_family" == "gcc" ]]; then
    min="${MIN_GCC[$arch]}"; max="${MAX_GCC[$arch]}"
  else
    min="${MIN_CLANG[$arch]}"; max="${MAX_CLANG[$arch]}"
  fi
  local min_major="${min%%.*}" min_minor="${min#*.}"
  local max_major="${max%%.*}" max_minor="${max#*.}"
  # Check minimum
  if (( min_major != 0 )); then
    (( _major < min_major )) && return 1
    (( _major == min_major && _minor < min_minor )) && return 1
  fi
  # Check maximum
  if (( max_major != 0 )); then
    (( _major > max_major )) && return 1
    (( _major == max_major && _minor > max_minor )) && return 1
  fi
  return 0
}

# Count results (excluding n/a combos below minimum supported version)
PASS=0; FAIL=0; SKIP=0; NA=0
for compiler in "${COMPILERS[@]}"; do
  for arch in "${ARCHS[@]}"; do
    if ! is_supported "$compiler" "$arch"; then
      NA=$((NA + 1))
      continue
    fi
    outfile="$RESULTS_DIR/${compiler}__${arch}"
    [ -f "$outfile" ] || continue
    case "$(cat "$outfile")" in
      ok)   PASS=$((PASS + 1)) ;;
      fail) FAIL=$((FAIL + 1)) ;;
      skip) SKIP=$((SKIP + 1)) ;;
    esac
  done
done

echo "- **Passed**: $PASS"
echo "- **Failed**: $FAIL"
echo "- **Skipped** (after first failure): $SKIP"
echo "- **Unsupported** (below minimum compiler version): $NA"
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
    if ! is_supported "$compiler" "$arch"; then
      printf " n/a       |"
    else
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
    fi
  done
  echo ""
done
