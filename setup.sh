#!/usr/bin/env bash
# One-time setup of this source folder, as your normal user (no sudo). It makes:
#   wheels/        the Python packages of requirements.lock, downloaded from https://pypi.org (never a mirror);
#                  build.sh installs from these files only, so a build needs no network
#   ALAB_ENV/  the Python environment for running (./run_web.sh) and testing (./run_tests.sh) here
# Safe to run again; it only adds what is missing.
set -euo pipefail
cd "$(dirname "$0")"
venv=ALAB_ENV
py="${PYTHON:-python3}"                        # another Python: PYTHON=python3.12 ./setup.sh
if [ "$(id -u)" -eq 0 ]; then
    echo "Run setup.sh as your normal user, without sudo: the files of this folder should stay yours." >&2
    exit 1
fi

# an environment remembers the folder it was made in (activate, pip): make it again if this folder moved
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "ALAB DAQ needs Python 3.11 or newer, not $("$py" -V 2>&1) (Ubuntu 24.04 has 3.12; on 22.04 install python3.11 and run PYTHON=python3.11 ./setup.sh)" >&2; exit 1; }
"$py" -c 'import ensurepip' 2>/dev/null || { echo "Python's venv module is missing: on Ubuntu run  sudo apt install python3-venv" >&2; exit 1; }
if [ -x "$venv/bin/python" ] && ! grep -qF "$PWD/$venv" "$venv/bin/activate"; then
    echo "$venv/ was made in another folder; making it again here"
    rm -rf "$venv"
fi
if [ ! -x "$venv/bin/python" ]; then
    echo "Creating the Python environment $venv/"
    "$py" -m venv --prompt "$venv" "$venv"
fi
pip=("$venv/bin/python" -m pip --disable-pip-version-check)
# no pip.conf or PIP_* variable may add a mirror: the packages come from https://pypi.org only
export PIP_CONFIG_FILE=/dev/null
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_NO_INDEX

# the packages of requirements.lock, downloaded only when wheels/ lacks one of them
mkdir -p wheels
if ! "${pip[@]}" download --no-cache-dir --quiet --no-index --find-links wheels --only-binary=:all: --no-deps \
        --dest wheels -r requirements.lock >/dev/null 2>&1; then
    echo "Downloading the packages of requirements.lock from https://pypi.org"
    "${pip[@]}" download --no-cache-dir --quiet --retries 20 --timeout 60 --index-url https://pypi.org/simple \
        --only-binary=:all: --no-deps --dest wheels -r requirements.lock
fi
"${pip[@]}" install --no-cache-dir --quiet --no-index --find-links wheels -r requirements.lock

# caen_felib loads libCAEN_FELib.so when it is imported: here only its presence is checked
"$venv/bin/python" -c "import aiohttp, uproot, numpy, importlib.metadata as m; print('aiohttp', aiohttp.__version__, '| uproot', uproot.__version__, '| numpy', numpy.__version__, '| caen-felib', m.version('caen-felib'))"
if [ ! -f caen/lib/libCAEN_FELib.so ] || [ ! -f caen/lib/libCAEN_Dig2.so ]; then
    echo "The CAEN libraries are not built yet: ./caen_libs.sh (needed for the real boards; the simulator runs without them)"
fi
echo "Setup done. Try the server here with ./run_web.sh serve; build it for every user with: sudo ./build.sh (see its --help)"
