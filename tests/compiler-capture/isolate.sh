#!/usr/bin/env bash
# Run only inside a new user, mount, PID and network namespace. All writable
# experiment storage lives on one bounded tmpfs; /nix/store retains its name.
set -euo pipefail
if [[ ${1:-} != --inside ]]; then
    network_args=(--net)
    memory_bytes=${CAPTURE_TEST_MEMORY_BYTES:-1073741824}
    if [[ ${CAPTURE_PREREQUISITE_NETWORK:-0} = 1 ]]; then network_args=(); fi
    if [[ ${CAPTURE_PREREQUISITE_NETWORK:-0} = 1 ]]; then memory_bytes=${CAPTURE_PREREQUISITE_MEMORY_BYTES:-1073741824}; fi
    [[ $memory_bytes = 1073741824 || $memory_bytes = 2147483648 || $memory_bytes = 4294967296 ]]
    exec systemd-run --user --scope --quiet --collect --unit="${CAPTURE_SCOPE:?}" \
        -p "MemoryMax=$memory_bytes" -p MemorySwapMax=3G -p CPUQuota=100% \
        unshare --user --map-root-user --mount --pid --fork --mount-proc "${network_args[@]}" \
        --propagation private "$0" --inside "$@"
fi
shift
mountpoint=$1
registration=$2
shift 2
[[ -d $mountpoint && -f $registration && $# -gt 0 ]]
if [[ ${CAPTURE_PREREQUISITE_NETWORK:-0} != 1 ]]; then ip link set lo up; fi
ulimit -c 0
scratch_bytes=${CAPTURE_SCRATCH_BYTES:-3221225472}
inode_limit=${CAPTURE_INODES:-131072}
[[ $scratch_bytes =~ ^[0-9]+$ && $inode_limit =~ ^[0-9]+$ ]]
(( scratch_bytes >= 67108864 && scratch_bytes <= 3221225472 ))
(( inode_limit >= 8192 && inode_limit <= 131072 ))
mount -t tmpfs -o "size=$scratch_bytes,nr_inodes=$inode_limit,mode=0700" capture "$mountpoint"
mkdir -p "$mountpoint"/{lower,upper,work,state,tmp,cache,home,build,logs,runtime}
mount --bind /nix/store "$mountpoint/lower"
mount -o remount,bind,ro "$mountpoint/lower"
if [[ -n ${CAPTURE_MASK_PATHS_JSON:-} ]]; then
    "${CAPTURE_MASK_PYTHON:?}" - "$mountpoint/upper" "$CAPTURE_MASK_PATHS_JSON" <<'PY'
import json,os,pathlib,sys
upper=pathlib.Path(sys.argv[1])
for value in json.load(open(sys.argv[2])):
    path=pathlib.Path(value)
    if path.parent!=pathlib.Path('/nix/store') or not path.name:
        raise RuntimeError('invalid offline store mask')
    marker=upper/path.name
    marker.touch()
    os.setxattr(marker,'user.overlay.whiteout',b'')
os.setxattr(upper,'user.overlay.opaque',b'x')
PY
fi
mount -t overlay overlay -o \
    "lowerdir=$mountpoint/lower,upperdir=$mountpoint/upper,workdir=$mountpoint/work,userxattr" /nix/store
if [[ -n ${CAPTURE_MASK_PATHS_JSON:-} ]]; then
    "${CAPTURE_MASK_PYTHON:?}" - "$CAPTURE_MASK_PATHS_JSON" <<'PY'
import json,os,sys
if any(os.path.lexists(path) for path in json.load(open(sys.argv[1]))):
    raise RuntimeError('kernel did not hide the offline transfer delta')
PY
fi
mount --bind "$mountpoint/state" /nix/var/nix
mount --bind "$mountpoint/logs" /nix/var/log/nix
mount --bind "$mountpoint/tmp" /tmp
export NIX_REMOTE=local
export NIX_CONFIG="build-users-group =
sandbox = true
require-drop-supplementary-groups = false
max-jobs = 1
cores = 1
substituters =
builders =
build-dir = $mountpoint/build
min-free = 0
max-free = 0
experimental-features = nix-command flakes"
export TMPDIR=/tmp XDG_CACHE_HOME="$mountpoint/cache" XDG_RUNTIME_DIR="$mountpoint/runtime"
# The home used by the experiment is distinct from the invoking user's home.
export HOME="$mountpoint/home"
nix-store --load-db < "$registration"
cd "$mountpoint"
exec "$@"
