#!/bin/sh
set -eu

runtime_root=/opt/sfincs-cpu/rootfs
runtime_lib="$runtime_root/usr/local/lib:$runtime_root/usr/lib/x86_64-linux-gnu:$runtime_root/lib/x86_64-linux-gnu"

if [ -z "${OMP_NUM_THREADS:-}" ]; then
    OMP_NUM_THREADS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
fi

export OMP_NUM_THREADS
export LD_LIBRARY_PATH="$runtime_lib"

exec "$runtime_root/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" \
    --library-path "$runtime_lib" \
    "$runtime_root/usr/local/bin/sfincs" "$@"
