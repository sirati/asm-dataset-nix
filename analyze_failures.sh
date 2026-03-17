#!/usr/bin/env bash
# Analyze cross-compilation failures by extracting root-cause errors from nix build logs.
#
# For each failure in .cross-results/, finds the failing derivation and extracts
# the actual error from `nix log`. Groups failures by root cause.
#
# Usage: ./analyze_failures.sh > failures.md
set -euo pipefail

RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)/.cross-results"

# Collect all failures
failures=()
for f in "$RESULTS_DIR"/*; do
  [[ "$f" == *.log ]] && continue
  [[ "$f" == *.buildlog ]] && continue
  [[ "$f" == *.status* ]] && continue
  [[ "$f" == *.lock ]] && continue
  [ ! -f "$f" ] && continue
  [ "$(cat "$f")" = "fail" ] || continue
  failures+=("$(basename "$f")")
done

echo "# Cross-Compilation Failure Analysis"
echo ""
echo "Total failures: ${#failures[@]}"
echo ""

# Temporary directory for per-failure summaries
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

for failure in "${failures[@]}"; do
  compiler="${failure%%__*}"
  arch="${failure#*__}"
  buildlog="$RESULTS_DIR/${failure}.buildlog"

  echo "## ${compiler} / ${arch}" >> "$tmpdir/all.md"
  echo "" >> "$tmpdir/all.md"

  if [ ! -f "$buildlog" ]; then
    echo "No buildlog found." >> "$tmpdir/all.md"
    echo "" >> "$tmpdir/all.md"
    continue
  fi

  # Check if it's an eval error (attribute missing, etc.)
  if grep -q "error: attribute" "$buildlog" 2>/dev/null; then
    # Extract the eval error
    errmsg=$(grep -A2 "error: attribute" "$buildlog" | head -5)
    echo "**Type**: Eval error (missing attribute)" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    echo "$errmsg" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    echo "" >> "$tmpdir/all.md"

    # Categorize
    echo "${compiler}__${arch}" >> "$tmpdir/cat_eval.txt"
    continue
  fi

  # Find the root-failing .drv from the buildlog
  # Pattern: "error: Cannot build '/nix/store/xxx.drv'." followed by "Reason: builder failed"
  root_drv=$(grep -oP "(?<=Cannot build ')[^']+\.drv" "$buildlog" | head -1)

  if [ -z "$root_drv" ]; then
    # Maybe a direct build failure
    root_drv=$(grep -oP '/nix/store/[a-z0-9]+-[^[:space:]]+\.drv' "$buildlog" | head -1)
  fi

  if [ -z "$root_drv" ]; then
    echo "**Type**: Unknown (no .drv found in buildlog)" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    tail -10 "$buildlog" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    echo "" >> "$tmpdir/all.md"
    echo "${compiler}__${arch}" >> "$tmpdir/cat_unknown.txt"
    continue
  fi

  drv_name=$(basename "$root_drv" | sed 's/^[a-z0-9]*-//')

  echo "**Failing drv**: \`${drv_name}\`" >> "$tmpdir/all.md"

  # Try to get the actual build log from nix store
  nixlog=$(nix log "$root_drv" 2>/dev/null || true)

  if [ -z "$nixlog" ]; then
    # Fall back to "Last N log lines" in the buildlog
    echo "**Type**: Build failure (nix log unavailable, using console output)" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    # Extract the "Last N log lines" block
    sed -n '/Last [0-9]* log lines/,/^[^ >]/p' "$buildlog" | head -30 >> "$tmpdir/all.md"
    if [ "$(wc -l < <(sed -n '/Last [0-9]* log lines/,/^[^ >]/p' "$buildlog"))" -eq 0 ]; then
      tail -15 "$buildlog" >> "$tmpdir/all.md"
    fi
    echo '```' >> "$tmpdir/all.md"
    echo "" >> "$tmpdir/all.md"
    echo "${compiler}__${arch}|${drv_name}" >> "$tmpdir/cat_nolog.txt"
    continue
  fi

  # Extract the interesting part of the log: errors, assertions, fatal
  # Look for the first real error line and grab context around it
  error_context=$(echo "$nixlog" | grep -n -i 'error:\|fatal:\|FAILED\|assertion.*failed\|cannot find\|undefined reference\|static.assert\|struct_kernel_stat' | head -5)

  if [ -n "$error_context" ]; then
    # Get line number of first error
    first_err_line=$(echo "$error_context" | head -1 | cut -d: -f1)
    total_lines=$(echo "$nixlog" | wc -l)

    echo "**Type**: Build failure" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    # Show 3 lines before and 10 lines after the first error, capped at 25 lines
    start=$((first_err_line > 3 ? first_err_line - 3 : 1))
    echo "$nixlog" | sed -n "${start},$((start + 25))p" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
  else
    # No obvious error keyword — show last 20 lines
    echo "**Type**: Build failure (no error keyword found)" >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
    echo "$nixlog" | tail -20 >> "$tmpdir/all.md"
    echo '```' >> "$tmpdir/all.md"
  fi
  echo "" >> "$tmpdir/all.md"

  # Categorize by drv name pattern
  echo "${compiler}__${arch}|${drv_name}" >> "$tmpdir/cat_build.txt"
done

# ── Output: grouped summary ──
echo "# Failure Categories"
echo ""

if [ -f "$tmpdir/cat_eval.txt" ]; then
  echo "## Eval Errors (missing nixpkgs attribute)"
  echo ""
  echo "**Affected**: $(cat "$tmpdir/cat_eval.txt" | tr '\n' ', ' | sed 's/,$//')"
  echo ""
fi

if [ -f "$tmpdir/cat_build.txt" ]; then
  echo "## Build Failures (by failing derivation)"
  echo ""
  # Group by drv name
  cut -d'|' -f2 "$tmpdir/cat_build.txt" | sort -u | while read -r drv; do
    cases=$(grep "|${drv}$" "$tmpdir/cat_build.txt" | cut -d'|' -f1 | tr '\n' ', ' | sed 's/,$//')
    echo "- **\`${drv}\`**: ${cases}"
  done
  echo ""
fi

if [ -f "$tmpdir/cat_nolog.txt" ]; then
  echo "## Build Failures (nix log unavailable)"
  echo ""
  cut -d'|' -f2 "$tmpdir/cat_nolog.txt" | sort -u | while read -r drv; do
    cases=$(grep "|${drv}$" "$tmpdir/cat_nolog.txt" | cut -d'|' -f1 | tr '\n' ', ' | sed 's/,$//')
    echo "- **\`${drv}\`**: ${cases}"
  done
  echo ""
fi

echo ""
echo "# Per-Failure Details"
echo ""
cat "$tmpdir/all.md"
