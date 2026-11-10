#!/usr/bin/env bash
# Build the Vx27webDAQ web server into a folder of this computer, add the command vx27webdaq, and run
# the server as a systemd service. Run ./setup.sh first, as your normal user; then, the first time:
#
#   sudo ./build.sh --service-user ACCOUNT --admin NAME      (ACCOUNT: the one that writes the data folder)
#
# and afterwards just  sudo ./build.sh  (the service keeps its account and port).
#
#   FOLDER           a name (the build goes to /opt/NAME) or an absolute path; default: Vx27webDAQ
#   --service-user   the account that runs the server and the DAQ program (it must write the data folder);
#                    needed the first time, then taken from the installed service
#   --port N         where the server listens, on localhost only; default 8770 (or the installed one)
#   --admin NAME     make the first admin account NAME (asks for its password) if there is no account yet
#   --no-service     leave systemd alone: start the server yourself with FOLDER/run_web.sh
#   --bin-dir DIR    where the command goes, as a link to FOLDER/run_web.sh; default /usr/local/bin
#   --no-link        leave the command alone (e.g. to try a build in another FOLDER)
#   --unit-dir DIR   write the service file into DIR and do not touch systemd (to check it)
#
# The service restarts the server when it crashes and at boot. A run goes on when the server restarts
# (KillMode=process: the DAQ program is not stopped with the server), so building again during a run is safe.
# The CAEN libraries of caen/lib (./caen_libs.sh) are copied into the build when they are there.
# BUILD_INFO in FOLDER records the version and the git commit of this folder (read only: git writes
# nothing here). The build uses only the packages in wheels/ (setup.sh downloads them from pypi.org),
# so it needs no network, and it never writes into this folder. Building again replaces the earlier build;
# if anything fails, the earlier build is put back.
#
# Check a build, as your normal user:  ./run_tests.sh --build /opt/NAME
# Remove it all:   sudo systemctl disable --now vx27webdaq; sudo rm -rf /opt/NAME
#                  /usr/local/bin/vx27webdaq /etc/systemd/system/vx27webdaq.service
set -euo pipefail
umask 022                                       # every user can read and run the build
src="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
name=Vx27webDAQ
bin_dir=/usr/local/bin
make_link=1
command=vx27webdaq
service=1 service_user="" port="" admin="" unit_dir=/etc/systemd/system
unit_name=vx27webdaq.service
old_unit_name=alab-daq-web.service             # the service of the old name (ALAB DAQ, until v0.0.18)
old_command=alab-daq-web
python="${PYTHON:-/usr/bin/python3}"
marker="Vx27webDAQ build"                  # first line of BUILD_INFO: only such folders are replaced

fail() { echo "build.sh: $*" >&2; exit 1; }
is_build() { [ -f "$1/BUILD_INFO" ] && [ "$(head -n 1 "$1/BUILD_INFO")" = "$marker" ]; }

while [ $# -gt 0 ]; do
    case "$1" in
        --service-user) service_user="${2:?--service-user needs an account}"; shift 2 ;;
        --port) port="${2:?--port needs a number}"; shift 2 ;;
        --admin) admin="${2:?--admin needs a user name}"; shift 2 ;;
        --no-service) service=0; shift ;;
        --bin-dir) bin_dir="${2:?--bin-dir needs a folder}"; shift 2 ;;
        --no-link) make_link=0; shift ;;
        --unit-dir) unit_dir="${2:?--unit-dir needs a folder}"; shift 2 ;;
        -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
        -*) fail "unknown option $1 (see ./build.sh --help)" ;;
        *) name="$1"; shift ;;
    esac
done
case "$name" in
    /*) dest="$(realpath -m "$name")" ;;
    */*|.|..|"") fail "FOLDER is a plain name (built in /opt/NAME) or an absolute path, not '$name'" ;;
    *) dest="$(realpath -m "/opt/$name")" ;;
