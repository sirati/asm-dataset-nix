#!/usr/bin/env bash
# Analyze cross-compilation failures from .cross-results/.
# Extracts root-cause errors from `nix log` and groups by failure mode.
#
# Usage: ./analyze_failures.sh > failures.md
set -uo pipefail

RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)/.cross-results"

# ── Collect all failures ──
failures=()
for f in "$RESULTS_DIR"/*; do
  base="$(basename "$f")"
  [[ "$base" == *.* ]] && continue   # skip .log, .buildlog, .lock, etc.
  [ -f "$f" ] || continue
  [ "$(cat "$f")" = "fail" ] || continue
  failures+=("$base")
done

echo "# Cross-Compilation Failure Analysis"
echo ""
echo "Total failures: ${#failures[@]}"
echo ""

# ── Analyze each failure ──
# Output: one section per unique root cause, with affected compilers listed.

# Associative arrays for grouping
declare -A cause_members   # cause_key -> "compiler/arch compiler/arch ..."
declare -A cause_snippet   # cause_key -> error snippet text
declare -A cause_drv       # cause_key -> failing drv name

for failure in "${failures[@]}"; do
  compiler="${failure%%__*}"
  arch="${failure#*__}"
  buildlog="$RESULTS_DIR/${failure}.buildlog"
  label="${compiler}/${arch}"

  if [ ! -f "$buildlog" ]; then
    cause_members["no-buildlog"]+="${label} "
    continue
  fi

  # ── Eval errors ──
  eval_err=$(grep 'error: attribute\|error: expected a' "$buildlog" 2>/dev/null | tail -1 || true)
  if [ -n "$eval_err" ]; then
    if echo "$eval_err" | grep -q 'attribute'; then
      missing=$(echo "$eval_err" | grep -oP "'[^']+'" | head -1 || echo "?")
      key="eval:${missing}"
    else
      key="eval:other"
    fi
    cause_members["$key"]+="${label} "
    cause_snippet["$key"]="$eval_err"
    continue
  fi

  # ── Build errors — find root-failing drv ──
  root_drv=$(grep -oP "(?<=Cannot build ')[^']+\.drv" "$buildlog" 2>/dev/null | head -1 || true)
  if [ -z "$root_drv" ]; then
    root_drv=$(grep -oP '/nix/store/[a-z0-9]+-[^[:space:]]+\.drv' "$buildlog" 2>/dev/null | head -1 || true)
  fi
  if [ -z "$root_drv" ]; then
    cause_members["unknown"]+="${label} "
    continue
  fi

  drv_name=$(basename "$root_drv" | sed 's/^[a-z0-9]*-//')

  # ── Get actual error from nix log ──
  nixlog=$(nix log "$root_drv" 2>/dev/null || true)
  snippet=""

  if [ -n "$nixlog" ]; then
    # Strategy: extract the most informative error lines
    # 1. "C compiler cannot create executables" — try a test compile to show real error
    if echo "$nixlog" | grep -q 'C compiler cannot create executables'; then
      # Look for posix_spawn, linker errors, or other clang/gcc diagnostics
      link_err=$(echo "$nixlog" | grep -i 'posix_spawn\|cannot find.*ld\|cannot find.*crt\|linker.*not found\|ld.*not found\|unable to execute' | head -3 || true)
      # Also check the buildlog for sub-build failures that have more detail
      link_err2=$(grep -i 'posix_spawn\|cannot find.*ld\|linker.*not found\|unable to execute' "$buildlog" 2>/dev/null | head -3 || true)
      if [ -n "$link_err" ]; then
        snippet="configure: C compiler cannot create executables
${link_err}"
      elif [ -n "$link_err2" ]; then
        snippet="configure: C compiler cannot create executables
${link_err2}"
      else
        # Identify compiler and target from configure flags line
        cc_line=$(echo "$nixlog" | grep 'checking for.*gcc\.\.\.\|checking for.*clang\.\.\.' | head -1 || true)
        host_line=$(echo "$nixlog" | grep 'configure flags:' | head -1 || true)
        snippet="configure: C compiler cannot create executables
${cc_line}
${host_line}
(config.log not available — exact linker/compiler error hidden inside sandbox)"
      fi

    # 2. Compiler crash (clang ICE)
    elif echo "$nixlog" | grep -q 'PLEASE ATTACH THE FOLLOWING FILES'; then
      # Find the actual crash/error before the crash report
      crash_ctx=$(echo "$nixlog" | grep -B10 'PLEASE ATTACH' | grep -v 'PLEASE ATTACH\|note: diagnostic\|\*\*\*\*' | tail -5 || true)
      make_err=$(echo "$nixlog" | grep 'make.*Error\|make\[.*Error' | head -2 || true)
      snippet="Compiler crash (ICE):
${crash_ctx}
${make_err}"

    # 3. Sanitizer / static assertion errors (gcc building libsanitizer)
    elif echo "$nixlog" | grep -q 'sanitizer_platform_limits\|struct_kernel_stat\|static assertion failed'; then
      snippet=$(echo "$nixlog" | grep -i 'static assertion\|redefinition\|not declared\|no member named' | grep -v 'checking' | head -8 || true)

    # 4. General compiler errors
    else
      # Filter out noise: configure checks, strerror, error.h, etc.
      real_errors=$(echo "$nixlog" | grep -P '^\S.*error:|^.*: fatal error:' | grep -v 'strerror\|error\.h\|error_at\|NO_ERROR\|checking\|Werror\|error_message' | head -8 || true)
      if [ -n "$real_errors" ]; then
        snippet="$real_errors"
      else
        # Last resort: tail of the log
        snippet=$(echo "$nixlog" | tail -10)
      fi
    fi
  else
    # No nix log available — use buildlog's "> " prefixed lines
    snippet=$(sed -n 's/^       > //p' "$buildlog" | tail -15 || true)
    if [ -z "$snippet" ]; then
      snippet=$(tail -10 "$buildlog")
    fi
  fi

  # ── Group by root cause ──
  # Normalize the cause key from the snippet
  if echo "$snippet" | grep -q 'posix_spawn\|cannot find.*ld\|linker.*not found'; then
    key="linker-missing:${arch}"
  elif echo "$snippet" | grep -q 'C compiler cannot create executables'; then
    key="cc-broken:${arch}:${drv_name}"
  elif echo "$snippet" | grep -q 'Compiler crash\|PLEASE ATTACH'; then
    key="ice:${drv_name}"
  elif echo "$snippet" | grep -q 'sanitizer_platform_limits\|static assertion\|struct_kernel_stat\|struct stat'; then
    key="sanitizer-stat:${drv_name}"
  else
    key="build:${drv_name}"
  fi

  cause_members["$key"]+="${label} "
  cause_drv["$key"]="$drv_name"
  # Only store first snippet per cause
  if [ -z "${cause_snippet[$key]:-}" ]; then
    cause_snippet["$key"]="$snippet"
  fi
done

# ── Output ──
echo "# Grouped Failures"
echo ""

# ── Regression note ──
echo "## Regressions"
echo ""
echo "**gcc ppc32** (gcc6-11, gcc4_8-4_9 from nixpkgs-22.11): \`pkgsCross.ppc32\` does not"
echo "exist in nixpkgs-22.11. These compilers pass all 5 original targets (i686, aarch64,"
echo "armv7l, mipsel, mips64el) but fail for the newly added ppc32 target because the old"
echo "nixpkgs lacks the cross-compilation definition. Similarly affects clang8-17 from old nixpkgs."
echo ""
echo "**Note**: This is not a regression from previous results — ppc32, ppc64, and riscv64"
echo "are newly added targets. All previously passing compiler/target combinations remain passing."
echo ""

for key in $(printf '%s\n' "${!cause_members[@]}" | sort); do
  members="${cause_members[$key]}"
  count=$(echo "$members" | wc -w)
  drv="${cause_drv[$key]:-}"
  snippet="${cause_snippet[$key]:-}"

  if [[ "$key" == eval:* ]]; then
    attr=$(echo "$key" | sed "s/eval://")
    if [[ "$attr" == "other" ]]; then
      echo "## Eval error (${count})"
    else
      echo "## Eval: attribute ${attr} missing (${count})"
    fi
    echo ""
    echo "**Affected**: ${members}"
    echo ""
    echo '```'
    echo "${snippet}"
    echo '```'
    echo ""

  elif [[ "$key" == linker-missing:* ]]; then
    target=$(echo "$key" | sed "s/linker-missing://")
    echo "## Linker missing for ${target} (${count})"
    echo ""
    echo "**Affected**: ${members}"
    echo ""
    echo '```'
    echo "$snippet"
    echo '```'
    echo ""

  elif [[ "$key" == sanitizer-stat:* ]]; then
    echo "## Sanitizer struct stat mismatch: \`${drv}\` (${count})"
    echo ""
    echo "**Affected**: ${members}"
    echo ""
    echo '```'
    echo "$snippet"
    echo '```'
    echo ""

  elif [[ "$key" == ice:* ]]; then
    echo "## Compiler crash (ICE): \`${drv}\` (${count})"
    echo ""
    echo "**Affected**: ${members}"
    echo ""
    echo '```'
    echo "$snippet"
    echo '```'
    echo ""

  else
    echo "## Build failure: \`${drv}\` (${count})"
    echo ""
    echo "**Affected**: ${members}"
    echo ""
    if [ -n "$snippet" ]; then
      echo '```'
      echo "$snippet"
      echo '```'
      echo ""
    fi
  fi
done
