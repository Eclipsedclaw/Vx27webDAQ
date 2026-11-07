#!/usr/bin/env bash
# The ALAB DAQ web server and its accounts, from this folder: the source folder (after ./setup.sh) or a
# build made by build.sh (whose systemd service starts it so).
#
#   ./run_web.sh [serve] [--port 8770]               the server, on this computer's localhost only
#   ./run_web.sh user add NAME --role admin          accounts: user list | add | password | role | remove
#
# Open the page through port forwarding: ssh -L 8770:localhost:8770 THIS-COMPUTER, then http://localhost:8770
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"     # also when started through a symlink
if [ ! -x "$here/ALAB_ENV/bin/python" ]; then
    echo "No Python environment in $here: run ./setup.sh there (source folder) or build.sh again." >&2
    exit 1
fi
export PYTHONPATH="$here/src"
# one thread for numpy's math libraries, so the server leaves the cores to others
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}" OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
exec "$here/ALAB_ENV/bin/python" -m alab_daq.web "$@"