esac
old="$dest.previous"
link="$bin_dir/$command"
unit="$unit_dir/$unit_name"
systemd=0                                       # really install and restart the service (root, the real place)
[ "$service" = 1 ] && [ "$unit_dir" = /etc/systemd/system ] && systemd=1

# ---- checks: nothing is changed before all of them pass ----
case "$dest/" in "$src"/*) fail "FOLDER must be outside the source folder $src" ;; esac
case "$src/" in "$dest"/*) fail "FOLDER must not contain the source folder $src" ;; esac
[ -d "$(dirname "$dest")" ] || fail "$(dirname "$dest") does not exist"
[ -w "$(dirname "$dest")" ] || fail "cannot write in $(dirname "$dest"): run it with sudo"
if [ -e "$dest" ] && ! is_build "$dest"; then fail "$dest exists and is not a build of this project; not touching it"; fi
if [ -e "$old" ] && ! is_build "$old"; then fail "$old exists and is not a build of this project; not touching it"; fi
if [ "$make_link" = 1 ]; then
    [ -d "$bin_dir" ] && [ -w "$bin_dir" ] || fail "cannot write in $bin_dir: run it with sudo, or choose --bin-dir"
    if [ -e "$link" ] && [ ! -L "$link" ]; then fail "$link exists and is not a link; not replacing it"; fi
fi
if [ "$service" = 1 ]; then
    installed="/etc/systemd/system/$unit_name"
    [ -f "$installed" ] || installed="/etc/systemd/system/$old_unit_name"     # the first build after the rename
    if [ -f "$installed" ]; then                # the service keeps its account and port unless given anew
        service_user="${service_user:-$(sed -n 's/^User=//p' "$installed")}"
        port="${port:-$(sed -n 's/.* --port \([0-9]*\).*/\1/p' "$installed")}"
    fi
    [ -n "$service_user" ] || fail "the first build needs --service-user NAME: the account that runs the server and the DAQ program (it must write the data folder); or --no-service"
    id -u "$service_user" >/dev/null 2>&1 || fail "there is no account $service_user"
    [ "$(id -u "$service_user")" != 0 ] || fail "the server never runs as root: choose a normal account"
    [ -d "$unit_dir" ] && [ -w "$unit_dir" ] || fail "cannot write in $unit_dir: run it with sudo, or --no-service"
    if [ "$systemd" = 1 ]; then
        [ -d /run/systemd/system ] || fail "this computer does not run systemd: use --no-service"
    fi
