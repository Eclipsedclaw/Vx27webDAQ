#!/usr/bin/env bash
# Build CAEN FELib and CAEN Dig2, the libraries that talk to Digitizer 2.0 boards (V2730, V2740, ...; the
# same ones WaveDump2 uses), into caen/ of this folder, as your normal user (no sudo). They are open source
# (LGPL-3.0) on CAEN's GitHub, github.com/caenspa; the versions are pinned below.
#
#   ./caen_libs.sh [--jobs N] [--with-boost DIR]
#
#   caen/src/       the sources (git, at the pinned commits)
#   caen/lib/       libCAEN_FELib.so and libCAEN_Dig2.so: ALAB DAQ loads them from here (the server sets
#                   LD_LIBRARY_PATH for the DAQ program; build.sh copies them into the build)
#   caen/include/   the headers
#
# Needs git, make, a C11 and C++17 compiler (GCC >= 8), and the Boost headers only (nothing of Boost is
# compiled or needed at run time): the system's (Debian/Ubuntu: libboost-dev; RHEL/Alma: boost-devel), or
# an unpacked Boost >= 1.67 given with --with-boost DIR. Tested here with GCC 12.2 and Boost 1.74.
# --jobs: parallel compile jobs (default 4, never more than 90 % of the physical cores).
# Safe to run again: it builds only what is missing (delete caen/ to build anew).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
FELIB_REPO=https://github.com/caenspa/caen-felib.git
FELIB_COMMIT=ce255f99c73fadb4933efac4f199eeaceb2b11ef     # v1.4.0, 2026-08-28
DIG2_REPO=https://github.com/caenspa/caen-dig2.git
DIG2_COMMIT=042679d782db764ff36101e209c495fa34788d8f      # v1.9, 2026-08-28
prefix="$PWD/caen"
jobs=4 boost=""

fail() { echo "caen_libs.sh: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --jobs) jobs="${2:?--jobs needs a number}"; shift 2 ;;
        --with-boost) boost="${2:?--with-boost needs a folder}"; shift 2 ;;
        -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
        *) fail "unknown argument $1 (see ./caen_libs.sh --help)" ;;
    esac
done
[ "$(id -u)" -ne 0 ] || fail "run it as your normal user, without sudo: everything goes into $prefix"
case "$jobs" in ''|*[!0-9]*) fail "--jobs needs a number" ;; esac
cores="$(lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
limit=$(( cores * 9 / 10 )); [ "$limit" -ge 1 ] || limit=1
[ "$jobs" -le "$limit" ] || jobs="$limit"
for tool in git make gcc g++; do command -v "$tool" >/dev/null || fail "$tool is missing"; done
if [ -n "$boost" ]; then
    [ -f "$boost/boost/version.hpp" ] || fail "$boost has no boost/version.hpp"
elif ! echo '#include <boost/version.hpp>' | g++ -x c++ -E - >/dev/null 2>&1; then
    fail "the Boost headers are missing: install libboost-dev (or boost-devel), or give --with-boost DIR"
fi

fetch() {           # fetch NAME REPO COMMIT: caen/src/NAME at that commit
    local dir="$prefix/src/$1"
    if [ -d "$dir/.git" ] && [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" = "$3" ]; then return; fi
    rm -rf "$dir"
    mkdir -p "$dir"
    git -C "$dir" init -q
    git -C "$dir" remote add origin "$2"
    echo "Downloading $1 ($3) from $2"
    git -C "$dir" fetch -q --depth 1 origin "$3"
    git -C "$dir" checkout -q FETCH_HEAD
}

build() {           # build NAME [configure options]
    local name="$1"; shift
    local dir="$prefix/src/$name"
    echo "Building $name with $jobs jobs (its log: $dir/build.log)"
    (
        cd "$dir"
        # the generated files of the repository are older than their sources after a checkout: keep make
        # from running autotools again
        touch aclocal.m4 configure Makefile.in */Makefile.in 2>/dev/null || true
        ./configure --prefix="$prefix" --disable-assert "$@" CPPFLAGS="-I$prefix/include" LDFLAGS="-L$prefix/lib"
        make -j "$jobs"
        make install
    ) > "$dir/build.log" 2>&1 || { tail -n 30 "$dir/build.log" >&2; fail "$name did not build: see $dir/build.log"; }
}

mkdir -p "$prefix/src"
if [ ! -f "$prefix/lib/libCAEN_FELib.so" ]; then
    fetch caen-felib "$FELIB_REPO" "$FELIB_COMMIT"
    build caen-felib
fi
if [ ! -f "$prefix/lib/libCAEN_Dig2.so" ]; then
    fetch caen-dig2 "$DIG2_REPO" "$DIG2_COMMIT"
    if [ -n "$boost" ]; then build caen-dig2 --with-boost="$boost"; else build caen-dig2; fi
fi
{
    echo "CAEN libraries for ALAB DAQ, built $(date '+%Y-%m-%d %H:%M:%S') by $(id -un) with $(gcc -dumpfullversion 2>/dev/null || gcc -dumpversion)"
    echo "caen-felib $FELIB_COMMIT ($FELIB_REPO)"
    echo "caen-dig2  $DIG2_COMMIT ($DIG2_REPO)"
} > "$prefix/VERSIONS"
# the libraries load and know each other: FELib finds Dig2 by name (libCAEN_Dig2.so) on LD_LIBRARY_PATH
if [ -x ALAB_ENV/bin/python ]; then
    LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ALAB_ENV/bin/python -c "
from caen_felib import lib
print('CAEN FELib', lib.version, 'loads from', '$prefix/lib')"
fi
echo "Done: $prefix/lib has $(cd "$prefix/lib" && ls libCAEN_*.so | tr '\n' ' ')"
