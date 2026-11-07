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
# inside .test_tmp (another place: set ALAB_DAQ_TEST_TMP, e.g. when this folder is not yours): HOME and
# the XDG folders point into it
export ALAB_DAQ_TEST_TMP="${ALAB_DAQ_TEST_TMP:-$here/.test_tmp}"
mkdir -p "$ALAB_DAQ_TEST_TMP/runtime" "$ALAB_DAQ_TEST_TMP/cache" "$ALAB_DAQ_TEST_TMP/config" \
         "$ALAB_DAQ_TEST_TMP/home" "$ALAB_DAQ_TEST_TMP/tmp"
chmod 700 "$ALAB_DAQ_TEST_TMP/runtime"
export HOME="$ALAB_DAQ_TEST_TMP/home" TMPDIR="$ALAB_DAQ_TEST_TMP/tmp"
export XDG_RUNTIME_DIR="$ALAB_DAQ_TEST_TMP/runtime" XDG_CACHE_HOME="$ALAB_DAQ_TEST_TMP/cache"
export XDG_CONFIG_HOME="$ALAB_DAQ_TEST_TMP/config" XDG_STATE_HOME="$ALAB_DAQ_TEST_TMP/state"
echo "Testing the code in $app/src with $app/ALAB_ENV"
exec "$app/ALAB_ENV/bin/python" -m unittest discover -s "$here/tests" -t "$here" "$@"