fi
port="${port:-8770}"
case "$port" in ''|*[!0-9]*) fail "--port needs a number, not '$port'" ;; esac
if [ -n "$admin" ] && [ "$systemd" != 1 ]; then fail "--admin goes with the service (it makes the account in the service account's files)"; fi
[ -f "$src/requirements.lock" ] || fail "requirements.lock is missing in $src"
ls "$src"/wheels/*.whl >/dev/null 2>&1 || fail "no packages in $src/wheels: run ./setup.sh first (as your normal user)"
[ -d "$src/src/vx27webdaq/web" ] || fail "the code is missing: $src/src/vx27webdaq/web"
[ -x "$python" ] || fail "no Python at $python (set PYTHON=...)"

# the git version of this folder, read only: as root, git may read this folder of another user
# (safe.directory) but must not write into it (no index refresh: --no-optional-locks)
revision="not a git repository"
if command -v git >/dev/null 2>&1 && [ -d "$src/.git" ]; then
    git_ro=(git -c safe.directory="$src" -C "$src" --no-optional-locks)
    revision="$("${git_ro[@]}" describe --tags --always 2>/dev/null || echo unknown)"
    changed="$("${git_ro[@]}" status --porcelain 2>/dev/null | wc -l)"
    if [ "$changed" -gt 0 ]; then
        revision="$revision + $changed file(s) changed and not committed"
        echo "Note: $src has $changed file(s) that are not committed; BUILD_INFO says so"
    fi
fi

# ---- build; on any failure the earlier build is put back ----
moved_old=0 created=0 finished=0
cleanup() {
    [ "$finished" = 1 ] && return
    if [ "$created" = 1 ]; then rm -rf "$dest"; fi
    if [ "$moved_old" = 0 ]; then
        echo "build.sh: the build failed; nothing was installed" >&2
    elif mv "$old" "$dest"; then
        echo "build.sh: the build failed; the earlier build is back in $dest" >&2
    else
        echo "build.sh: the build failed; the earlier build is in $old" >&2
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP PIPE               # Ctrl-C, kill, a dropped ssh session: clean up too
echo "Building $dest from $src with $("$python" -V 2>&1)"
[ -e "$old" ] && rm -rf "$old"
if [ -e "$dest" ]; then mv "$dest" "$old"; moved_old=1; fi
mkdir "$dest"
created=1

# 1. Python environment, from wheels/ only (no network; no pip.conf may add other sources)
export PIP_CONFIG_FILE=/dev/null
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_NO_INDEX
"$python" -m venv --prompt VX27_ENV "$dest/VX27_ENV"
py="$dest/VX27_ENV/bin/python"
"$py" -m pip --disable-pip-version-check install --no-cache-dir --quiet --root-user-action=ignore \
    --no-index --find-links "$src/wheels" -r "$src/requirements.lock"

# 2. the code and the page, compiled here once, so that no user has to write .pyc files
mkdir "$dest/src"
cp -R "$src/src/vx27webdaq" "$dest/src/"
find "$dest/src" -name __pycache__ -prune -exec rm -rf {} +
"$py" -m compileall -q "$dest/src" >/dev/null
cp "$src/run_web.sh" "$src/README.md" "$src/INSTALL.md" "$src/CHANGELOG.md" "$src/requirements.lock" "$dest/"
if [ -d "$src/docs" ]; then cp -R "$src/docs" "$dest/"; fi     # the README links to docs/ (the guide for kids)
if [ -f "$src/caen/lib/libCAEN_FELib.so" ] && [ -f "$src/caen/lib/libCAEN_Dig2.so" ]; then
    mkdir -p "$dest/caen"
    cp -a "$src/caen/lib" "$dest/caen/"
    cp "$src/caen/VERSIONS" "$dest/caen/" 2>/dev/null || true
    caen_libs="caen/lib ($(head -n 1 "$src/caen/VERSIONS" 2>/dev/null || echo built))"
else
    caen_libs="none (only the simulator runs; ./caen_libs.sh builds them, then build again)"
fi

# 3. check the build: the code loads from it, with its packages, and the page is complete
version="$(cd "$dest" && env -i PATH=/usr/bin:/bin PYTHONPATH="$dest/src" "$py" -B -c '
import sys
import vx27webdaq
import uproot, aiohttp, importlib.util
if importlib.util.find_spec("caen_felib") is None:
    sys.exit("the package caen_felib is missing")
from vx27webdaq.web.server import STATIC, create_app
if not vx27webdaq.__file__.startswith(sys.argv[1] + "/"):
    sys.exit("the code was loaded from " + vx27webdaq.__file__)
for need in ("index.html", "js/app.js", "i18n/zh.json", "vendor/uplot/uPlot.iife.min.js"):
    if not (STATIC / need).is_file():
        sys.exit("the page lacks " + need)
print(vx27webdaq.__version__)' "$dest/src")"

# 4. build record, permissions, the command
{
    echo "$marker"
    echo "version:  v$version"
    echo "built:    $(date '+%Y-%m-%d %H:%M:%S') by ${SUDO_USER:-$(id -un)}"
    echo "source:   $src"
    echo "commit:   $revision"
    echo "python:   $("$py" -V 2>&1)"
    echo "packages: $(grep -v '^#' "$src/requirements.lock" | tr '\n' ' ')"
    echo "CAEN:     $caen_libs"
    if [ "$service" = 1 ]; then echo "service:  $unit_name as $service_user on port $port"; fi
} > "$dest/BUILD_INFO"
chmod -R a+rX,go-w "$dest"
before=""
if [ "$make_link" = 1 ]; then
    before="$(readlink "$link" 2>/dev/null || true)"
    ln -sfn "$dest/run_web.sh" "$link"
fi
finished=1
if [ "$moved_old" = 1 ]; then rm -rf "$old"; fi
echo "Built $dest (Vx27webDAQ v$version, commit $revision)."
if [ -n "$before" ] && [ "$before" != "$dest/run_web.sh" ]; then
    echo "Note: $command started $before until now; from now on it starts this build"
fi

# 5. the service: written, then (as root, in the real place) enabled and restarted with the new build
if [ "$service" = 1 ]; then
    tmp_unit="$(mktemp "$unit_dir/.$unit_name.XXXXXX")"
    cat > "$tmp_unit" <<UNIT
# Made by $src/build.sh on $(date '+%Y-%m-%d %H:%M:%S'); build.sh writes it again on every build.
[Unit]
Description=Vx27webDAQ web server (multi-board runs of CAEN Digitizer 2.0 boards)
Documentation=file://$dest/README.md
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Type=simple
User=$service_user
ExecStart=$dest/run_web.sh serve --host 127.0.0.1 --port $port
Restart=on-failure
RestartSec=5
# a run goes on when the server stops or restarts: only the server is stopped, not the DAQ program
KillMode=process
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
    chmod 644 "$tmp_unit"
    mv "$tmp_unit" "$unit"
    if [ "$systemd" = 1 ]; then
        old_unit="/etc/systemd/system/$old_unit_name"
        if [ -f "$old_unit" ]; then           # the old service goes (same port); a run going is not stopped
            old_dest="$(sed -n 's/^ExecStart=\(.*\)\/run_web.sh .*/\1/p' "$old_unit")"
            systemctl disable --now "$old_unit_name" >/dev/null 2>&1 || true
            rm -f "$old_unit"
            old_link="$bin_dir/$old_command"
            if [ -L "$old_link" ]; then rm -f "$old_link"; fi
            echo "Removed the old service $old_unit_name and the command $old_command (renamed to $unit_name, $command)."
            if [ -n "$old_dest" ] && [ -d "$old_dest" ]; then
                echo "The old build is still in $old_dest; once this one works:  sudo rm -rf $old_dest"
            fi
        fi
        systemctl daemon-reload
        systemctl enable "$unit_name" >/dev/null 2>&1
        systemctl restart "$unit_name"
        sleep 2
        if systemctl is-active --quiet "$unit_name"; then
            echo "The service $unit_name runs (as $service_user, port $port)."
        else
            echo "build.sh: the service did not start; see: sudo systemctl status $unit_name; sudo journalctl -u $unit_name" >&2
        fi
        if [ -n "$admin" ]; then
            if runuser -u "$service_user" -- "$dest/run_web.sh" user list | grep -q "^$admin "; then
                echo "The account $admin exists already."
            else
                echo "The first admin, $admin: choose a password (at least 8 characters)."
                runuser -u "$service_user" -- "$dest/run_web.sh" user add "$admin" --role admin
            fi
        fi
    else
        echo "Wrote $unit (systemd left alone: --unit-dir)."
    fi
fi

echo
echo "Open the page:  on your computer  ssh -L $port:localhost:$port $(hostname)   then  http://localhost:$port"
[ "$service" = 1 ] || echo "Start it:       $dest/run_web.sh serve --port $port    (as the account that runs the DAQ)"
echo "Accounts:       sudo -u ${service_user:-ACCOUNT} $command user add NAME --role viewer|operator|admin"
echo "Check it:       $src/run_tests.sh --build $dest    (as your normal user)"
