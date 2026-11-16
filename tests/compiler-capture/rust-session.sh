#!/usr/bin/env bash
set -euo pipefail
export CARGO_HOME="$PWD/cargo" CARGO_TARGET_DIR="$PWD/target" CARGO_BUILD_JOBS=1
mkdir -p "$CARGO_HOME/registry"
mount --bind "$1" "$CARGO_HOME/registry"
mount -o remount,bind,ro "$CARGO_HOME/registry"
export RUST_TEST_THREADS=1
exec bash --norc --noprofile
