#!/usr/bin/env bash
# Run the automatic tests: no browser, no hardware (simulated boards).
#   ./run_tests.sh                         all tests, on this source folder
#   ./run_tests.sh -k web                  only tests whose name contains "web"
#   ./run_tests.sh --build /opt/NAME       all tests on a build made by build.sh (its code and its environment)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app="$here"
if [ "${1:-}" = "--build" ]; then
    app="$(cd "${2:?--build needs the build folder}" && pwd)"
    shift 2
fi
export PYTHONPATH="$app/src${PYTHONPATH:+:$PYTHONPATH}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
# test files, the save folders, the server's settings and accounts, and every runtime and cache file stay
# inside .test_tmp (another place: set VX27WEBDAQ_TEST_TMP, e.g. when this folder is not yours): HOME and
# the XDG folders point into it
export VX27WEBDAQ_TEST_TMP="${VX27WEBDAQ_TEST_TMP:-$here/.test_tmp}"
mkdir -p "$VX27WEBDAQ_TEST_TMP/runtime" "$VX27WEBDAQ_TEST_TMP/cache" "$VX27WEBDAQ_TEST_TMP/config" \
         "$VX27WEBDAQ_TEST_TMP/home" "$VX27WEBDAQ_TEST_TMP/tmp"
chmod 700 "$VX27WEBDAQ_TEST_TMP/runtime"
export HOME="$VX27WEBDAQ_TEST_TMP/home" TMPDIR="$VX27WEBDAQ_TEST_TMP/tmp"
export XDG_RUNTIME_DIR="$VX27WEBDAQ_TEST_TMP/runtime" XDG_CACHE_HOME="$VX27WEBDAQ_TEST_TMP/cache"
export XDG_CONFIG_HOME="$VX27WEBDAQ_TEST_TMP/config" XDG_STATE_HOME="$VX27WEBDAQ_TEST_TMP/state"
echo "Testing the code in $app/src with $app/VX27_ENV"
exec "$app/VX27_ENV/bin/python" -m unittest discover -s "$here/tests" -t "$here" "$@"
